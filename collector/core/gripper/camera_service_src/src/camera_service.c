/* pthread_timedjoin_np 是 GNU 扩展，必须在任何头文件之前打开。 */
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/uio.h>
#include <time.h>
#include <unistd.h>

#include <libuvc/libuvc.h>

#include <dlfcn.h>
#include <pthread.h>

#define MAX_CAMERAS 8
#define FRAME_QUEUE_DEPTH 4
#define MAX_LINE 512
#define IPC_MAGIC 0x4b535146u /* "KSQF" */
#define IPC_VERSION 1u
/* 等时流静默停摆看门狗。DECXIN (1bcf:2d4f) 会在 6~82 秒之间无声停止推帧，
 * 此后永不恢复，内核也不报任何错误（实测：同一总线同一 Hub 上的两路
 * Sightac 全程 30fps 正常，只有 DECXIN 自己死）。停滞不是「暂时掉帧」：
 * libuvc 丢完等时包后不会自愈，必须重开流。停滞超过这个秒数就中断本路，
 * 交给 camera_main 的重试循环重开——否则整场会话只剩最后一帧，客户端
 * 重连也只能重连进空气。 */
#define STREAM_STALL_TIMEOUT_SECONDS 3.0

/* 首次启动等首帧的时限（秒）。 */
#define FIRST_FRAME_TIMEOUT_SECONDS 4.0
/* 停摆重开时等首帧的时限（秒）。实测 DECXIN 被拆流后不会立刻回来，
 * 4 秒内一帧都没有（会被误判成 startup failed 再拆一次，越拆越糟），
 * 放宽到 20 秒再判死。 */
#define RESTART_FIRST_FRAME_TIMEOUT_SECONDS 20.0

/* 连着停摆几次才降档。
 *
 * 设成 2 而不是 1：单次停摆多半只是设备自己抽了一下——实测停摆 6 次里每次
 * 单靠重建 libusb 上下文就能恢复；一停就降会把 alt7 的带宽白白让出去，而
 * alt6 的单帧上限只有 241664 B（实测最大帧 234148 B，3% 余量），降下去是有
 * 代价的。两次说明当前档位确实撑不住：实测 alt7 在整机三路出流时每 45~130 秒
 * 必停一次（AMD 1022:15b7 / PCI 0000:74:00.4，以及 1022:43fc / PCI
 * 0000:0a:00.0，两块都是），两次最多赔 260 秒，且结论会落盘，每台机器只赔这
 * 一回。 */
#define STALLS_BEFORE_ALTSETTING_DOWNGRADE 2

/* 开不起来（rc<0）连着几次才降档。
 *
 * 与停摆分开计、门槛也更高：开不起来多半是设备侧卡死，而那有自己的一条恢复
 * 路径（连续 2 次失败升级成 USB 复位，实测 t≈3 秒就能出帧），一开不起来就降
 * 档会在正常恢复路径上白降。门槛 3 意味着复位试过、还是开不起来，档位才是
 * 剩下的嫌疑。要覆盖的是另一种失败：STREAMON 阶段就排不下高带宽等时档
 * （libusb_set_interface_alt_setting 报 -ENOSPC），此时流根本不会启动，
 * 光靠停摆那条路永远等不到。 */
#define OPEN_FAILURES_BEFORE_ALTSETTING_DOWNGRADE 3

/* 一段流稳定跑够这么久，就把「本档位停摆计数」清零。
 * 要比实测的停摆间隔（45~130 秒）明显长，否则计数永远清不掉。 */
#define CLEAN_RUN_SECONDS 300.0

/* 复位 USB 设备（等价于拔插一次）。只在一个场合用：重开之后一帧都没有，
 * 说明设备侧真的卡住了（典型是上一次服务被 SIGKILL、设备停在激活的等时
 * alt setting 上），只重建上下文救不回来。
 *
 * 平时停摆恢复**不要**走这里：复位是 USB 端口级操作，会重新枚举整条链路，
 * 连带打扰同一总线同一 Hub 上的另外两路 Sightac（实测每次复位两个 Sightac
 * 都掉一批帧）。实测停摆 6 次只用「重建 libusb 上下文」就 6 次全恢复，复位
 * 是白付的代价。
 *
 * libusb 是 libuvc 的依赖、进程里必然已加载，所以按符号名取而不是直接链接
 * ——这样不给二进制新增 NEEDED，交付树里 libuvc/libusb 的解析结果不变。 */
typedef int (*libusb_reset_device_fn)(struct libusb_device_handle *);

static libusb_reset_device_fn resolve_libusb_reset_device(void) {
    static libusb_reset_device_fn cached = NULL;
    static int resolved = 0;
    if (!resolved) {
        cached = (libusb_reset_device_fn)dlsym(RTLD_DEFAULT,
                                               "libusb_reset_device");
        resolved = 1;
    }
    return cached;
}

static volatile sig_atomic_t g_stop = 0;
static int g_parent_pid = 0;

typedef struct {
    char name[64];
    int vid;
    int pid;
    int usb_bus;
    char usb_port_path[64];
    int streaming_interface;
    /* 实际生效的等时档位。这两项不是「声明」——run_camera_once 打开设备后
     * 会把它们交给 libuvc 的 uvc_set_altsetting_override()，由它同时决定
     * COMMIT 的 payload 和 libusb 的等时传输几何。留空（-1）时退回 libuvc
     * 的编译期兜底表。 */
    int forced_altsetting;
    int forced_payload;
    int width;
    int height;
    int fps;
    char format[16];
    /* 相机的 USB 序列号、所在控制器的 PCI 路径、以及学习结果的存放目录。
     * 三者只用来给「这台相机 × 这条物理路径」的档位选择建一个稳定的键，
     * 见 mode_state_key()。控制器只进键、不参与选档。 */
    char usb_serial[64];
    char usb_controller[64];
    char state_dir[PATH_MAX];
    char socket_path[PATH_MAX];
} camera_config_t;

typedef struct {
    uint32_t magic;
    uint16_t version;
    uint16_t header_bytes;
    uint32_t sequence;
    uint32_t width;
    uint32_t height;
    uint32_t fourcc;
    uint32_t payload_bytes;
    uint64_t timestamp_ns;
} ipc_frame_header_t;

typedef struct {
    uint8_t *data;
    size_t length;
    uint32_t sequence;
    uint64_t capture_timestamp_ns;
} frame_item_t;

typedef struct camera_state camera_state_t;

struct camera_state {
    camera_config_t config;
    pthread_t thread;
    pthread_t writer_thread;
    pthread_mutex_t mutex;
    pthread_cond_t condition;
    frame_item_t queue[FRAME_QUEUE_DEPTH];
    size_t head;
    size_t count;
    int stopping;
    int listen_fd;
    int client_fd;
    uint64_t frames_received;
    uint64_t frames_output;
    uint64_t frames_dropped;
    uint64_t bad_jpeg_frames;
    uint64_t bad_jpeg_logs;
    uint64_t bad_jpeg_replaced;
    uint64_t write_errors;
    uint64_t reconnects;
    uint64_t stalls;
    /* 当前档位下已经停摆了几次，连着停够 STALLS_BEFORE_ALTSETTING_DOWNGRADE
     * 次就降到下一档；中间只要有一段够长的稳定运行就清零（那次停摆是设备
     * 自己抽了一下，不是档位撑不住）。 */
    unsigned stalls_at_mode;
    /* 当前档位下连着几次没能把流开起来（rc<0）。任何一次「开起来了」的证据
     * 都会清零它——停摆也算，因为停摆说明设备是开得起来的。 */
    unsigned open_failures_at_mode;
    /* 连续「开不起来」的次数（rc<0）。与 stalls 分开计：停摆说明设备能开、
     * 能出帧，只是中途死了；连续开不起来才是设备侧卡死的signature。 */
    unsigned hard_failures;
    unsigned usb_resets;

};

static int has_mjpeg_soi(const uint8_t *data, size_t length) {
    return length >= 2 && data[0] == 0xff && data[1] == 0xd8;
}

static int has_mjpeg_eoi(const uint8_t *data, size_t length) {
    /* Some firmware variants append padding after the EOI marker, so scan
     * backwards instead of requiring the final two bytes to be exactly
     * FF D9.  A truncated scan will not contain the marker at all. */
    for (size_t index = length; index >= 2; --index) {
        if (data[index - 2] == 0xff && data[index - 1] == 0xd9) {
            return 1;
        }
    }
    return 0;
}

static int is_complete_mjpeg_payload(const uint8_t *data, size_t length) {
    return has_mjpeg_soi(data, length) && has_mjpeg_eoi(data, length);
}

static void request_stop(int signal_number) {
    (void)signal_number;
    g_stop = 1;
}

static char *trim(char *value) {
    char *end;
    while (*value == ' ' || *value == '\t' || *value == '\r' ||
           *value == '\n') {
        ++value;
    }
    end = value + strlen(value);
    while (end > value && (end[-1] == ' ' || end[-1] == '\t' ||
                           end[-1] == '\r' || end[-1] == '\n')) {
        --end;
    }
    *end = '\0';
    return value;
}

static int parse_int(const char *value, int *result) {
    char *end = NULL;
    long parsed;
    errno = 0;
    parsed = strtol(value, &end, 0);
    if (errno != 0 || end == value || *trim(end) != '\0' ||
        parsed < INT_MIN || parsed > INT_MAX) {
        return -1;
    }
    *result = (int)parsed;
    return 0;
}

static void set_defaults(camera_config_t *config) {
    memset(config, 0, sizeof(*config));
    config->usb_bus = -1;
    config->streaming_interface = 1;
    config->forced_altsetting = -1;
    config->forced_payload = -1;
    config->width = 640;
    config->height = 480;
    config->fps = 30;
    snprintf(config->format, sizeof(config->format), "MJPEG");
}

static int assign_key(camera_config_t *config, const char *key,
                      const char *value) {
    if (strcmp(key, "vid") == 0) {
        return parse_int(value, &config->vid);
    }
    if (strcmp(key, "pid") == 0) {
        return parse_int(value, &config->pid);
    }
    if (strcmp(key, "usb_bus") == 0) {
        return parse_int(value, &config->usb_bus);
    }
    if (strcmp(key, "streaming_interface") == 0) {
        return parse_int(value, &config->streaming_interface);
    }
    if (strcmp(key, "forced_altsetting") == 0) {
        return parse_int(value, &config->forced_altsetting);
    }
    if (strcmp(key, "forced_payload") == 0) {
        return parse_int(value, &config->forced_payload);
    }
    if (strcmp(key, "width") == 0) {
        return parse_int(value, &config->width);
    }
    if (strcmp(key, "height") == 0) {
        return parse_int(value, &config->height);
    }
    if (strcmp(key, "fps") == 0) {
        return parse_int(value, &config->fps);
    }
    if (strcmp(key, "usb_port_path") == 0) {
        snprintf(config->usb_port_path, sizeof(config->usb_port_path),
                 "%s", value);
        return 0;
    }
    if (strcmp(key, "usb_serial") == 0) {
        snprintf(config->usb_serial, sizeof(config->usb_serial), "%s", value);
        return 0;
    }
    if (strcmp(key, "usb_controller") == 0) {
        snprintf(config->usb_controller, sizeof(config->usb_controller),
                 "%s", value);
        return 0;
    }
    if (strcmp(key, "state_dir") == 0) {
        snprintf(config->state_dir, sizeof(config->state_dir), "%s", value);
        return 0;
    }
    if (strcmp(key, "socket_path") == 0) {
        snprintf(config->socket_path, sizeof(config->socket_path), "%s", value);
        return 0;
    }
    if (strcmp(key, "format") == 0) {
        snprintf(config->format, sizeof(config->format),
                 "%s", value);
        return 0;
    }
    if (strcmp(key, "name") == 0) {
        snprintf(config->name, sizeof(config->name), "%s", value);
        return 0;
    }
    return 1;
}

static int is_supported_device(const camera_config_t *config) {
    return (config->vid == 0x0c45 && config->pid == 0x636f) ||
           (config->vid == 0x1bcf && config->pid == 0x2d4f);
}

/* 每个机型允许的「档位对」（altsetting, payload），按从好到差的顺序排列。
 * 第 0 项是带宽最好的一档，停摆降级时往后走一格，走到最后一项就不再降
 * ——**降级方向必须是从好到差，表序不能颠倒**。
 *
 * 这两张表必须与 libuvc 的编译期兜底表（third_party/libuvc/src/stream.c
 * 的 ksq_altsetting_quirks）以及扫描程序（tools/discover_uvc_config.c 的
 * profile_for）保持一致：libuvc 的表决定「没有覆盖值时的默认」，这里决定
 * 「服务愿意接受哪些值」。
 *
 * **起始档位不在这张表里定**：起手用哪一档由 uvc_camera_service.py 的
 * _starting_mode 写进 ini（表里最保守的一档，即最后一项），服务只负责接受
 * 表内的值、并在反复停摆时降档。
 *
 * DECXIN 两档的账：alt7/1280 是 2 事务/微帧、10.24 MB/s，单帧上限 327680 B；
 * alt6/944 是单事务档里最大的一档，8 transfer × 32 包 × 944 B = 241664 B 就是
 * 单帧硬上限，而实测最大帧 234148 B——只剩 3% 余量。也就是说 alt7 的余量更
 * 好看，但实测 alt7 在整机三路一起出流时每 45~130 秒必停摆一次，而 alt6 长跑
 * 不停，**两档的出帧率完全一样（都 30.00fps）**——多出来的余量换不到任何看得
 * 见的好处，所以默认就是 alt6。哪一档真的撑不住不靠推测，靠停摆实测往下走
 * （见 maybe_downgrade_altsetting）。 */
typedef struct {
    int altsetting;
    int payload;
} altsetting_mode_t;

static const altsetting_mode_t kSightacModes[] = {
    {3, 800},
};
static const altsetting_mode_t kDecxinModes[] = {
    {7, 1280},
    {6, 944},
};

static const altsetting_mode_t *mode_table_for(const camera_config_t *config,
                                               size_t *count) {
    if (config->vid == 0x0c45 && config->pid == 0x636f) {
        *count = sizeof(kSightacModes) / sizeof(kSightacModes[0]);
        return kSightacModes;
    }
    if (config->vid == 0x1bcf && config->pid == 0x2d4f) {
        *count = sizeof(kDecxinModes) / sizeof(kDecxinModes[0]);
        return kDecxinModes;
    }
    *count = 0;
    return NULL;
}

static size_t mode_index_for(const camera_config_t *config) {
    size_t count = 0;
    const altsetting_mode_t *modes = mode_table_for(config, &count);
    for (size_t i = 0; i < count; ++i) {
        if (modes[i].altsetting == config->forced_altsetting &&
            modes[i].payload == config->forced_payload) {
            return i;
        }
    }
    return count;
}

/* 「这台相机 × 这条物理路径」的落盘键。
 *
 * 刻意不用总线号/端口路径：机架插在哪个口、哪条总线都不是永久的（用户
 * 2026-09-15 明确说过 bus 1 不是永久的），端口一变键就变，要么把上一台
 * 机器的结论套到新拓扑上，要么白学一遍。序列号取自相机描述符、随相机走；
 * 控制器取自 PCI 路径、随插槽走。换电脑 / 换 USB 口都会得到新键，也就是
 * 重新学一次——取保守的那一边：结论严格绑在学它的那条物理路径上。控制器
 * 本身是不是变量并无定论（2026-09-15 实测 alt7 在 74:00.4 与 0a:00.0 上都
 * 停摆），所以它只进键、不参与选档。
 *
 * Python 侧 core/gripper/devices/uvc_camera_service.py 用同一套规则造键去
 * 查这个目录，两边的清洗规则必须逐字一致。 */
static void mode_state_key(const camera_config_t *config, char *out,
                           size_t size) {
    char raw[192];
    size_t written = 0;

    snprintf(raw, sizeof(raw), "%04x_%04x_%s_%s", config->vid, config->pid,
             config->usb_controller[0] != '\0' ? config->usb_controller
                                               : "unknown",
             config->usb_serial[0] != '\0' ? config->usb_serial : "noserial");
    for (const char *cursor = raw; *cursor != '\0' && written + 1 < size;
         ++cursor) {
        char c = *cursor;
        int safe = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                   (c >= '0' && c <= '9') || c == '-';
        out[written++] = safe ? c : '_';
    }
    out[written] = '\0';
}

/* 把学到的档位写进 state_dir，下次开机直接用它，不必再赔一次停摆窗口。
 * 尽力而为：目录建不出来、盘写不进去都只记日志，绝不能因此让相机起不来。 */
static void persist_mode_choice(const camera_state_t *camera) {
    char key[200];
    char path[PATH_MAX];
    char temporary[PATH_MAX];
    FILE *file;

    if (camera->config.state_dir[0] == '\0') {
        return;
    }
    mode_state_key(&camera->config, key, sizeof(key));
    if (snprintf(path, sizeof(path), "%s/%s.mode", camera->config.state_dir,
                 key) >= (int)sizeof(path) ||
        snprintf(temporary, sizeof(temporary), "%s.tmp.%d", path,
                 (int)getpid()) >= (int)sizeof(temporary)) {
        fprintf(stderr, "[%s] state path too long; not persisting mode\n",
                camera->config.name);
        return;
    }
    if (mkdir(camera->config.state_dir, 0755) != 0 && errno != EEXIST) {
        fprintf(stderr, "[%s] cannot create state dir %s: %s\n",
                camera->config.name, camera->config.state_dir,
                strerror(errno));
        return;
    }
    file = fopen(temporary, "w");
    if (file == NULL) {
        fprintf(stderr, "[%s] cannot write %s: %s\n", camera->config.name,
                temporary, strerror(errno));
        return;
    }
    fprintf(file, "altsetting=%d\npayload=%d\n",
            camera->config.forced_altsetting, camera->config.forced_payload);
    fflush(file);
    fsync(fileno(file));
    fclose(file);
    if (rename(temporary, path) != 0) {
        fprintf(stderr, "[%s] cannot install %s: %s\n", camera->config.name,
                path, strerror(errno));
        unlink(temporary);
        return;
    }
    fprintf(stderr, "[%s] remembered altsetting=%d payload=%d for this "
            "camera/controller in %s\n", camera->config.name,
            camera->config.forced_altsetting, camera->config.forced_payload,
            path);
}

static int validate_config(const camera_config_t *config, size_t index) {
    if (config->name[0] == '\0' || config->vid <= 0 || config->pid <= 0 ||
        config->usb_bus < 0 || config->usb_port_path[0] == '\0' ||
        config->socket_path[0] == '\0' || config->streaming_interface < 0 ||
        config->forced_altsetting < 0 || config->forced_payload <= 0 ||
        config->width <= 0 || config->height <= 0 || config->fps <= 0 ||
        strcasecmp(config->format, "MJPEG") != 0) {
        fprintf(stderr, "invalid camera config at index %zu\n", index);
        return -1;
    }
    if (!is_supported_device(config)) {
        fprintf(stderr, "%s: unsupported UVC device %04x:%04x; only Sightac "
                "0c45:636f and DECXIN 1bcf:2d4f are supported\n",
                config->name, config->vid, config->pid);
        return -1;
    }
    if (config->vid == 0x0c45 && config->pid == 0x636f &&
        (config->width != 640 || config->height != 480 || config->fps != 30)) {
        fprintf(stderr, "%s: unsupported Sightac mode; expected 640x480 "
                "MJPEG@30\n", config->name);
        return -1;
    }
    if (config->vid == 0x1bcf && config->pid == 0x2d4f &&
        (config->width != 1280 || config->height != 960 || config->fps != 30)) {
        fprintf(stderr, "%s: unsupported DECXIN mode; expected 1280x960 "
                "MJPEG@30\n", config->name);
        return -1;
    }
    /* 档位对必须整体命中表里某一项。只查「alt 在表里」是不够的：alt 和
     * payload 是一个整体（alt7 的端点就是 1280 B/包），把 7 和 944 拼在
     * 一起会让 libuvc 用 alt7 的端点去跑 944 的预算，而 944 只是 COMMIT
     * 上写的数——2026-09-15 主程序连不上夹爪就是这两项被拆开配出来的。 */
    {
        size_t count = 0;
        mode_table_for(config, &count);
        if (mode_index_for(config) < count) {
            return 0;
        }
    }
    fprintf(stderr, "%s: unsupported altsetting pair alt=%d payload=%d; this "
            "camera accepts only the pairs in the service's mode table\n",
            config->name, config->forced_altsetting, config->forced_payload);
    return -1;
}

static int configs_conflict(const camera_config_t *left,
                            const camera_config_t *right) {
    if (strcmp(left->socket_path, right->socket_path) == 0) {
        return 1;
    }
    return left->vid == right->vid && left->pid == right->pid &&
           left->usb_bus == right->usb_bus &&
           strcmp(left->usb_port_path, right->usb_port_path) == 0;
}

static int load_config(const char *path, camera_config_t *configs,
                       size_t *count) {
    FILE *file;
    char line[MAX_LINE];
    camera_config_t *current = NULL;
    size_t index = 0;

    file = fopen(path, "r");
    if (file == NULL) {
        fprintf(stderr, "cannot open config %s: %s\n", path, strerror(errno));
        return -1;
    }
    while (fgets(line, sizeof(line), file) != NULL) {
        char *text = trim(line);
        char *equals;
        if (*text == '\0' || *text == '#' || *text == ';') {
            continue;
        }
        if (*text == '[') {
            char *close = strchr(text, ']');
            if (close == NULL || index >= MAX_CAMERAS) {
                fclose(file);
                return -1;
            }
            *close = '\0';
            set_defaults(&configs[index]);
            snprintf(configs[index].name, sizeof(configs[index].name),
                     "%s", trim(text + 1));
            current = &configs[index++];
            continue;
        }
        if (current == NULL || (equals = strchr(text, '=')) == NULL) {
            fclose(file);
            return -1;
        }
        *equals = '\0';
        {
            char *key = trim(text);
            char *value = trim(equals + 1);
            char *comment = strchr(value, '#');
            if (comment != NULL) {
                *comment = '\0';
                value = trim(value);
            }
            if (assign_key(current, key, value) < 0) {
                fprintf(stderr, "invalid config value %s=%s\n", key, value);
                fclose(file);
                return -1;
            }
        }
    }
    fclose(file);
    if (index == 0 || index > MAX_CAMERAS) {
        return -1;
    }
    for (size_t i = 0; i < index; ++i) {
        if (validate_config(&configs[i], i) != 0) {
            return -1;
        }
        for (size_t j = 0; j < i; ++j) {
            if (configs_conflict(&configs[i], &configs[j])) {
                fprintf(stderr,
                        "camera config conflict between %s and %s "
                        "(duplicate output or USB identity)\n",
                        configs[j].name, configs[i].name);
                return -1;
            }
        }
    }
    *count = index;
    return 0;
}

static int uvc_port_path(uvc_device_t *device, char *buffer,
                         size_t buffer_size) {
    uint8_t ports[8];
    int count;
    size_t used = 0;
    count = uvc_get_port_numbers(device, ports, sizeof(ports));
    if (count < 0) {
        return -1;
    }
    buffer[0] = '\0';
    for (int i = 0; i < count; ++i) {
        int written = snprintf(buffer + used, buffer_size - used, "%s%u",
                               i == 0 ? "" : ".", ports[i]);
        if (written < 0 || (size_t)written >= buffer_size - used) {
            return -1;
        }
        used += (size_t)written;
    }
    return 0;
}

static uvc_error_t open_matching_device(uvc_context_t *context,
                                        const camera_config_t *config,
                                        uvc_device_handle_t **result,
                                        char *actual_path,
                                        size_t actual_path_size) {
    uvc_device_t **list = NULL;
    uvc_error_t error;
    *result = NULL;
    error = uvc_get_device_list(context, &list);
    if (error != UVC_SUCCESS) {
        return error;
    }
    for (size_t i = 0; list[i] != NULL; ++i) {
        uvc_device_descriptor_t *descriptor = NULL;
        uvc_device_handle_t *candidate = NULL;
        error = uvc_get_device_descriptor(list[i], &descriptor);
        if (error != UVC_SUCCESS || descriptor == NULL ||
            descriptor->idVendor != (uint16_t)config->vid ||
            descriptor->idProduct != (uint16_t)config->pid ||
            (int)uvc_get_bus_number(list[i]) != config->usb_bus) {
            uvc_free_device_descriptor(descriptor);
            continue;
        }
        if (uvc_port_path(list[i], actual_path, actual_path_size) != 0 ||
            strcmp(actual_path, config->usb_port_path) != 0) {
            uvc_free_device_descriptor(descriptor);
            continue;
        }
        error = uvc_open(list[i], &candidate);
        uvc_free_device_descriptor(descriptor);
        if (error != UVC_SUCCESS || candidate == NULL) {
            fprintf(stderr, "[%s] matched bus=%d port=%s open failed: %s (%d)\n",
                    config->name, config->usb_bus, actual_path,
                    uvc_strerror(error), error);
            uvc_free_device_list(list, 1);
            return error == UVC_SUCCESS ? UVC_ERROR_IO : error;
        }
        *result = candidate;
        uvc_free_device_list(list, 1);
        return UVC_SUCCESS;
    }
    uvc_free_device_list(list, 1);
    return UVC_ERROR_NO_DEVICE;
}

static uint64_t timespec_nanoseconds(struct timespec value) {
    if (value.tv_sec < 0 || value.tv_nsec < 0 || value.tv_nsec >= 1000000000L) {
        return 0;
    }
    return (uint64_t)value.tv_sec * 1000000000ull +
           (uint64_t)value.tv_nsec;
}

static int configure_ipc(camera_state_t *camera) {
    struct sockaddr_un address;
    mode_t old_umask;
    size_t path_length = strlen(camera->config.socket_path);

    if (path_length >= sizeof(address.sun_path)) {
        fprintf(stderr, "[%s] Unix socket path is too long: %s\n",
                camera->config.name, camera->config.socket_path);
        return -1;
    }
    /*
     * Use a byte-stream Unix socket instead of SOCK_SEQPACKET.  A DECXIN
     * MJPEG frame can exceed the kernel's AF_UNIX packet-size limit; a
     * length-bearing stream header avoids EMSGSIZE while retaining one
     * complete frame per read_packet() at the protocol level.
     */
    camera->listen_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (camera->listen_fd < 0) {
        fprintf(stderr, "[%s] create Unix socket failed: %s\n",
                camera->config.name, strerror(errno));
        return -1;
    }
    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    memcpy(address.sun_path, camera->config.socket_path, path_length + 1);
    unlink(camera->config.socket_path);
    old_umask = umask(0);
    if (bind(camera->listen_fd, (struct sockaddr *)&address,
             sizeof(address)) < 0) {
        umask(old_umask);
        fprintf(stderr, "[%s] bind Unix socket %s failed: %s\n",
                camera->config.name, camera->config.socket_path,
                strerror(errno));
        close(camera->listen_fd);
        camera->listen_fd = -1;
        return -1;
    }
    umask(old_umask);
    if (chmod(camera->config.socket_path, 0666) != 0 ||
        listen(camera->listen_fd, 1) != 0) {
        fprintf(stderr, "[%s] prepare Unix socket %s failed: %s\n",
                camera->config.name, camera->config.socket_path,
                strerror(errno));
        close(camera->listen_fd);
        camera->listen_fd = -1;
        unlink(camera->config.socket_path);
        return -1;
    }
    fprintf(stderr, "[%s] IPC socket=%s format=MJPEG size=%dx%d fps=%d\n",
            camera->config.name, camera->config.socket_path,
            camera->config.width, camera->config.height, camera->config.fps);
    return 0;
}

static int accept_ipc_client(camera_state_t *camera) {
    while (!g_stop && camera->client_fd < 0) {
        struct pollfd descriptor = {
            .fd = camera->listen_fd,
            .events = POLLIN,
            .revents = 0,
        };
        int ready = poll(&descriptor, 1, 500);
        if (ready < 0 && errno == EINTR) {
            continue;
        }
        if (ready <= 0) {
            continue;
        }
        camera->client_fd = accept(camera->listen_fd, NULL, NULL);
        if (camera->client_fd < 0 && errno != EINTR) {
            return errno;
        }
    }
    return camera->client_fd >= 0 ? 0 : ECANCELED;
}

static int send_all(int fd, const void *data, size_t length) {
    const uint8_t *cursor = (const uint8_t *)data;
    while (length > 0) {
        ssize_t sent = send(fd, cursor, length, MSG_NOSIGNAL);
        if (sent < 0) {
            if (errno == EINTR) {
                continue;
            }
            return errno;
        }
        if (sent == 0) {
            return EPIPE;
        }
        cursor += (size_t)sent;
        length -= (size_t)sent;
    }
    return 0;
}

static int write_frame(camera_state_t *camera, const frame_item_t *item) {
    ipc_frame_header_t header;
    int error;

    if (accept_ipc_client(camera) != 0) {
        return g_stop ? ECANCELED : ENOTCONN;
    }
    memset(&header, 0, sizeof(header));
    header.magic = IPC_MAGIC;
    header.version = IPC_VERSION;
    header.header_bytes = (uint16_t)sizeof(header);
    header.sequence = item->sequence;
    header.width = (uint32_t)camera->config.width;
    header.height = (uint32_t)camera->config.height;
    header.fourcc = (uint32_t)('M' | ('J' << 8) | ('P' << 16) | ('G' << 24));
    header.payload_bytes = (uint32_t)item->length;
    /*
     * Preserve the timestamp assigned by libuvc when the complete UVC frame
     * was received.  The socket writer can run later (or burst after a
     * client connects), so send time would measure IPC scheduling rather than
     * camera acquisition cadence.
     */
    header.timestamp_ns = item->capture_timestamp_ns;

    error = send_all(camera->client_fd, &header, sizeof(header));
    if (error == 0) {
        error = send_all(camera->client_fd, item->data, item->length);
    }
    /* Any partial write invalidates byte-stream framing, including timeout. */
    if (error != 0) {
        close(camera->client_fd);
        camera->client_fd = -1;
    }
    return error;
}

static void enqueue_frame(uvc_frame_t *frame, camera_state_t *camera) {
    uint8_t *copy;
    size_t tail;
    uint64_t capture_timestamp_ns;
    uint8_t *source = NULL;
    size_t source_length = 0;
    uint32_t source_sequence = 0;
    if (frame == NULL || frame->data == NULL || frame->data_bytes == 0 ||
        frame->frame_format != UVC_FRAME_FORMAT_MJPEG) {
        return;
    }
    if (!is_complete_mjpeg_payload(frame->data, frame->data_bytes)) {
        int should_log;
        uint64_t bad_count;
        pthread_mutex_lock(&camera->mutex);
        camera->bad_jpeg_frames++;
        camera->bad_jpeg_logs++;
        camera->frames_dropped++;
        bad_count = camera->bad_jpeg_frames;
        should_log = bad_count == 1 || camera->bad_jpeg_logs % 100 == 0;
        pthread_mutex_unlock(&camera->mutex);
        if (should_log) {
            fprintf(stderr,
                    "[%s] dropped incomplete MJPEG sequence=%u bytes=%zu "
                    "bad=%llu\n",
                    camera->config.name, frame->sequence, frame->data_bytes,
                    (unsigned long long)bad_count);
        }
        /* Never fabricate a new sample from an older image.  The sequence
         * gap is intentional and preserves honest acquisition timestamps. */
        return;
    }
    source = frame->data;
    source_length = frame->data_bytes;
    source_sequence = frame->sequence;
    capture_timestamp_ns = timespec_nanoseconds(frame->capture_time_finished);
    if (capture_timestamp_ns == 0) {
        pthread_mutex_lock(&camera->mutex);
        camera->frames_dropped++;
        pthread_mutex_unlock(&camera->mutex);
        return;
    }
    copy = malloc(source_length);
    if (copy == NULL) {
        pthread_mutex_lock(&camera->mutex);
        camera->frames_dropped++;
        pthread_mutex_unlock(&camera->mutex);
        return;
    }
    memcpy(copy, source, source_length);
    pthread_mutex_lock(&camera->mutex);
    if (camera->stopping) {
        free(copy);
        pthread_mutex_unlock(&camera->mutex);
        return;
    }
    if (camera->count == FRAME_QUEUE_DEPTH) {
        free(camera->queue[camera->head].data);
        camera->head = (camera->head + 1) % FRAME_QUEUE_DEPTH;
        camera->count--;
        camera->frames_dropped++;
    }
    tail = (camera->head + camera->count) % FRAME_QUEUE_DEPTH;
    camera->queue[tail].data = copy;
    camera->queue[tail].length = source_length;
    camera->queue[tail].sequence = source_sequence;
    camera->queue[tail].capture_timestamp_ns = capture_timestamp_ns;
    camera->count++;
    camera->frames_received++;

    pthread_cond_signal(&camera->condition);
    pthread_mutex_unlock(&camera->mutex);
}

static void frame_callback(uvc_frame_t *frame, void *user_ptr) {
    enqueue_frame(frame, (camera_state_t *)user_ptr);
}

static void *writer_main(void *user_ptr) {
    camera_state_t *camera = (camera_state_t *)user_ptr;
    for (;;) {
        frame_item_t item;
        memset(&item, 0, sizeof(item));
        pthread_mutex_lock(&camera->mutex);
        while (camera->count == 0 && !camera->stopping) {
            pthread_cond_wait(&camera->condition, &camera->mutex);
        }
        if (camera->count == 0 && camera->stopping) {
            pthread_mutex_unlock(&camera->mutex);
            break;
        }
        item = camera->queue[camera->head];
        camera->queue[camera->head].data = NULL;
        camera->head = (camera->head + 1) % FRAME_QUEUE_DEPTH;
        camera->count--;
        pthread_mutex_unlock(&camera->mutex);

        int write_error = write_frame(camera, &item);
        if (write_error == 0) {
            pthread_mutex_lock(&camera->mutex);
            camera->frames_output++;
            pthread_mutex_unlock(&camera->mutex);
        } else if (write_error == ECANCELED && g_stop) {
            /* Normal shutdown: do not report queued frames canceled by the
             * stop signal as an output failure. */
        } else {
            pthread_mutex_lock(&camera->mutex);
            camera->write_errors++;
            pthread_mutex_unlock(&camera->mutex);
            fprintf(stderr, "[%s] output write failed: %s (%d)\n",
                    camera->config.name, strerror(write_error), write_error);
        }
        free(item.data);
    }
    return NULL;
}

static double monotonic_seconds(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        return 0.0;
    }
    return (double)now.tv_sec + (double)now.tv_nsec / 1000000000.0;
}

static void report_stats(camera_state_t *camera, double elapsed,
                         uint64_t previous_received,
                         uint64_t previous_output,
                         uint64_t previous_dropped,
                         uint64_t previous_bad_jpeg) {
    uint64_t received;
    uint64_t output;
    uint64_t dropped;
    uint64_t bad_jpeg;
    uint64_t bad_replaced;
    uint64_t write_errors;
    size_t queued;

    pthread_mutex_lock(&camera->mutex);
    received = camera->frames_received;
    output = camera->frames_output;
    dropped = camera->frames_dropped;
    bad_jpeg = camera->bad_jpeg_frames;
    bad_replaced = camera->bad_jpeg_replaced;
    write_errors = camera->write_errors;
    queued = camera->count;
    pthread_mutex_unlock(&camera->mutex);
    if (elapsed <= 0.0) {
        return;
    }
    fprintf(stderr,
            "[%s] fps_in=%.2f fps_out=%.2f received=%llu output=%llu "
            "dropped=%llu (+%llu) bad_jpeg=%llu (+%llu) "
            "bad_replaced=%llu write_errors=%llu queue=%zu\n",
            camera->config.name,
            (double)(received - previous_received) / elapsed,
            (double)(output - previous_output) / elapsed,
            (unsigned long long)received, (unsigned long long)output,
            (unsigned long long)dropped,
            (unsigned long long)(dropped - previous_dropped),
            (unsigned long long)bad_jpeg,
            (unsigned long long)(bad_jpeg - previous_bad_jpeg),
            (unsigned long long)bad_replaced,
            (unsigned long long)write_errors, queued);
}

static void close_ipc(camera_state_t *camera) {
    pthread_mutex_lock(&camera->mutex);
    camera->stopping = 1;
    pthread_cond_broadcast(&camera->condition);
    pthread_mutex_unlock(&camera->mutex);
    if (camera->writer_thread != 0) {
        pthread_join(camera->writer_thread, NULL);
        camera->writer_thread = 0;
    }
    for (size_t i = 0; i < FRAME_QUEUE_DEPTH; ++i) {
        free(camera->queue[i].data);
        camera->queue[i].data = NULL;
    }
    camera->head = 0;
    camera->count = 0;
    if (camera->client_fd >= 0) {
        close(camera->client_fd);
        camera->client_fd = -1;
    }
    if (camera->listen_fd >= 0) {
        close(camera->listen_fd);
        camera->listen_fd = -1;
    }
    unlink(camera->config.socket_path);
}

static void reset_usb_device(camera_state_t *camera,
                             uvc_device_handle_t *device) {
    struct libusb_device_handle *usb_handle = uvc_get_libusb_handle(device);
    libusb_reset_device_fn reset_device = resolve_libusb_reset_device();
    if (usb_handle == NULL || reset_device == NULL) {
        fprintf(stderr,
                "[%s] cannot reset device (handle=%p symbol=%p)\n",
                camera->config.name, (void *)usb_handle, (void *)reset_device);
        return;
    }
    int reset_rc = reset_device(usb_handle);
    fprintf(stderr, "[%s] usb reset: rc=%d (%s)\n", camera->config.name,
            reset_rc, uvc_strerror(reset_rc));
}

/* 上一次复位之后留给客户端的余量：客户端只等 10 秒就把服务收掉，所以
 * 「第几次失败才升级成复位」是拿这个窗口倒推出来的，不是拍的。
 * 实测卡死设备的失败很快（每次约 0.3 秒 + 1 秒间隔），第 2 次失败触发
 * 复位的时刻在 t≈2 秒，重枚举加重开约 1 秒，t≈3 秒就能出帧——10 秒够用。
 * 门槛设成「连续 2 次」而不是「1 次」：单次失败往往只是设备还没缓过来
 * （那种情况重建上下文就够，实测 6/6 全恢复），一失败就复位会白白打扰
 * 同 Hub 的另外两路。
 *
 * 上限 USB_RESET_LIMIT 次：设备真坏了的时候，不设上限会变成每 2 秒复位
 * 一次、把另外两路一直按在地上。打满就不再复位，退回普通重试。 */
#define HARD_FAILURES_BEFORE_RESET 1
#define USB_RESET_LIMIT 3

/* 停摆够次数就退一档，并记住这次选择。
 *
 * 为什么要有这条路（而不是起手就用最保守档、或者按机器写死）：起手档已经
 * 是最保守的那一档了，这条路是给**操作者手动把 ini 改成高带宽档**兜底的
 * ——想要 alt7 那点每帧余量就改，真撑不住时服务停两次自己退回 alt6 并把结论
 * 落盘，下次开机直接用，不用人去记哪台机器该用哪档。按机器写死则是另一回事：
 * 机架插在哪条总线、哪台机器都不固定，写死要每台各配一份，而且写死的那个
 * 判断（「这台控制器扛不扛得住」）本身就没被测准过——试错要便宜得多。
 *
 * 返回 1 表示已降档。 */
static int maybe_downgrade_altsetting(camera_state_t *camera) {
    size_t count = 0;
    const altsetting_mode_t *modes = mode_table_for(&camera->config, &count);
    size_t index = mode_index_for(&camera->config);

    if (modes == NULL || index + 1 >= count) {
        return 0;
    }
    camera->config.forced_altsetting = modes[index + 1].altsetting;
    camera->config.forced_payload = modes[index + 1].payload;
    /* 两个计数都要归零，各自对应一条降档触发路径。漏掉哪个，那条路就
     * 只能降一次：stalls 由本函数清零，open_failures 若不清零，判等
     * （== 门槛）在降档后永远不再成立，梯子在「开不起来」那条路上走
     * 完一格就卡死，后两档形同不存在。 */
    camera->stalls_at_mode = 0;
    camera->open_failures_at_mode = 0;
    persist_mode_choice(camera);
    return 1;
}

static void escalate_to_usb_reset(camera_state_t *camera,
                                  uvc_device_handle_t *device) {
    if (camera->hard_failures < HARD_FAILURES_BEFORE_RESET) {
        return;
    }
    if (camera->usb_resets >= USB_RESET_LIMIT) {
        return;
    }
    camera->usb_resets++;
    reset_usb_device(camera, device);
}

/* 失败路径的收尾。复位要抢在 uvc_stop_streaming 之前：
 * 卡死的设备上 uvc_stop_streaming 会一直等它那批永远回不来的等时传输，
 * 实测能卡满客户端那 2 秒宽限期、被 SIGKILL 打断——进程死在这中间，
 * 设备就停在激活的 alt setting 上，下一轮启动照样起不来（用户 09:24
 * 的日志正是断在 startup failed 与 usb reset 之间）。复位会取消在途
 * 传输，先复位再停流，这条路径才走得完。 */
static void teardown_failed_open(camera_state_t *camera,
                                 uvc_device_handle_t *device) {
    escalate_to_usb_reset(camera, device);
    uvc_stop_streaming(device);
    uvc_close(device);
    close_ipc(camera);
}

static int run_camera_once(camera_state_t *camera, uvc_context_t *context) {
    uvc_device_handle_t *device = NULL;
    uvc_stream_ctrl_t control;
    char actual_path[64] = {0};
    uvc_error_t error;
    double previous_report = monotonic_seconds();
    uint64_t previous_received = 0;
    uint64_t previous_output = 0;
    uint64_t previous_dropped = 0;
    uint64_t previous_bad_jpeg = 0;

    pthread_mutex_lock(&camera->mutex);
    uint64_t initial_received = camera->frames_received;
    /* 每次重开都要把上报基线对齐到当前累计值。留在 0 会让重开后的第一次
     * 上报把「进程启动至今的全部帧数」当成本窗口的增量，算出 200+fps 这种
     * 假速率（纯日志问题，不影响出帧，但会把人带偏）。 */
    previous_received = camera->frames_received;
    previous_output = camera->frames_output;
    previous_dropped = camera->frames_dropped;
    previous_bad_jpeg = camera->bad_jpeg_frames;
    camera->stopping = 0;
    pthread_mutex_unlock(&camera->mutex);
    error = open_matching_device(context, &camera->config, &device,
                                 actual_path, sizeof(actual_path));
    if (error != UVC_SUCCESS) {
        fprintf(stderr, "[%s] device %04x:%04x bus=%d port=%s unavailable: %s\n",
                camera->config.name, camera->config.vid, camera->config.pid,
                camera->config.usb_bus, camera->config.usb_port_path,
                uvc_strerror(error));
        close_ipc(camera);
        return -1;
    }
    /* 在协商之前把 ini 里的档位钉到设备句柄上。libuvc 自己的编译期表只是
     * 兜底：真正决定用哪一档的是这份 ini，而 ini 里的值来自扫描程序实测的
     * 端点描述符、或者上一轮学到的结论。 */
    error = uvc_set_altsetting_override(device, camera->config.forced_altsetting,
                                        (unsigned int)camera->config.forced_payload);
    if (error != UVC_SUCCESS) {
        fprintf(stderr, "[%s] cannot apply altsetting override alt=%d payload=%d: %s\n",
                camera->config.name, camera->config.forced_altsetting,
                camera->config.forced_payload, uvc_strerror(error));
        teardown_failed_open(camera, device);
        return -1;
    }
    memset(&control, 0, sizeof(control));
    error = uvc_get_stream_ctrl_format_size(
        device, &control, UVC_FRAME_FORMAT_MJPEG,
        camera->config.width, camera->config.height, camera->config.fps);
    if (error != UVC_SUCCESS ||
        control.bInterfaceNumber != camera->config.streaming_interface) {
        fprintf(stderr, "[%s] UVC format/interface negotiation failed: %s interface=%u expected=%d\n",
                camera->config.name, uvc_strerror(error),
                control.bInterfaceNumber, camera->config.streaming_interface);
        teardown_failed_open(camera, device);
        return -1;
    }
    fprintf(stderr,
            "[%s] vidpid=%04x:%04x bus=%d port=%s interface=%u alt=%d payload=%d "
            "format=MJPEG size=%dx%d fps=%d socket=%s negotiated_payload=%u\n",
            camera->config.name, camera->config.vid, camera->config.pid,
            camera->config.usb_bus, actual_path, control.bInterfaceNumber,
            camera->config.forced_altsetting, camera->config.forced_payload,
            camera->config.width, camera->config.height, camera->config.fps,
            camera->config.socket_path, control.dwMaxPayloadTransferSize);
    error = uvc_start_streaming(device, &control, frame_callback, camera, 0);
    if (error != UVC_SUCCESS) {
        fprintf(stderr, "[%s] uvc_start_streaming failed: %s\n",
                camera->config.name, uvc_strerror(error));
        teardown_failed_open(camera, device);
        return -1;
    }
    /* Publish IPC only once MJPG negotiation, STREAMON and a valid frame
     * have succeeded. Socket existence is a readiness promise to the parent. */
    int first_frame = 0;
    int restarting = camera->reconnects > 0;
    double first_frame_timeout = restarting
        ? RESTART_FIRST_FRAME_TIMEOUT_SECONDS : FIRST_FRAME_TIMEOUT_SECONDS;
    double deadline = monotonic_seconds() + first_frame_timeout;
    while (!g_stop && monotonic_seconds() < deadline) {
        pthread_mutex_lock(&camera->mutex);
        first_frame = camera->frames_received > initial_received;
        pthread_mutex_unlock(&camera->mutex);
        if (first_frame) break;
        usleep(10000);
    }
    if (!first_frame || configure_ipc(camera) != 0) {
        fprintf(stderr, "[%s] startup failed: no valid MJPG frame or IPC unavailable\n", camera->config.name);
        teardown_failed_open(camera, device);
        return -1;
    }
    if (pthread_create(&camera->writer_thread, NULL, writer_main, camera) != 0) {
        fprintf(stderr, "[%s] writer thread creation failed\n", camera->config.name);
        uvc_stop_streaming(device);
        uvc_close(device);
        close_ipc(camera);
        return -1;
    }
    int stalled = 0;
    double last_progress = monotonic_seconds();
    double stream_started = last_progress;
    int clean_run_credited = 0;
    uint64_t last_received = 0;
    pthread_mutex_lock(&camera->mutex);
    last_received = camera->frames_received;
    pthread_mutex_unlock(&camera->mutex);

    while (!g_stop) {
        sleep(1);
        if (g_parent_pid > 1 && getppid() != g_parent_pid) {
            fprintf(stderr,
                    "[%s] parent %d exited; stopping stream\n",
                    camera->config.name, g_parent_pid);
            g_stop = 1;
            break;
        }
        double now = monotonic_seconds();
        if (now - previous_report >= 5.0) {
            report_stats(camera, now - previous_report,
                         previous_received, previous_output,
                        previous_dropped, previous_bad_jpeg);
            pthread_mutex_lock(&camera->mutex);
            previous_received = camera->frames_received;
            previous_output = camera->frames_output;
            previous_dropped = camera->frames_dropped;
            previous_bad_jpeg = camera->bad_jpeg_frames;
            pthread_mutex_unlock(&camera->mutex);
            previous_report = now;
        }
        /* 停滞看门狗：见 STREAM_STALL_TIMEOUT_SECONDS 的注释。这里不能
         * 只等 g_stop——流死了 g_stop 永远不会置位，重试循环也就永远
         * 轮不到，整路相机就此静默报废。 */
        pthread_mutex_lock(&camera->mutex);
        uint64_t received_now = camera->frames_received;
        pthread_mutex_unlock(&camera->mutex);
        /* 稳定跑够 CLEAN_RUN_SECONDS 就把本档位的停摆计数清零：说明这个档位
         * 是撑得住的，之前那次停摆是设备自己抽了一下。 */
        if (!clean_run_credited && now - stream_started >= CLEAN_RUN_SECONDS) {
            clean_run_credited = 1;
            camera->stalls_at_mode = 0;
        }
        if (received_now != last_received) {
            last_received = received_now;
            last_progress = now;
        } else if (now - last_progress >= STREAM_STALL_TIMEOUT_SECONDS) {
            pthread_mutex_lock(&camera->mutex);
            camera->stalls++;
            pthread_mutex_unlock(&camera->mutex);
            fprintf(stderr,
                    "[%s] stream stalled: no frame for %.1fs "
                    "(received=%llu); restarting UVC stream\n",
                    camera->config.name, now - last_progress,
                    (unsigned long long)received_now);
            stalled = 1;
            break;
        }
    }
    uvc_stop_streaming(device);
    uvc_close(device);
    close_ipc(camera);
    /* 返回 0 会被 camera_main 当成正常收尾而退出重试循环；停摆用 +1 单独
     * 标记，让 camera_main 知道还要重建 libusb 上下文。 */
    return stalled ? 1 : 0;
}

static void *camera_main(void *user_ptr) {
    camera_state_t *camera = (camera_state_t *)user_ptr;
    uvc_context_t *context = NULL;
    uvc_error_t error = uvc_init(&context, NULL);
    if (error != UVC_SUCCESS) {
        fprintf(stderr, "[%s] uvc_init failed: %s\n", camera->config.name,
                uvc_strerror(error));
        return NULL;
    }
    while (!g_stop) {
        if (g_parent_pid > 1 && getppid() != g_parent_pid) {
            fprintf(stderr,
                    "[SUPERVISION] parent %d exited; stopping service\n",
                    g_parent_pid);
            g_stop = 1;
            break;
        }
        int rc = run_camera_once(camera, context);
        if (rc == 0) {
            break;
        }
        camera->reconnects++;
        if (rc > 0) {
            /* 停摆那次是开得起来、出过帧的，设备侧没卡死；把连续失败清零，
             * 免得几次停摆的失败凑够数、误触发 USB 复位去打扰另外两路。 */
            camera->hard_failures = 0;
            /* 开得起来 ⇒ 这个档位至少被控制器接受了，清掉开不起来的计数。 */
            camera->open_failures_at_mode = 0;
            /* 当前档位连着停够次数就退一档（并落盘）。降档要在重建上下文
             * 之前生效——重开的流才会用新的 alt/payload。 */
            camera->stalls_at_mode++;
            if (camera->stalls_at_mode == STALLS_BEFORE_ALTSETTING_DOWNGRADE) {
                /* 计数要在降档之前抓下来：maybe_downgrade_altsetting 成功时
                 * 会把它清零，之后再读就只能打出「0 stalls」，正好把最该
                 * 看的那次降级说成没有依据。 */
                unsigned stalls = camera->stalls_at_mode;
                int previous_alt = camera->config.forced_altsetting;
                if (maybe_downgrade_altsetting(camera)) {
                    fprintf(stderr,
                            "[%s] %u stalls at alt=%d; dropping to alt=%d "
                            "payload=%d\n",
                            camera->config.name, stalls, previous_alt,
                            camera->config.forced_altsetting,
                            camera->config.forced_payload);
                } else {
                    fprintf(stderr,
                            "[%s] %u stalls but alt=%d is already the lowest "
                            "setting for this camera; staying put\n",
                            camera->config.name, stalls,
                            camera->config.forced_altsetting);
                }
            }
            /* 停摆：同一个 libusb 上下文里的设备对象在复位后已经失效，
             * 用它重开只会拿到一个永远不出帧的死句柄（表现为
             * 「negotiated OK 但 20 秒一帧没有」）。新进程能恢复、同进程
             * 重开不能，差别就在这里——重建上下文＝拿到与「新进程」等价
             * 的干净枚举。 */
            fprintf(stderr, "[%s] rebuilding libusb context after stall\n",
                    camera->config.name);
            uvc_exit(context);
            context = NULL;
            if (uvc_init(&context, NULL) != UVC_SUCCESS) {
                fprintf(stderr, "[%s] uvc_init failed after stall; "
                        "camera thread exiting\n", camera->config.name);
                return NULL;
            }
        } else {
            /* 开不起来（rc<0）：计数到第 2 次就升级成 USB 复位，见
             * escalate_to_usb_reset。 */
            camera->hard_failures++;
            /* 复位也救不回来时，档位是剩下的嫌疑——可能在 STREAMON 阶段就
             * 排不下这个等时档，那样流根本不会启动，停摆那条路永远等不到。 */
            camera->open_failures_at_mode++;
            if (camera->open_failures_at_mode ==
                OPEN_FAILURES_BEFORE_ALTSETTING_DOWNGRADE) {
                /* 和停摆那条路一样，计数要在降档之前抓下来：成功降档会把
                 * 它清零，之后再读就打不出触发这次降级的次数。 */
                unsigned failed = camera->open_failures_at_mode;
                int previous_alt = camera->config.forced_altsetting;
                if (maybe_downgrade_altsetting(camera)) {
                    fprintf(stderr,
                            "[%s] %u failed opens at alt=%d; dropping to "
                            "alt=%d payload=%d\n",
                            camera->config.name, failed, previous_alt,
                            camera->config.forced_altsetting,
                            camera->config.forced_payload);
                } else {
                    fprintf(stderr,
                            "[%s] %u failed opens but alt=%d is already the "
                            "lowest setting for this camera; staying put\n",
                            camera->config.name, failed,
                            camera->config.forced_altsetting);
                }
            }
        }
        if (!g_stop) {
            sleep(1);
        }
    }
    uvc_exit(context);
    return NULL;
}

static void print_usage(const char *program) {
    fprintf(stderr,
            "usage: %s --config FILE [--validate-only] "
            "[--parent-pid PID]\n",
            program);
}

/* SIGTERM 之后必须退得掉。客户端（uvc_camera_service.py）只给 2 秒宽限：
 * terminate() → 等 2 秒 → kill()。超了就是 SIGKILL，进程死在 libusb 调用
 * 中间、设备停在激活的 alt setting 上，下一轮照样起不来——正是 2026-09-15
 * 09:24 那次事故的形状。
 *
 * 光置 g_stop 不够：相机线程可能正陷在 uvc_open 的同步 libusb 调用里（卡死
 * 的设备上它不返回），永远走不到循环头那个 g_stop 判断，pthread_join 于是
 * 永远等下去。实测：生产二进制收到 SIGTERM 后 3 分钟不退，只攥着 DECXIN 的
 * fd，left/right 都已干净释放，日志停在断流那一刻——连
 * [SUPERVISION] parent exited 都没打出来。
 *
 * 所以 join 必须有界：整体 1.5 秒、单路 0.8 秒（健康时三路是并行收尾的，
 * 实测全程 0.56 秒，留足余量）。超时就 detach、继续走，main 一返回进程即
 * 结束——至少把能收的收干净了，跟客户端硬杀不是一回事。
 *
 * 注意 join 循环同时就是主运行循环，所以正常运行时不能套这个截止时间，
 * 只能短超时轮询着等 g_stop（见 main 里那段说明）。 */
#define SHUTDOWN_JOIN_TOTAL_MS 1500
#define SHUTDOWN_JOIN_PER_CAMERA_MS 800
#define SHUTDOWN_JOIN_POLL_MS 200

/* 距 deadline 还有多少毫秒（≤0 表示已过期）。 */
static long ms_until(const struct timespec *deadline) {
    struct timespec now;
    clock_gettime(CLOCK_REALTIME, &now);
    return (long)(deadline->tv_sec - now.tv_sec) * 1000L +
           (long)(deadline->tv_nsec - now.tv_nsec) / 1000000L;
}

/* 现在起 ms 毫秒之后的时刻。pthread_timedjoin_np 按 CLOCK_REALTIME 算。 */
static struct timespec deadline_after_ms(long ms) {
    struct timespec t;
    clock_gettime(CLOCK_REALTIME, &t);
    t.tv_sec += ms / 1000;
    t.tv_nsec += (ms % 1000) * 1000000L;
    if (t.tv_nsec >= 1000000000L) {
        t.tv_sec += 1;
        t.tv_nsec -= 1000000000L;
    }
    return t;
}

int main(int argc, char **argv) {
    const char *config_path = NULL;
    int validate_only = 0;
    camera_config_t configs[MAX_CAMERAS];
    camera_state_t cameras[MAX_CAMERAS];
    size_t camera_count = 0;
    size_t started = 0;
    struct sigaction action;

    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--config") == 0 && i + 1 < argc) {
            config_path = argv[++i];
        } else if (strcmp(argv[i], "--validate-only") == 0) {
            validate_only = 1;
        } else if (strcmp(argv[i], "--parent-pid") == 0 &&
                   i + 1 < argc) {
            if (parse_int(argv[++i], &g_parent_pid) != 0 ||
                g_parent_pid <= 1) {
                fprintf(stderr, "invalid --parent-pid\n");
                return 2;
            }
        } else {
            print_usage(argv[0]);
            return 2;
        }
    }
    if (config_path == NULL || load_config(config_path, configs,
                                           &camera_count) != 0) {
        print_usage(argv[0]);
        return 2;
    }
    if (validate_only) {
        for (size_t i = 0; i < camera_count; ++i) {
            fprintf(stdout,
                    "valid[%zu] name=%s vidpid=%04x:%04x bus=%d port=%s "
                    "interface=%d alt=%d payload=%d mode=%dx%d MJPEG@%d "
                    "socket=%s\n",
                    i, configs[i].name, configs[i].vid, configs[i].pid,
                    configs[i].usb_bus, configs[i].usb_port_path,
                    configs[i].streaming_interface,
                    configs[i].forced_altsetting, configs[i].forced_payload,
                    configs[i].width, configs[i].height, configs[i].fps,
                    configs[i].socket_path);
        }
        return 0;
    }
    memset(&action, 0, sizeof(action));
    action.sa_handler = request_stop;
    sigemptyset(&action.sa_mask);
    sigaction(SIGINT, &action, NULL);
    sigaction(SIGTERM, &action, NULL);

    memset(cameras, 0, sizeof(cameras));
    for (size_t i = 0; i < camera_count; ++i) {
        cameras[i].config = configs[i];
        cameras[i].listen_fd = -1;
        cameras[i].client_fd = -1;
        pthread_mutex_init(&cameras[i].mutex, NULL);
        pthread_cond_init(&cameras[i].condition, NULL);
        if (pthread_create(&cameras[i].thread, NULL, camera_main,
                           &cameras[i]) != 0) {
            fprintf(stderr, "[%s] capture thread creation failed\n",
                    cameras[i].config.name);
            g_stop = 1;
            break;
        }
        started++;
    }
    /* 这个 join 循环**就是服务的主运行循环**——它在启动时进入、一直跑到
     * g_stop，不是「收尾时才走的一段」。所以截止时间不能在进循环前就算好
     * （那样正常运行时也会到点超时、服务自己退出），要等 g_stop 真置位时
     * 才开始计时。没置位时只用 200ms 短超时轮询，成本可忽略。 */
    int shutdown_started = 0;
    struct timespec shutdown_deadline = {0, 0};
    for (size_t i = 0; i < started; ++i) {
        int join_rc;
        for (;;) {
            long budget = SHUTDOWN_JOIN_POLL_MS;
            if (g_stop) {
                if (!shutdown_started) {
                    shutdown_deadline =
                        deadline_after_ms(SHUTDOWN_JOIN_TOTAL_MS);
                    shutdown_started = 1;
                }
                long left = ms_until(&shutdown_deadline);
                budget = left < SHUTDOWN_JOIN_PER_CAMERA_MS
                             ? left : SHUTDOWN_JOIN_PER_CAMERA_MS;
                /* 总预算已用尽：不再等，但还是要问一次——线程可能早就退了
                 * （健康的那几路是并行收尾的），0 超时的 join 会立刻返回。 */
                if (budget < 0) {
                    budget = 0;
                }
            }
            struct timespec limit = deadline_after_ms(budget);
            join_rc = pthread_timedjoin_np(cameras[i].thread, NULL, &limit);
            if (join_rc != ETIMEDOUT) {
                break;
            }
            if (!g_stop) {
                continue; /* 正常运行中的轮询到期，接着等 */
            }
            if (budget == 0) {
                break; /* 预算已尽，放弃这一路 */
            }
        }
        if (join_rc == ETIMEDOUT) {
            /* 线程没在预算内退出——多半还陷在设备调用里。不能为它把整个
             * 进程陪进去，否则客户端 2 秒宽限一到就是 SIGKILL、设备留脏。
             * detach 后 main 一返回进程即结束，内核会替我们释放设备句柄。
             * 跳过下面的统计与销毁：那些结构线程可能还在用，销毁它们是
             * use-after-free。 */
            fprintf(stderr,
                    "[%s] 收尾超时：线程未在预算内退出（多半卡在设备调用里），"
                    "不再等它\n", cameras[i].config.name);
            pthread_detach(cameras[i].thread);
            continue;
        }
        pthread_mutex_lock(&cameras[i].mutex);
        fprintf(stderr, "[%s] received=%llu output=%llu dropped=%llu "
                "bad_jpeg=%llu bad_replaced=%llu write_errors=%llu "
                "retries=%llu stalls=%llu usb_resets=%u\n",
                cameras[i].config.name,
                (unsigned long long)cameras[i].frames_received,
                (unsigned long long)cameras[i].frames_output,
                (unsigned long long)cameras[i].frames_dropped,
                (unsigned long long)cameras[i].bad_jpeg_frames,
                (unsigned long long)cameras[i].bad_jpeg_replaced,
                (unsigned long long)cameras[i].write_errors,
                (unsigned long long)cameras[i].reconnects,
                (unsigned long long)cameras[i].stalls,
                cameras[i].usb_resets);
        pthread_mutex_unlock(&cameras[i].mutex);
        pthread_cond_destroy(&cameras[i].condition);
        pthread_mutex_destroy(&cameras[i].mutex);
    }
    return started == camera_count ? 0 : 1;
}
