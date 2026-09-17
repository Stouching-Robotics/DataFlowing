/**
 * @file fayssense_orb_slam.cc
 * @brief S80M 回调模式 → ORB-SLAM3 (单进程, SDK 内部同步 IMU+图像)
 *
 * 注册 FaysSense SDK 回调获取数据, 直接喂 ORB-SLAM3。
 * 回调收集 IMU → 图像到达时 Track → 清空 IMU。
 */

// ############################################################################
// 本文件是**生产桥接二进制的构建源**。改这里才会进二进制。
//
// online/dist/fays_opencv48/CMakeLists.txt:62 的 FAYS_BRIDGE_SOURCE 硬指向
// ${KSQ_ROOT}/ORB-SLAM/Examples/fays/fayssense_orb_slam.cc，KSQ_ROOT 相对该
// CMakeLists 自身位置解析 → 就是本文件。所有 sn* 目标（含生产用的 sn219
// mark_only）都从这一份编译。
//
// 构建/部署配方见 online/杂项日志 与 memory「SLAM 崩溃根因」；要点：
//   -DKSQ_FAYS_BINARY_SUFFIX=_mark_only -DKSQ_ORB_LIBRARY_PATH=...orb_mark_only/lib/libORB_SLAM3.so
//   -DCMAKE_EXE_LINKER_FLAGS="-Wl,-rpath-link,<orb48_env>/lib -Wl,-rpath-link,<conda opencv4.8>/lib"
// RUNTIME_OUTPUT_DIRECTORY 指向 online/dist/fays_opencv48/bin/，同目录另有
// core/gripper/native/ORB-SLAM/... 一份**陈旧副本**（不是构建源，别改那份）。
// 两棵树都被 .gitignore，源码无版本控制兜底。
// ############################################################################
#include "fays_sdk_shutdown.h"
#include "TimeRegressionGuard.h"

#include <signal.h>
#include <sched.h>
#include <execinfo.h>
#include <unistd.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <poll.h>
#include <iostream>
#include <fstream>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <atomic>
#include <algorithm>
#include <vector>
#include <deque>
#include <chrono>
#include <iomanip>
#include <cmath>
#include <cstdlib>
#include <cerrno>
#include <cctype>
#include <cstring>
#include <functional>
#include <pthread.h>
#include <exception>
#include <system_error>
#include <dirent.h>
#include <malloc.h>

#include <opencv2/core.hpp>
#include <opencv2/highgui.hpp>
#include <opencv2/imgproc.hpp>
#include <opencv2/calib3d.hpp>
#include <fcntl.h>
#include <sys/stat.h>
#include <sys/ioctl.h>
#include <linux/videodev2.h>
#include <sstream>

#include "fays_atrak/fays_atrak_types.h"
#include "fays_atrak/fays_vikit.h"
#include "common/print_helpers.h"

#include <System.h>
#include <ImuTypes.h>
#include <MapPointCleanup.h>
#include <KeyFrameCleanup.h>
#include <RuntimeMapDiagnostics.h>

// ============================================================================
static std::atomic<bool> g_running{true};
static void*     g_handle = nullptr;
static std::string g_sdk_config;
static_assert(ATOMIC_BOOL_LOCK_FREE == 2, "signal stop flag must be lock-free");
static bool g_fake_imu = false;
static constexpr double kFakeImuStepS = 0.001;
static constexpr double kFakeImuExcitationS = 3.0;
// SDK definition: timeshift_cam_imu = t_imu - t_cam.  Real IMU samples are
// moved into the camera clock domain by subtracting this signed value.
static double g_cam_imu_timeshift_s = 0.0;
static constexpr double kMpCleanupFirstTriggerS = 240.0;
static constexpr double kMpCleanupIntervalS = 240.0;
static constexpr double kMpCleanupGraceS = 30.0;
// Longer than the MapPoint grace on purpose: a stale KeyFrame* is more likely
// than a stale MapPoint* to still be in flight (LocalMapping and LoopClosing
// both keep covisibility snapshots and loop-candidate queues), and the payload
// being released -- the feature grid -- is the one member a bad keyframe's
// in-flight readers would still touch.
static constexpr double kKfCleanupGraceS = 120.0;
static constexpr unsigned long long kInactiveCleanupHighWater = 1000;
static constexpr unsigned long long kInactiveCleanupBatch = 1000;
static constexpr double kInactiveCleanupRetryS = 1.0;
// A gripper cannot physically cross these per-sample envelopes.  ORB-SLAM3
// may nevertheless change its world frame after VIBA/loop correction; that
// coordinate-frame jump is rebased below instead of being published as
// motion.  The same limits are independently enforced at the Python boundary.
static constexpr float kPoseStepAllowanceM = 0.05f;
static constexpr float kPoseMaxLinearSpeedMps = 3.0f;
static constexpr double kPoseMaxGuardIntervalS = 0.1;
static constexpr float kPoseAngleAllowanceRad =
    20.0f * static_cast<float>(M_PI) / 180.0f;
static constexpr float kPoseMaxAngularSpeedRadS =
    1080.0f * static_cast<float>(M_PI) / 180.0f;
static double g_fake_imu_started_ts = -1.0;
static double g_fake_imu_last_ts = -1.0;

static void printNativeBacktrace(const char* scope) noexcept {
    void* frames[64];
    const int count = backtrace(frames, 64);
    std::cerr << "[NATIVE_BACKTRACE] scope=" << scope
              << " frames=" << count << "\n";
    if (count > 0) {
        backtrace_symbols_fd(frames, count, STDERR_FILENO);
    }
}

static void reportNativeException(
        const char* scope, const std::exception_ptr& error) noexcept {
    if (!error) {
        std::cerr << "[NATIVE_EXCEPTION] scope=" << scope
                  << " type=none\n";
        return;
    }
    try {
        std::rethrow_exception(error);
    } catch (const std::system_error& exception) {
        std::cerr << "[NATIVE_EXCEPTION] scope=" << scope
                  << " type=system_error"
                  << " code=" << exception.code().value()
                  << " category=" << exception.code().category().name()
                  << " message=" << exception.what() << "\n";
    } catch (const std::exception& exception) {
        std::cerr << "[NATIVE_EXCEPTION] scope=" << scope
                  << " type=exception"
                  << " message=" << exception.what() << "\n";
    } catch (...) {
        std::cerr << "[NATIVE_EXCEPTION] scope=" << scope
                  << " type=unknown\n";
    }
}

[[noreturn]] static void terminateWithDiagnostics() noexcept {
    reportNativeException("terminate", std::current_exception());
    printNativeBacktrace("terminate");
    std::abort();
}

struct IpcPaths {
    std::string directory;
    std::string current_frame_tmp;
    std::string current_frame;
    std::string meta_tmp;
    std::string meta;
    std::string raw_stream_socket;
    std::string camera_yaml;
    std::string imu_yaml;
};

static IpcPaths g_ipc_paths;

static bool configureIpcPaths() {
    const char* configured = std::getenv("KSQ_FAYS_IPC_DIR");
    std::string directory =
        (configured && configured[0] != '\0') ? configured : "/dev/shm";
    while (directory.size() > 1 && directory.back() == '/') {
        directory.pop_back();
    }
    struct stat status {};
    if (directory.empty() || directory.front() != '/' ||
        stat(directory.c_str(), &status) != 0 || !S_ISDIR(status.st_mode)) {
        std::cerr << "[ERR] Invalid KSQ_FAYS_IPC_DIR: "
                  << directory << "\n";
        return false;
    }
    auto path = [&directory](const char* leaf) {
        return directory + "/" + leaf;
    };
    g_ipc_paths = {
        directory,
        path("orb_current_frame_tmp.jpg"),
        path("orb_current_frame.jpg"),
        path("orb_meta_tmp.json"),
        path("orb_meta.json"),
        path("orb_raw_stream.sock"),
        path("camera_calibration.yaml"),
        path("imu.yaml"),
    };
    std::cout << "[FAYS-IPC] dir=" << g_ipc_paths.directory << "\n";
    return true;
}

// ── 每个 GUI 实例独立目录中的原子写入 ──
static void write_atomic(const char* tmp_path, const char* final_path,
                         const void* data, size_t len) {
    FILE* f = fopen(tmp_path, "wb");
    if (!f) return;
    fwrite(data, 1, len, f);
    fclose(f);
    rename(tmp_path, final_path);
}

// ---------------------------------------------------------------------------
// Raw side-channel for LeRobot v3.  It is independent of ORB-SLAM3: a client
// connecting to this socket starts one recording; disconnecting stops it.
// ---------------------------------------------------------------------------
static constexpr uint32_t kRawStreamMagic = 0x53544F55;
static constexpr uint32_t kRawStreamVersion = 1;
static constexpr uint32_t kRawPacketStereo = 1;
static constexpr uint32_t kRawPacketImu = 2;

struct RawStreamHeader {
    uint32_t magic;
    uint32_t version;
    uint32_t kind;
    uint32_t header_size;
    uint32_t payload_size;
    uint32_t reserved0;
    uint64_t sensor_timestamp_ns;
    uint64_t host_monotonic_ns;
    uint64_t host_realtime_ns;
    int32_t sequence;
    int32_t width;
    int32_t height;
    int32_t channels;
    int16_t encoding;
    int16_t reserved1;
    int32_t step;
    uint32_t reserved2;
    uint32_t reserved3;
};
static_assert(sizeof(RawStreamHeader) == 80, "raw header layout changed");

struct RawImuPayload {
    double ax;
    double ay;
    double az;
    double gx;
    double gy;
    double gz;
};
static_assert(sizeof(RawImuPayload) == 48, "raw IMU payload layout changed");

#include "fays_debug_capture.h"
static FaysDebugCapture g_debug_capture;

struct RawPacket {
    RawStreamHeader header{};
    std::vector<unsigned char> payload;
};

static std::mutex g_raw_stereo_mutex;
static std::condition_variable g_raw_stereo_cv;
static std::deque<RawPacket> g_raw_stereo;
static constexpr size_t kRawStereoQueueCapacity = 16;
static uint64_t g_raw_stereo_dropped = 0;

static std::mutex g_raw_imu_mutex;
static std::condition_variable g_raw_imu_cv;
static std::deque<RawPacket> g_raw_imu;
static constexpr size_t kRawImuQueueCapacity = 4096;
static std::atomic<uint64_t> g_raw_imu_source_sequence{0};
static uint64_t g_raw_imu_dropped = 0;
static std::atomic<bool> g_raw_capture_active{false};

static uint64_t rawClockNs(clockid_t clock_id) {
    timespec value{};
    if (clock_gettime(clock_id, &value) != 0) return 0;
    return static_cast<uint64_t>(value.tv_sec) * 1000000000ULL
        + static_cast<uint64_t>(value.tv_nsec);
}

static void pushRawPacket(
        std::deque<RawPacket>& packets,
        size_t capacity,
        uint64_t* dropped,
        std::mutex& mutex,
        std::condition_variable& condition,
        RawPacket&& packet) {
    {
        std::lock_guard<std::mutex> lock(mutex);
        if (!g_raw_capture_active.load(std::memory_order_relaxed)) return;
        if (packets.size() >= capacity) {
            ++*dropped;
            return;
        }
        packets.emplace_back(std::move(packet));
    }
    condition.notify_one();
}

static void pushRawStereo(
        const cv::Mat& raw, const AtrakImage& image) {
    // 与 SLAM 预处理相同的 3-取-5 抽帧：50fps 输入 → 30fps raw 流，
    // 保证录制的 fays_stereo 帧与 slam_pose 逐帧对齐。
    static std::atomic<unsigned long long> raw_selection_index{0};
    const unsigned long long raw_index =
        raw_selection_index.fetch_add(1, std::memory_order_relaxed);
    if ((raw_index % 5) >= 3) return;

    const uint64_t timestamp = static_cast<uint64_t>(image.timestamp);
    RawPacket packet;
    packet.header.magic = kRawStreamMagic;
    packet.header.version = kRawStreamVersion;
    packet.header.header_size = sizeof(RawStreamHeader);
    packet.header.kind = kRawPacketStereo;
    packet.header.payload_size = static_cast<uint32_t>(
        raw.total() * raw.elemSize());
    packet.header.sensor_timestamp_ns = timestamp;
    packet.header.host_monotonic_ns = rawClockNs(CLOCK_MONOTONIC);
    packet.header.host_realtime_ns = rawClockNs(CLOCK_REALTIME);
    packet.header.sequence = image.seq;
    packet.header.width = raw.cols;
    packet.header.height = raw.rows;
    packet.header.channels = raw.channels();
    packet.header.encoding = static_cast<int16_t>(image.encoding);
    packet.header.step = static_cast<int32_t>(
        raw.step > 0 ? raw.step : raw.cols * raw.elemSize());
    if (packet.header.payload_size == 0 || packet.header.step <= 0) return;
    packet.payload.resize(packet.header.payload_size);
    std::memcpy(
        packet.payload.data(), raw.data, packet.header.payload_size);
    pushRawPacket(
        g_raw_stereo, kRawStereoQueueCapacity, &g_raw_stereo_dropped,
        g_raw_stereo_mutex, g_raw_stereo_cv, std::move(packet));
}

static void pushRawImu(const AtrakIMU& imu) {
    const uint64_t timestamp = static_cast<uint64_t>(imu.timestamp);
    const uint64_t source_sequence = g_raw_imu_source_sequence.fetch_add(
        1, std::memory_order_relaxed) + 1;
    RawPacket packet;
    RawImuPayload payload{
        imu.acc[0], imu.acc[1], imu.acc[2],
        imu.gyro[0], imu.gyro[1], imu.gyro[2]};
    packet.header.magic = kRawStreamMagic;
    packet.header.version = kRawStreamVersion;
    packet.header.header_size = sizeof(RawStreamHeader);
    packet.header.kind = kRawPacketImu;
    packet.header.payload_size = sizeof(payload);
    packet.header.sensor_timestamp_ns = timestamp;
    packet.header.host_monotonic_ns = rawClockNs(CLOCK_MONOTONIC);
    packet.header.host_realtime_ns = rawClockNs(CLOCK_REALTIME);
    packet.header.sequence = static_cast<int32_t>(source_sequence);
    packet.payload.resize(sizeof(payload));
    std::memcpy(packet.payload.data(), &payload, sizeof(payload));
    pushRawPacket(
        g_raw_imu, kRawImuQueueCapacity, &g_raw_imu_dropped,
        g_raw_imu_mutex, g_raw_imu_cv, std::move(packet));
}

static bool sendAll(int fd, const void* data, size_t size) {
    const unsigned char* cursor = static_cast<const unsigned char*>(data);
    while (size > 0) {
        const ssize_t written = ::send(
            fd, cursor, size, MSG_NOSIGNAL);
        if (written <= 0) {
            if (written < 0 && (errno == EINTR || errno == EAGAIN)) continue;
            return false;
        }
        cursor += written;
        size -= static_cast<size_t>(written);
    }
    return true;
}

static bool sendRawPacket(int fd, const RawPacket& packet) {
    if (!sendAll(fd, &packet.header, sizeof(packet.header))) return false;
    if (packet.header.payload_size == 0 || packet.payload.empty()) return true;
    return sendAll(fd, packet.payload.data(), packet.payload.size());
}

static bool drainRawPacketQueue(
        std::deque<RawPacket>& packets,
        std::mutex& mutex,
        std::condition_variable& condition,
        const std::function<bool(const RawPacket&)>& send) {
    std::deque<RawPacket> batch;
    {
        std::unique_lock<std::mutex> lock(mutex);
        condition.wait_for(lock, std::chrono::milliseconds(20), [&] {
            return !g_running || !packets.empty();
        });
        batch.swap(packets);
    }
    for (const RawPacket& packet : batch) {
        if (!send(packet)) return false;
    }
    return true;
}

static void clearRawStreamQueues() {
    {
        std::lock_guard<std::mutex> lock(g_raw_stereo_mutex);
        g_raw_stereo.clear();
        g_raw_stereo_dropped = 0;
    }
    g_raw_stereo_cv.notify_all();
    {
        std::lock_guard<std::mutex> lock(g_raw_imu_mutex);
        g_raw_imu.clear();
        g_raw_imu_dropped = 0;
    }
    g_raw_imu_cv.notify_all();
}

static void clearRawStereoQueue() {
    {
        std::lock_guard<std::mutex> lock(g_raw_stereo_mutex);
        g_raw_stereo.clear();
        g_raw_stereo_dropped = 0;
    }
    g_raw_stereo_cv.notify_all();
}

static void rawStreamServer() {
    unlink(g_ipc_paths.raw_stream_socket.c_str());
    const int listener = ::socket(AF_UNIX, SOCK_STREAM, 0);
    if (listener < 0) {
        std::cerr << "[RAW-STREAM] socket failed: "
                  << std::strerror(errno) << "\n";
        return;
    }
    sockaddr_un address{};
    address.sun_family = AF_UNIX;
    if (g_ipc_paths.raw_stream_socket.size()
            >= sizeof(address.sun_path)) {
        std::cerr << "[RAW-STREAM] IPC path is too long\n";
        ::close(listener);
        return;
    }
    std::strncpy(
        address.sun_path, g_ipc_paths.raw_stream_socket.c_str(),
        sizeof(address.sun_path) - 1);
    if (::bind(
            listener,
            reinterpret_cast<const sockaddr*>(&address),
            sizeof(address)) != 0 || ::listen(listener, 1) != 0) {
        std::cerr << "[RAW-STREAM] bind/listen failed: "
                  << std::strerror(errno) << "\n";
        ::close(listener);
        return;
    }
    std::cout << "[RAW-STREAM] socket="
              << g_ipc_paths.raw_stream_socket << "\n";
    g_raw_capture_active.store(true, std::memory_order_release);
    while (g_running) {
        pollfd poller{listener, POLLIN, 0};
        const int ready = ::poll(&poller, 1, 100);
        if (ready <= 0) continue;
        if (!(poller.revents & POLLIN)) continue;
        const int client = ::accept(listener, nullptr, nullptr);
        if (client < 0) {
            if (errno == EINTR || errno == EAGAIN) continue;
            break;
        }
        clearRawStreamQueues();
        {
            // Keep a short IMU pre-roll before the first stereo frame.  The
            // calibration can shift IMU timestamps slightly behind camera.
            std::unique_lock<std::mutex> lock(g_raw_imu_mutex);
            const bool preroll = g_raw_imu_cv.wait_for(
                lock, std::chrono::milliseconds(200), [&] {
                    // Full-rate IMU is about 1 kHz; 20 samples prove the
                    // callback chain is alive without a long startup delay.
                    return !g_running || g_raw_imu.size() >= 20;
                });
            if (!preroll || (!g_running && g_raw_imu.size() < 20)) {
                std::cerr << "[RAW-STREAM] IMU pre-roll failed\n";
                ::close(client);
                clearRawStreamQueues();
                continue;
            }
        }
        clearRawStereoQueue();
        std::cout << "[RAW-STREAM] client connected\n";
        uint64_t dropped = 0;
        bool healthy = true;
        while (healthy && g_running) {
            healthy = drainRawPacketQueue(
                g_raw_imu, g_raw_imu_mutex, g_raw_imu_cv,
                [&client](const RawPacket& packet) {
                    return sendRawPacket(client, packet);
                });
            if (!healthy) break;
            healthy = drainRawPacketQueue(
                g_raw_stereo, g_raw_stereo_mutex, g_raw_stereo_cv,
                [&client](const RawPacket& packet) {
                    return sendRawPacket(client, packet);
                });
            {
                std::lock_guard<std::mutex> lock(g_raw_stereo_mutex);
                dropped = g_raw_stereo_dropped;
            }
            if (dropped != 0) {
                std::cerr << "[RAW-STREAM] stereo loss detected: "
                          << dropped << "\n";
                healthy = false;
            }
        }
        ::close(client);
        std::cout << "[RAW-STREAM] client disconnected\n";
        clearRawStreamQueues();
    }
    g_raw_capture_active.store(false, std::memory_order_release);
    ::close(listener);
    unlink(g_ipc_paths.raw_stream_socket.c_str());
}

static void do_cleanup() {
    if (!g_handle) { g_debug_capture.stop(); return; }
    void* handle = g_handle;
    g_handle = nullptr;
    ksq_fays::stopOwnedImuStream(g_sdk_config);
    const int result = FAYS_VIK_DestroyHandle(handle);
    g_debug_capture.stop();
    std::cerr << "[FAYS-CLEANUP] sdk_destroyed result=" << result << "\n";
}

static unsigned long long currentRssKb() {
    std::ifstream status("/proc/self/status");
    std::string key;
    while (status >> key) {
        if (key == "VmRSS:") {
            unsigned long long value = 0;
            status >> value;
            return value;
        }
        std::string rest;
        std::getline(status, rest);
    }
    return 0;
}

// glibc-only breakdown of where the heap actually went.  VmRSS alone cannot
// separate "live objects nobody ever freed" from "free chunks the allocator is
// still holding instead of returning to the kernel", and those two need
// opposite fixes: the first is an object-lifetime bug, the second is
// fragmentation.  in_use is the live total; arena minus in_use is what glibc is
// sitting on.  If in_use grows linearly the leak is real objects; if in_use is
// flat while arena grows, nothing is actually leaking and the cure is allocator
// tuning, not more cleanup passes.
struct HeapBreakdown {
    unsigned long long inUseKb = 0;
    unsigned long long arenaKb = 0;
    unsigned long long mmapKb = 0;
    unsigned long long freeKb = 0;
};

static HeapBreakdown currentHeapBreakdown() {
    HeapBreakdown breakdown;
#if defined(__GLIBC__) && defined(__GLIBC_PREREQ)
#if __GLIBC_PREREQ(2, 33)
    const struct mallinfo2 info = mallinfo2();
    breakdown.inUseKb = static_cast<unsigned long long>(info.uordblks) / 1024ULL;
    breakdown.arenaKb = static_cast<unsigned long long>(info.arena) / 1024ULL;
    breakdown.mmapKb = static_cast<unsigned long long>(info.hblkhd) / 1024ULL;
    breakdown.freeKb = static_cast<unsigned long long>(info.fordblks) / 1024ULL;
#endif
#endif
    return breakdown;
}

void sigint_handler(int) {
    const char msg[] = "\n[SLAM] Exit.\n"; write(STDERR_FILENO, msg, sizeof(msg)-1);
    // Never lock/join/close SDK resources from a signal handler. SIGTERM can
    // interrupt an ORB/SDK thread while it holds exactly those locks.
    g_running.store(false, std::memory_order_relaxed);
}

// ============================================================================
// Fatal-signal diagnostics.
//
// Until now only SIGINT/SIGTERM had handlers, so a SIGABRT/SIGSEGV left nothing
// but the kernel's one-line report (`fays-track[..]: segfault at 54 ip ..`).
// The 2026-09-11 futex abort -- SIGABRT raised by glibc's __libc_fatal after
// futex_wait returned EFAULT -- had to be diagnosed from that single `ip`,
// because the process died with no C++ stack at all.
//
// Everything below is async-signal-safe on purpose.  The crashing thread may be
// inside malloc, inside iostreams, or holding any lock in ORB-SLAM3, so this
// handler may only use write(), backtrace() and backtrace_symbols_fd() -- the
// latter two allocate nothing (backtrace_symbols_fd writes straight from the
// loaded ELF symbol tables; backtrace's own lazy libgcc load is warmed up by
// installFatalSignalHandlers before any handler can run).
// ============================================================================
static char* appendLiteral(char* out, const char* text) {
    while (*text) *out++ = *text++;
    return out;
}

static char* appendDecimal(char* out, long long value) {
    if (value < 0) { *out++ = '-'; value = -value; }
    char digits[24];
    int count = 0;
    do { digits[count++] = static_cast<char>('0' + value % 10); value /= 10; } while (value);
    while (count > 0) *out++ = digits[--count];
    return out;
}

static char* appendHex(char* out, unsigned long long value) {
    out = appendLiteral(out, "0x");
    char digits[16];
    int count = 0;
    do { digits[count++] = "0123456789abcdef"[value & 0xf]; value >>= 4; } while (value);
    while (count > 0) *out++ = digits[--count];
    return out;
}

// 64 KB is plenty for the handler plus backtrace(); it is a fixed buffer rather
// than SIGSTKSZ because glibc >= 2.34 makes SIGSTKSZ a sysconf() call, which is
// not usable as a static array bound.
static char g_alt_signal_stack[64 * 1024];

static void fatalSignalHandler(int signo, siginfo_t* info, void*) {
    char line[192];
    char* out = line;
    out = appendLiteral(out, "\n[FATAL_SIGNAL] signo=");
    out = appendDecimal(out, signo);
    out = appendLiteral(out, " code=");
    out = appendDecimal(out, info ? info->si_code : 0);
    out = appendLiteral(out, " fault_addr=");
    out = appendHex(out, info ? reinterpret_cast<unsigned long long>(info->si_addr) : 0ULL);
    out = appendLiteral(out, "\n");
    const ssize_t written = write(STDERR_FILENO, line, static_cast<size_t>(out - line));
    (void)written;

    void* frames[64];
    const int count = backtrace(frames, 64);
    const char header[] = "[NATIVE_BACKTRACE] scope=fatal frames=";
    char headerBuffer[sizeof(header) + 8];
    char* headerEnd = appendLiteral(headerBuffer, header);
    headerEnd = appendDecimal(headerEnd, count);
    *headerEnd++ = '\n';
    const ssize_t headerWritten =
        write(STDERR_FILENO, headerBuffer, static_cast<size_t>(headerEnd - headerBuffer));
    (void)headerWritten;
    if (count > 0)
        backtrace_symbols_fd(frames, count, STDERR_FILENO);

    // Restore SIG_DFL and re-raise so the process still dies from the original
    // signal: the parent keeps seeing -6/-11 instead of a generic exit code,
    // and the kernel gets its chance to run the configured core pattern.
    struct sigaction defaultAction;
    memset(&defaultAction, 0, sizeof(defaultAction));
    defaultAction.sa_handler = SIG_DFL;
    sigemptyset(&defaultAction.sa_mask);
    sigaction(signo, &defaultAction, NULL);

    sigset_t unblock;
    sigemptyset(&unblock);
    sigaddset(&unblock, signo);
    // pthread_sigmask, not sigprocmask: the latter is unspecified in a
    // multithreaded process, and this crash may be on any ORB/SDK thread.
    pthread_sigmask(SIG_UNBLOCK, &unblock, NULL);
    raise(signo);
}

static void installFatalSignalHandlers() {
    // Warm up backtrace() here, at a point where allocating is still legal: its
    // first call dlopens libgcc to resolve the unwinder, and doing that from
    // inside the handler would deadlock if the crash happened in the loader.
    void* warmup[4];
    (void)backtrace(warmup, 4);

    stack_t altStack;
    memset(&altStack, 0, sizeof(altStack));
    altStack.ss_sp = g_alt_signal_stack;
    altStack.ss_size = sizeof(g_alt_signal_stack);
    // A SIGSEGV caused by stack exhaustion cannot run its handler on the
    // exhausted stack -- without an alternate stack that crash reports nothing
    // at all, which is the exact failure mode this handler exists to remove.
    if (sigaltstack(&altStack, NULL) == 0) {
        // Only meaningful if SA_ONSTACK below actually took effect.
    } else {
        // "rejected", not "failed": these two lines run at startup, and
        // protocol._ERROR_RE turns any "failed" into a state.error event --
        // which is exactly how the ld.so LD_PRELOAD message locked the
        // wait_sdk_ready gate on 2026-09-10.  Losing SIGALTSTACK degrades a
        // crash report; it must not also block startup.
        std::cerr << "[FATAL_SIGNAL] sigaltstack rejected errno=" << errno << "\n";
    }

    const int fatalSignals[] = {SIGABRT, SIGSEGV, SIGBUS, SIGILL, SIGFPE};
    for (int signo : fatalSignals) {
        struct sigaction action;
        memset(&action, 0, sizeof(action));
        action.sa_sigaction = fatalSignalHandler;
        action.sa_flags = SA_SIGINFO | SA_ONSTACK;
        sigemptyset(&action.sa_mask);
        if (sigaction(signo, &action, NULL) != 0)
            std::cerr << "[FATAL_SIGNAL] sigaction rejected signo=" << signo
                      << " errno=" << errno << "\n";
    }
    std::cerr << "[FATAL_SIGNAL] handlers installed signo=ABRT,SEGV,BUS,ILL,FPE\n";
}

// ============================================================================
// IMU 原始样本缓冲区 + 双目原始帧有界 FIFO。
// SDK 回调只负责校验、计数和必要的数据所有权复制；双目拆分、灰度化、
// 鱼眼校正和IMU格式转换放到CPU5预处理阶段。
// ============================================================================
static std::mutex g_imu_mutex;
static std::vector<AtrakIMU> g_imu_buf;

struct RawImageFrame {
    double timestamp = 0.0;
    // SDK 帧序号，仅用于诊断日志：区分「重复投递同一帧」与「新帧配旧时间戳」
    // 这两种都会表现为时间戳回退，但成因完全不同。
    int seq = 0;
    cv::Mat image;
    // 该取样帧进入本进程时的宿主单调钟纳秒（CLOCK_MONOTONIC）。与主程序
    // 侧 hardware_ns / time.monotonic_ns() 同一时基，是 slam 点与视频帧
    // 按时间对齐的唯一依据。timestamp 是相机传感器钟，与宿主钟不同源，
    // 两者之间只差一个近似常量但带抖动的偏移，无法互相换算。
    uint64_t host_mono_ns = 0;
};

static std::mutex g_img_mutex;
static std::condition_variable g_img_cv;
static std::deque<RawImageFrame> g_img_queue;
static constexpr size_t kImageQueueCapacity = 8;
static std::atomic<unsigned long long> g_stereo_enqueued_count{0};
static std::atomic<unsigned long long> g_stereo_dropped_count{0};
static std::atomic<unsigned long long> g_stereo_frameselect_dropped_count{0};
// 时间戳回退被丢弃的帧数（见 preprocessWorker 的单调性守卫）。
static std::atomic<unsigned long long> g_time_drop_count{0};
// 输入速率计数：双目回调对应 /dev/video0，IMU 回调对应 /dev/video2。
// 两者只累计 SDK 已成功解码并交给应用层的数据包。
static std::atomic<unsigned long long> g_stereo_input_count{0};
static std::atomic<unsigned long long> g_imu_input_count{0};

// 图像输入与完整输出分别维护自己的事件窗口，避免CPU4回调和CPU8输出线程
// 因共享速率锁产生无意义竞争。FPS严格使用 (N-1)/(末事件-首事件)，快照
// 时刻只负责裁掉一秒前的事件，不参与频率分母。
struct SlidingRateWindow {
    std::mutex mutex;
    std::deque<long long> events_ns;
};
static SlidingRateWindow g_stereo_rate_window;
static SlidingRateWindow g_process_rate_window;
static constexpr long long kRateWindowNs = 1000000000LL;

static long long rateEventNs(std::chrono::steady_clock::time_point now) {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        now.time_since_epoch()).count();
}

static void pruneRateEvents(
        std::deque<long long>& events, long long now_ns) {
    const long long cutoff = now_ns - kRateWindowNs;
    while (!events.empty() && events.front() < cutoff) events.pop_front();
}

static void recordRateEvent(
        SlidingRateWindow& window,
        std::chrono::steady_clock::time_point now =
            std::chrono::steady_clock::now()) {
    const long long now_ns = rateEventNs(now);
    std::lock_guard<std::mutex> lk(window.mutex);
    window.events_ns.push_back(now_ns);
    pruneRateEvents(window.events_ns, now_ns);
}

static double slidingRate(
        SlidingRateWindow& window,
        std::chrono::steady_clock::time_point now) {
    const long long now_ns = rateEventNs(now);
    std::lock_guard<std::mutex> lk(window.mutex);
    pruneRateEvents(window.events_ns, now_ns);
    if (window.events_ns.size() < 2) return 0.0;
    const long long span_ns =
        window.events_ns.back() - window.events_ns.front();
    if (span_ns <= 0) return 0.0;
    return static_cast<double>(window.events_ns.size() - 1)
        * 1e9 / static_cast<double>(span_ns);
}

// 矫正映射 (预计算)
static cv::Mat g_map1x, g_map1y, g_map2x, g_map2y;
// 调试: 记录第一帧图像信息
static bool g_first_img = true;
static int g_img_w = 0, g_img_h = 0, g_img_ch = 0;

static std::string boundedString(const char* value, size_t capacity) {
    if (!value || capacity == 0) return std::string();
    const char* end = std::find(value, value + capacity, '\0');
    return std::string(value, end);
}

struct RuntimeCalibration {
    std::string serial;
    std::string model;
    std::string camera_source;
    int width = 0;
    int height = 0;
    double fx = 0.0;
    double fy = 0.0;
    double cx = 0.0;
    double cy = 0.0;
    double baseline = 0.0;
    double timeshift_cam_imu = 0.0;
    double noise_acc = 0.0;
    double walk_acc = 0.0;
    double noise_gyro = 0.0;
    double walk_gyro = 0.0;
    double imu_hz = 0.0;
    cv::Mat T_b_c1;
};

struct DumpCameraCalibration {
    bool seen = false;
    bool pinhole = false;
    bool equidistant = false;
    std::vector<double> intrinsics;
    std::vector<double> distortion;
    std::vector<double> resolution;
    double timeshift = 0.0;
    bool has_timeshift = false;
    cv::Mat T_cam_imu;
    cv::Mat T_cn_cnm1;
};

static void writeYamlMatrix(
        std::ostream& output, const cv::Mat& matrix) {
    output << std::scientific << std::setprecision(15);
    for (int row = 0; row < matrix.rows; ++row) {
        output << "  - [";
        for (int column = 0; column < matrix.cols; ++column) {
            if (column) output << ", ";
            output << matrix.at<double>(row, column);
        }
        output << "]\n";
    }
    output.unsetf(std::ios::scientific);
}

static bool writeSplitCalibrationYaml(
        const RuntimeCalibration& runtime,
        const DumpCameraCalibration& left,
        const DumpCameraCalibration& right) {
    auto writeDoubleList = [](std::ostream& output,
                              const std::vector<double>& values) {
        output << "[";
        for (size_t index = 0; index < values.size(); ++index) {
            if (index) output << ", ";
            output << std::scientific << std::setprecision(15)
                   << values[index];
        }
        output << "]";
        output.unsetf(std::ios::scientific);
    };
    auto writeCameraBlock = [writeDoubleList](
            std::ostream& output, const DumpCameraCalibration& camera) {
        output << "  T_cam_imu:\n";
        writeYamlMatrix(output, camera.T_cam_imu);
        output << "  camera_model: "
               << (camera.pinhole ? "pinhole" : "unknown") << "\n"
               << "  distortion_coeffs: ";
        writeDoubleList(output, camera.distortion);
        output << "\n  distortion_model: "
               << (camera.equidistant ? "equidistant" : "unknown") << "\n"
               << "  intrinsics: ";
        writeDoubleList(output, camera.intrinsics);
        output << "\n  resolution: ["
               << static_cast<int>(camera.resolution[0]) << ", "
               << static_cast<int>(camera.resolution[1]) << "]\n"
               << "  timeshift_cam_imu: " << std::scientific
               << std::setprecision(15) << camera.timeshift << "\n";
        output.unsetf(std::ios::scientific);
    };
    try {
        {
            const std::string temporary = g_ipc_paths.camera_yaml + ".tmp";
            std::ofstream output(temporary, std::ios::trunc);
            if (!output) return false;
            output << "cam0:\n";
            writeCameraBlock(output, left);
            output << "cam1:\n  T_cam_imu:\n";
            writeYamlMatrix(output, right.T_cam_imu);
            output << "  T_cn_cnm1:\n";
            writeYamlMatrix(output, right.T_cn_cnm1);
            writeCameraBlock(output, right);
            output.flush();
            if (!output || ::rename(
                    temporary.c_str(), g_ipc_paths.camera_yaml.c_str()) != 0) {
                return false;
            }
        }
        {
            const std::string temporary = g_ipc_paths.imu_yaml + ".tmp";
            std::ofstream output(temporary, std::ios::trunc);
            if (!output) return false;
            output << "%YAML:1.0\n"
                   << "# Generated from the live FaysSense SDK calibration\n"
                   << "device:\n"
                   << "  model: " << runtime.model << "\n"
                   << "  serial_number: " << runtime.serial << "\n"
                   << "  camera_count: 2\n"
                   << "source:\n"
                   << "  api: FAYS_VIK_GetCalibrationParam\n"
                   << "  camera_source: " << runtime.camera_source << "\n"
                   << "imu:\n"
                   << "  accelerometer_noise_density: " << std::scientific
                   << std::setprecision(15) << runtime.noise_acc << "\n"
                   << "  accelerometer_random_walk: " << runtime.walk_acc << "\n"
                   << "  gyroscope_noise_density: " << runtime.noise_gyro << "\n"
                   << "  gyroscope_random_walk: " << runtime.walk_gyro << "\n"
                   << "  update_rate_hz: " << runtime.imu_hz << "\n";
            output.unsetf(std::ios::scientific);
            output.flush();
            if (!output || ::rename(
                    temporary.c_str(), g_ipc_paths.imu_yaml.c_str()) != 0) {
                return false;
            }
        }
    } catch (const std::exception&) {
        return false;
    }
    return true;
}

static std::string trim(const std::string& value) {
    const size_t first = value.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) return std::string();
    const size_t last = value.find_last_not_of(" \t\r\n");
    return value.substr(first, last - first + 1);
}

static bool endsWith(const std::string& value, const std::string& suffix) {
    return value.size() >= suffix.size() &&
        value.compare(value.size() - suffix.size(), suffix.size(), suffix) == 0;
}

static std::vector<double> bracketNumbers(const std::string& line) {
    const size_t left = line.find('[');
    const size_t right = line.rfind(']');
    std::vector<double> values;
    if (left == std::string::npos || right == std::string::npos ||
        right <= left) return values;
    std::string body = line.substr(left + 1, right - left - 1);
    std::replace(body.begin(), body.end(), ',', ' ');
    std::istringstream input(body);
    double value = 0.0;
    while (input >> value) values.push_back(value);
    return values;
}

static bool finiteVector(const std::vector<double>& values, size_t size) {
    if (values.size() != size) return false;
    for (double value : values) {
        if (!std::isfinite(value)) return false;
    }
    return true;
}

static bool parseDumpCalibration(
        const std::string& path, DumpCameraCalibration cameras[2]) {
    std::ifstream input(path);
    if (!input) {
        std::cerr << "[FAYS-CALIB] cannot open live SDK dump: " << path << "\n";
        return false;
    }
    int camera = -1;
    std::string matrix_name;
    std::vector<double> matrix_values;
    auto finish_matrix = [&]() -> bool {
        if (matrix_name.empty()) return true;
        if (camera < 0 || camera > 1 || matrix_values.size() != 16) return false;
        cv::Mat matrix(4, 4, CV_64F);
        for (int i = 0; i < 16; ++i) matrix.at<double>(i / 4, i % 4) = matrix_values[i];
        if (matrix_name == "T_cam_imu") cameras[camera].T_cam_imu = matrix;
        else if (matrix_name == "T_cn_cnm1") cameras[camera].T_cn_cnm1 = matrix;
        else return false;
        matrix_name.clear();
        matrix_values.clear();
        return true;
    };

    std::string raw;
    while (std::getline(input, raw)) {
        const std::string line = trim(raw);
        if (line.empty() || line[0] == '#') continue;
        if (line == "cam0:" || line == "cam1:") {
            if (!finish_matrix()) return false;
            camera = line == "cam0:" ? 0 : 1;
            cameras[camera].seen = true;
            continue;
        }
        if (camera < 0) continue;
        if (!matrix_name.empty() && !line.empty() && line[0] == '-') {
            const std::vector<double> row = bracketNumbers(line);
            if (row.size() != 4) return false;
            matrix_values.insert(matrix_values.end(), row.begin(), row.end());
            if (matrix_values.size() == 16 && !finish_matrix()) return false;
            continue;
        }
        if (!finish_matrix()) return false;
        if (line == "T_cam_imu:") matrix_name = "T_cam_imu";
        else if (line == "T_cn_cnm1:") matrix_name = "T_cn_cnm1";
        else if (line == "camera_model: pinhole") cameras[camera].pinhole = true;
        else if (line == "distortion_model: equidistant") cameras[camera].equidistant = true;
        else if (line.find("intrinsics:") == 0) cameras[camera].intrinsics = bracketNumbers(line);
        else if (line.find("distortion_coeffs:") == 0) cameras[camera].distortion = bracketNumbers(line);
        else if (line.find("resolution:") == 0) cameras[camera].resolution = bracketNumbers(line);
        else if (line.find("timeshift_cam_imu:") == 0) {
            std::istringstream value(line.substr(line.find(':') + 1));
            cameras[camera].has_timeshift = static_cast<bool>(value >> cameras[camera].timeshift);
        }
    }
    if (!finish_matrix()) return false;
    for (int index = 0; index < 2; ++index) {
        const DumpCameraCalibration& cam = cameras[index];
        if (!cam.seen || !cam.pinhole || !cam.equidistant ||
            !finiteVector(cam.intrinsics, 4) ||
            !finiteVector(cam.distortion, 4) ||
            !finiteVector(cam.resolution, 2) || !cam.has_timeshift ||
            cam.T_cam_imu.empty()) {
            std::cerr << "[FAYS-CALIB] incomplete cam" << index
                      << " in live SDK dump\n";
            return false;
        }
    }
    if (cameras[1].T_cn_cnm1.empty()) {
        std::cerr << "[FAYS-CALIB] live SDK dump lacks cam1 T_cn_cnm1\n";
        return false;
    }
    return true;
}

static std::string currentDirectory() {
    char path[4096];
    return getcwd(path, sizeof(path)) ? std::string(path) : std::string();
}

// Both live SDK dumps and the derived ORB YAML belong to this run-local
// container. Keep the per-process names intact for multi-gripper isolation.
static bool prepareFactoryCalibrationDirectory(
        const std::string& run_directory, std::string* directory) {
    if (run_directory.empty() || !directory) return false;
    *directory = run_directory + "/fays_factory_calib";
    if (mkdir(directory->c_str(), 0700) != 0 && errno != EEXIST) {
        std::cerr << "[FAYS-CALIB] cannot create calibration container: "
                  << *directory << " error=" << std::strerror(errno) << "\n";
        return false;
    }
    struct stat info{};
    if (stat(directory->c_str(), &info) != 0 || !S_ISDIR(info.st_mode)) {
        std::cerr << "[FAYS-CALIB] calibration container is not a directory: "
                  << *directory << "\n";
        return false;
    }
    return true;
}

static std::string findLiveDump(
        const std::string& directory, const std::string& serial) {
    DIR* dir = opendir(directory.c_str());
    if (!dir) return std::string();
    std::string result;
    while (dirent* entry = readdir(dir)) {
        const std::string name(entry->d_name);
        if (name.find(serial) != std::string::npos &&
            endsWith(name, "_dump_calib.yaml")) {
            if (!result.empty()) {
                result.clear();
                break;
            }
            result = directory + "/" + name;
        }
    }
    closedir(dir);
    return result;
}

static bool finalizeRuntimeCalibration(
        const cv::Mat& K1, const cv::Mat& D1,
        const cv::Mat& K2, const cv::Mat& D2,
        const cv::Mat& R, const cv::Mat& T,
        int width, int height, const cv::Mat& T_cam_imu,
        RuntimeCalibration* runtime) {
    if (!runtime || width <= 0 || height <= 0 || T_cam_imu.rows != 4 ||
        T_cam_imu.cols != 4 || std::abs(runtime->timeshift_cam_imu) > 0.1) {
        std::cerr << "[FAYS-CALIB] invalid live camera calibration dimensions\n";
        return false;
    }
    try {
        const cv::Size image_size(width, height);
        cv::Mat R1, R2, P1, P2, Q;
        cv::fisheye::stereoRectify(
            K1, D1, K2, D2, image_size, R, T,
            R1, R2, P1, P2, Q, cv::CALIB_ZERO_DISPARITY);
        cv::fisheye::initUndistortRectifyMap(
            K1, D1, R1, P1, image_size, CV_32FC1, g_map1x, g_map1y);
        cv::fisheye::initUndistortRectifyMap(
            K2, D2, R2, P2, image_size, CV_32FC1, g_map2x, g_map2y);
        runtime->width = width;
        runtime->height = height;
        runtime->fx = P1.at<double>(0, 0);
        runtime->fy = P1.at<double>(1, 1);
        runtime->cx = P1.at<double>(0, 2);
        runtime->cy = P1.at<double>(1, 2);
        runtime->baseline = cv::norm(T);
        // Vendor dump: T_cam_imu maps IMU/body -> unrectified cam0.
        // ORB-SLAM3's IMU.T_b_c1 is Tbc (camera -> body).  Because images
        // entering TrackStereo are already transformed by R1, convert the
        // extrinsic to rectified-cam0 -> body before writing the ORB YAML.
        cv::Mat T_r1_u1 = cv::Mat::eye(4, 4, CV_64F);
        R1.copyTo(T_r1_u1(cv::Rect(0, 0, 3, 3)));
        runtime->T_b_c1 = T_cam_imu.inv() * T_r1_u1.inv();
    } catch (const cv::Exception& error) {
        std::cerr << "[FAYS-CALIB] live rectification failed: "
                  << error.what() << "\n";
        return false;
    }
    if (g_map1x.empty() || g_map1y.empty() || g_map2x.empty() ||
        g_map2y.empty() || !std::isfinite(runtime->fx) ||
        !std::isfinite(runtime->baseline) || runtime->fx <= 0.0 ||
        runtime->baseline <= 0.0 || !cv::checkRange(runtime->T_b_c1)) {
        std::cerr << "[FAYS-CALIB] invalid live rectification result\n";
        return false;
    }
    g_cam_imu_timeshift_s = runtime->timeshift_cam_imu;
    return true;
}

static bool configureSdkFactoryCalibration(
        void* handle, const std::string& calibration_directory,
        RuntimeCalibration* runtime) {
    if (!runtime) return false;
    ViKitDeviceInfo info{};
    if (FAYS_VIK_GetDeviceInfo(handle, &info) != EXIT_SUCCESS) {
        std::cerr << "[FAYS-CALIB] failed to read device identity\n";
        return false;
    }
    const std::string serial = boundedString(
        info.serial_number, sizeof(info.serial_number));
    if (serial.empty()) {
        std::cerr << "[FAYS-CALIB] device serial is empty\n";
        return false;
    }
    std::cerr << "[FAYS-CALIB] live device serial accepted: "
              << serial << "\n";
    runtime->serial = serial;
    runtime->model = boundedString(
        info.device_model, sizeof(info.device_model));

    AtrakCalibrationParam calibration{};
    if (FAYS_VIK_GetCalibrationParam(handle, &calibration) != EXIT_SUCCESS) {
        std::cerr << "[FAYS-CALIB] SDK factory calibration read failed\n";
        return false;
    }
    if (calibration.cameras.num_of_cams < 2) {
        std::cerr << "[FAYS-CALIB] expected two calibrated cameras, actual="
                  << calibration.cameras.num_of_cams << "\n";
        return false;
    }

    // VI Kit exposes the factory IMU model through
    // FAYS_VIK_GetCalibrationParam().  Validate it independently of the
    // camera available_mask: SDK 3.5.2 reports zero camera masks for SN198,
    // but still returns a complete, non-zero IMU calibration.
    const AtrakImuParam& imu = calibration.imu;
    if (!std::isfinite(imu.accelerometer_noise_density) ||
        !std::isfinite(imu.accelerometer_random_walk) ||
        !std::isfinite(imu.gyroscope_noise_density) ||
        !std::isfinite(imu.gyroscope_random_walk) ||
        !std::isfinite(imu.update_rate) ||
        imu.accelerometer_noise_density <= 0.0 ||
        imu.accelerometer_random_walk <= 0.0 ||
        imu.gyroscope_noise_density <= 0.0 ||
        imu.gyroscope_random_walk <= 0.0 || imu.update_rate <= 0.0f) {
        std::cerr << "[FAYS-CALIB] invalid factory IMU noise model\n";
        return false;
    }
    runtime->noise_acc = imu.accelerometer_noise_density;
    runtime->walk_acc = imu.accelerometer_random_walk;
    runtime->noise_gyro = imu.gyroscope_noise_density;
    runtime->walk_gyro = imu.gyroscope_random_walk;
    runtime->imu_hz = imu.update_rate;

    const AtrakCamParam& left = calibration.cameras.cameras[0];
    const AtrakCamParam& right = calibration.cameras.cameras[1];
    // The documented standalone workflow uses the official dump as the
    // camera source. Do that for every serial, not only SN198, so the exact
    // raw factory file used for this ORB run remains auditable.
    const std::string dump_directory = calibration_directory +
        "/fays_factory_calib_" + std::to_string(getpid());
    if (mkdir(dump_directory.c_str(), 0700) != 0 && errno != EEXIST) {
        std::cerr << "[FAYS-CALIB] cannot create live dump directory: "
                  << dump_directory << " error=" << std::strerror(errno)
                  << "\n";
        return false;
    }
    const std::string dump_output_directory = dump_directory + "/";
    if (FAYS_VIK_DumpCalib(
            handle, dump_output_directory.c_str()) != EXIT_SUCCESS) {
        std::cerr << "[FAYS-CALIB] official live DumpCalib failed\n";
        return false;
    }
    const std::string dump_path = findLiveDump(dump_directory, serial);
    DumpCameraCalibration dump[2];
    if (dump_path.empty() || !parseDumpCalibration(dump_path, dump)) {
        std::cerr << "[FAYS-CALIB] cannot identify complete live SDK dump"
                  << " serial=" << serial << " dir=" << dump_directory
                  << "\n";
        return false;
    }
    if (dump[0].resolution != dump[1].resolution ||
        dump[0].resolution[0] != std::floor(dump[0].resolution[0]) ||
        dump[0].resolution[1] != std::floor(dump[0].resolution[1])) {
        std::cerr << "[FAYS-CALIB] invalid live dump stereo resolution\n";
        return false;
    }
    auto K = [](const DumpCameraCalibration& cam) -> cv::Mat {
        cv::Mat matrix = (cv::Mat_<double>(3, 3) <<
            cam.intrinsics[0], 0.0, cam.intrinsics[2],
            0.0, cam.intrinsics[1], cam.intrinsics[3],
            0.0, 0.0, 1.0);
        return matrix;
    };
    auto D = [](const DumpCameraCalibration& cam) -> cv::Mat {
        cv::Mat matrix = (cv::Mat_<double>(1, 4) <<
            cam.distortion[0], cam.distortion[1],
            cam.distortion[2], cam.distortion[3]);
        return matrix;
    };
    runtime->camera_source = "sdk_factory_dump_live";
    runtime->timeshift_cam_imu = dump[0].timeshift;
    if (!writeSplitCalibrationYaml(*runtime, dump[0], dump[1])) {
        std::cerr << "[FAYS-CALIB] cannot write split camera/IMU YAML\n";
        return false;
    }
    if (!finalizeRuntimeCalibration(
            K(dump[0]), D(dump[0]), K(dump[1]), D(dump[1]),
            dump[1].T_cn_cnm1(cv::Rect(0, 0, 3, 3)).clone(),
            dump[1].T_cn_cnm1(cv::Rect(3, 0, 1, 3)).clone(),
            static_cast<int>(dump[0].resolution[0]),
            static_cast<int>(dump[0].resolution[1]),
            dump[0].T_cam_imu, runtime)) return false;
    std::cout << "[FAYS-CALIB] live_dump=" << dump_path
              << " sdk_masks=0x" << std::hex
              << static_cast<int>(left.available_mask) << "/0x"
              << static_cast<int>(right.available_mask) << std::dec << "\n";
    std::cout << std::setprecision(15)
              << "[FAYS-CALIB] source=" << runtime->camera_source
              << " serial=" << serial
              << " rect_fx=" << runtime->fx
              << " rect_fy=" << runtime->fy
              << " rect_cx=" << runtime->cx
              << " rect_cy=" << runtime->cy
              << " baseline_m=" << runtime->baseline
              << " timeshift_cam_imu_s=" << g_cam_imu_timeshift_s
              << " noise_acc=" << runtime->noise_acc
              << " walk_acc=" << runtime->walk_acc
              << " noise_gyro=" << runtime->noise_gyro
              << " walk_gyro=" << runtime->walk_gyro
              << " imu_hz=" << runtime->imu_hz << "\n";
    return true;
}

static bool writeRuntimeOrbYaml(
        const std::string& template_path, const std::string& output_path,
        const RuntimeCalibration& runtime) {
    std::ifstream input(template_path);
    if (!input) {
        std::cerr << "[FAYS-CALIB] cannot open ORB settings template: "
                  << template_path << "\n";
        return false;
    }
    const std::string temporary = output_path + ".tmp";
    std::ofstream output(temporary, std::ios::trunc);
    if (!output) return false;
    output << std::setprecision(15);
    bool replaced_fx=false, replaced_fy=false, replaced_cx=false;
    bool replaced_cy=false, replaced_b=false, replaced_T=false;
    bool replaced_ng=false, replaced_na=false, replaced_gw=false;
    bool replaced_aw=false, replaced_hz=false, replaced_w=false;
    bool replaced_h=false;
    bool skipping_transform = false;
    auto replace_real = [&](const std::string& line, const char* key,
                            double value, bool* replaced) -> bool {
        if (line.find(std::string(key) + ":") != 0) return false;
        std::ostringstream formatted;
        formatted << std::setprecision(15) << value;
        std::string text = formatted.str();
        if (text.find_first_of(".eE") == std::string::npos) text += ".0";
        output << key << ": " << text << "\n";
        *replaced = true;
        return true;
    };
    auto replace_integer = [&](const std::string& line, const char* key,
                               int value, bool* replaced) -> bool {
        if (line.find(std::string(key) + ":") != 0) return false;
        output << key << ": " << value << "\n";
        *replaced = true;
        return true;
    };
    std::string raw;
    while (std::getline(input, raw)) {
        const std::string line = trim(raw);
        if (skipping_transform) {
            if (raw.empty() || (!raw.empty() && std::isspace(
                    static_cast<unsigned char>(raw[0])))) continue;
            skipping_transform = false;
        }
        if (line.find("IMU.T_b_c1:") == 0) {
            output << "IMU.T_b_c1: !!opencv-matrix\n"
                   << "  rows: 4\n  cols: 4\n  dt: f\n  data: [ ";
            for (int row = 0; row < 4; ++row) {
                for (int col = 0; col < 4; ++col) {
                    if (row != 0 || col != 0) output << ", ";
                    output << runtime.T_b_c1.at<double>(row, col);
                }
            }
            output << " ]\n";
            replaced_T = true;
            skipping_transform = true;
            continue;
        }
        if (replace_real(line,"Camera1.fx",runtime.fx,&replaced_fx) ||
            replace_real(line,"Camera1.fy",runtime.fy,&replaced_fy) ||
            replace_real(line,"Camera1.cx",runtime.cx,&replaced_cx) ||
            replace_real(line,"Camera1.cy",runtime.cy,&replaced_cy) ||
            replace_real(line,"Stereo.b",runtime.baseline,&replaced_b) ||
            replace_real(line,"IMU.NoiseGyro",runtime.noise_gyro,&replaced_ng) ||
            replace_real(line,"IMU.NoiseAcc",runtime.noise_acc,&replaced_na) ||
            replace_real(line,"IMU.GyroWalk",runtime.walk_gyro,&replaced_gw) ||
            replace_real(line,"IMU.AccWalk",runtime.walk_acc,&replaced_aw) ||
            replace_real(line,"IMU.Frequency",runtime.imu_hz,&replaced_hz) ||
            replace_integer(line,"Camera.width",runtime.width,&replaced_w) ||
            replace_integer(line,"Camera.height",runtime.height,&replaced_h)) continue;
        output << raw << "\n";
    }
    output.close();
    const bool complete = replaced_fx && replaced_fy && replaced_cx &&
        replaced_cy && replaced_b && replaced_T && replaced_ng && replaced_na &&
        replaced_gw && replaced_aw && replaced_hz && replaced_w && replaced_h;
    if (!complete || !output || rename(temporary.c_str(), output_path.c_str()) != 0) {
        unlink(temporary.c_str());
        std::cerr << "[FAYS-CALIB] failed to generate complete runtime ORB YAML\n";
        return false;
    }
    std::cout << "[FAYS-CALIB] orb_runtime_yaml=" << output_path
              << " template=" << template_path << "\n";
    return true;
}

// Fays 双目和 IMU 由厂商 SDK 的原生回调线程交付。两个回调可能由不同线程
// 调用，也可能复用同一线程，因此必须在每个实际进入回调的线程上设置亲和性，
// 不能只给启动 SDK 的主线程绑核。
// 与上位机两路 Sightac cap.read() 共用CPU4；后续阶段使用CPU5-8专用域。
static constexpr int kFaysInputCpu = 4;
static constexpr int kFaysPrepareCpu = 5;
static constexpr int kFaysTrackCpu = 8;
static constexpr int kFaysCleanupCpu = 9;
static std::mutex g_input_affinity_log_mutex;

// 双设备模式下上位机可把第二套 Fays 输入回调放到另一个核；
// 单设备/未设置环境变量时保持默认 CPU4，行为不变。
static int configuredFaysInputCpu() {
    static const int cpu = []() -> int {
        const char* configured = std::getenv("KSQ_FAYS_INPUT_CPU");
        if (!configured || !*configured)
            return kFaysInputCpu;
        errno = 0;
        char* end = nullptr;
        const long parsed = std::strtol(configured, &end, 10);
        if (errno != 0 || end == configured || *end != '\0'
                || parsed < 0 || parsed >= CPU_SETSIZE) {
            std::cerr << "[FAYS-AFFINITY] invalid KSQ_FAYS_INPUT_CPU="
                      << configured << " falling back to "
                      << kFaysInputCpu << "\n";
            return kFaysInputCpu;
        }
        return static_cast<int>(parsed);
    }();
    return cpu;
}

// 仅在启动前显式设置时覆盖 tracking/output 的默认 CPU8。这样双设备
// affinity 实验不需要在线程运行后从外部迁移；未设置时生产行为完全不变。
static int configuredFaysTrackCpu() {
    static const int cpu = []() -> int {
        const char* configured = std::getenv("KSQ_FAYS_TRACK_CPU");
        if (!configured || !*configured)
            return kFaysTrackCpu;
        errno = 0;
        char* end = nullptr;
        const long parsed = std::strtol(configured, &end, 10);
        if (errno != 0 || end == configured || *end != '\0'
                || parsed < 0 || parsed >= CPU_SETSIZE) {
            std::cerr << "[FAYS-AFFINITY] invalid KSQ_FAYS_TRACK_CPU="
                      << configured << " falling back to "
                      << kFaysTrackCpu << "\n";
            return kFaysTrackCpu;
        }
        return static_cast<int>(parsed);
    }();
    return cpu;
}

static int configuredFaysPrepareCpu() {
    static const int cpu = []() -> int {
        const char* configured = std::getenv("KSQ_FAYS_PREPARE_CPU");
        if (!configured || !*configured)
            return kFaysPrepareCpu;
        errno = 0;
        char* end = nullptr;
        const long parsed = std::strtol(configured, &end, 10);
        if (errno != 0 || end == configured || *end != '\0'
                || parsed < 0 || parsed >= CPU_SETSIZE) {
            std::cerr << "[FAYS-AFFINITY] invalid KSQ_FAYS_PREPARE_CPU="
                      << configured << " falling back to "
                      << kFaysPrepareCpu << "\n";
            return kFaysPrepareCpu;
        }
        return static_cast<int>(parsed);
    }();
    return cpu;
}

static bool bindFaysStageThreadToCpus(
        const char* thread_name, const char* role,
        const std::vector<int>& cpus) {
    // Linux comm 最长 15 个可见字符；固定名字也供父进程边界审计器识别。
    pthread_setname_np(pthread_self(), thread_name);

    cpu_set_t requested;
    CPU_ZERO(&requested);
    for (int cpu : cpus) CPU_SET(cpu, &requested);
    const int bind_result = sched_setaffinity(0, sizeof(requested), &requested);

    cpu_set_t actual;
    CPU_ZERO(&actual);
    bool verified = bind_result == 0
        && sched_getaffinity(0, sizeof(actual), &actual) == 0
        && CPU_COUNT(&actual) == static_cast<int>(cpus.size());
    for (int cpu : cpus) verified = verified && CPU_ISSET(cpu, &actual);
    {
        std::lock_guard<std::mutex> lk(g_input_affinity_log_mutex);
        std::cerr << "[FAYS-AFFINITY] " << role
                  << " stage: tid=" << static_cast<long>(syscall(SYS_gettid))
                  << " cpus=";
        for (size_t i = 0; i < cpus.size(); ++i) {
            if (i) std::cerr << ",";
            std::cerr << cpus[i];
        }
        std::cerr
                  << " verified=" << (verified ? "yes" : "no");
        if (!verified) {
            const int error = errno;
            std::cerr << " error=" << std::strerror(error);
        }
        std::cerr << "\n";
    }
    return verified;
}

static bool bindFaysStageThread(
        const char* thread_name, const char* role, int cpu) {
    return bindFaysStageThreadToCpus(thread_name, role, {cpu});
}

static void bindFaysInputCallbackThread(const char* role, unsigned role_bit) {
    thread_local bool bound = false;
    thread_local bool failure_reported = false;
    thread_local unsigned reported_roles = 0;
    const int input_cpu = configuredFaysInputCpu();

    if (!bound) {
        pthread_setname_np(pthread_self(), "fays-input");
        cpu_set_t requested;
        CPU_ZERO(&requested);
        CPU_SET(input_cpu, &requested);
        if (sched_setaffinity(0, sizeof(requested), &requested) == 0) {
            bound = true;
        } else if (!failure_reported) {
            const int error = errno;
            std::lock_guard<std::mutex> lk(g_input_affinity_log_mutex);
            std::cerr << "[FAYS-AFFINITY] " << role
                      << " callback bind FAILED: tid="
                      << static_cast<long>(syscall(SYS_gettid))
                      << " requested_cpu=" << input_cpu
                      << " error=" << std::strerror(error) << "\n";
            failure_reported = true;
        }
    }

    if (!bound || (reported_roles & role_bit) != 0) return;

    cpu_set_t actual;
    CPU_ZERO(&actual);
    const bool verified =
        sched_getaffinity(0, sizeof(actual), &actual) == 0
        && CPU_COUNT(&actual) == 1
        && CPU_ISSET(input_cpu, &actual);
    {
        std::lock_guard<std::mutex> lk(g_input_affinity_log_mutex);
        std::cerr << "[FAYS-AFFINITY] " << role
                  << " callback: tid="
                  << static_cast<long>(syscall(SYS_gettid))
                  << " cpu=" << input_cpu
                  << " verified=" << (verified ? "yes" : "no") << "\n";
    }
    reported_roles |= role_bit;
}

// ============================================================================
// SDK 回调
// ============================================================================
void imuCallback(const AtrakIMU& imu) {
    g_debug_capture.imu(imu);  // retain raw values even if finite validation rejects them
    bindFaysInputCallbackThread("imu", 1u);
    if (g_fake_imu) return;
    for (int axis = 0; axis < 3; ++axis) {
        if (!std::isfinite(imu.acc[axis]) || !std::isfinite(imu.gyro[axis])) return;
    }
    g_imu_input_count.fetch_add(1, std::memory_order_relaxed);
    pushRawImu(imu);
    std::lock_guard<std::mutex> lk(g_imu_mutex);
    g_imu_buf.push_back(imu);
}

void stereoCallback(AtrakImage* img) {
    bindFaysInputCallbackThread("stereo", 2u);
    if (!img || !img->data) return;
    if (img->width <= 0 || img->height <= 1 || img->height % 2 != 0) return;
    if (img->channel != 1 && img->channel != 3) return;

    // 取样帧的宿主时刻：取在回调入口、深拷贝之前，代表这帧「到达本进程」
    // 的时间，不含后续预处理/解算/排队耗时。与 Python 侧 time.monotonic_ns()
    // 同一时基（CLOCK_MONOTONIC 全系统一致），故可直接与 hardware_ns 比较。
    const uint64_t host_mono_ns = rawClockNs(CLOCK_MONOTONIC);

    // 输入 FPS 的计数点：SDK 已向应用交付一组有效的双目原始数据。
    g_stereo_input_count.fetch_add(1, std::memory_order_relaxed);
    recordRateEvent(g_stereo_rate_window);
    // 记录第一帧信息
    if (g_first_img) { g_first_img=false; g_img_w=img->width; g_img_h=img->height; g_img_ch=img->channel; }

    // img->data 由 SDK 持有，回调返回后生命周期不受应用控制，因此这里仍需
    // 做一次必要的深拷贝。队列已经满时直接丢弃新帧，避免在回调线程等待或
    // 复制一张注定无法入队的图像；其余图像计算全部延后到主处理循环。
    {
        std::lock_guard<std::mutex> lk(g_img_mutex);
        if (!g_running) return;
        if (g_img_queue.size() >= kImageQueueCapacity && !g_debug_capture.enabled()) {
            g_stereo_dropped_count.fetch_add(
                1, std::memory_order_relaxed);
            return;
        }
    }
    cv::Mat raw;
    try {
        const int type = img->channel == 1 ? CV_8UC1 : CV_8UC3;
        const size_t step = img->step > 0
            ? static_cast<size_t>(img->step) : cv::Mat::AUTO_STEP;
        raw = cv::Mat(img->height, img->width, type, img->data, step).clone();
    } catch (const cv::Exception&) {
        return;
    }
    if (raw.empty()) return;
    g_debug_capture.stereo(raw, *img);
    pushRawStereo(raw, *img);

    {
        std::lock_guard<std::mutex> lk(g_img_mutex);
        if (!g_running) return;
        // 预检查后仍可能与取帧线程交错，入队前必须再次检查容量。
        if (g_img_queue.size() >= kImageQueueCapacity) {
            g_stereo_dropped_count.fetch_add(
                1, std::memory_order_relaxed);
            return;
        }
        g_img_queue.push_back(
            {img->timestamp * 1e-9, img->seq, std::move(raw), host_mono_ns});
        g_stereo_enqueued_count.fetch_add(1, std::memory_order_relaxed);
    }
    g_img_cv.notify_one();
}

// ============================================================================
// Fays 专用流水线
//   CPU5 preprocess: 有界 FIFO 原始帧 → 拆分/灰度/鱼眼校正/IMU转换
//   CPU6/7 ORB:      TrackStereo内部左/右ORB提取（核心库线程入口绑定）
//   CPU8 track/post: 等待ORB → 双目匹配 → 后续Tracking → Tcw，随后完成
//                    位姿和限频 Current Frame/JPEG/meta 输出
//   CPU9:            Fays/ORB后台线程
//   CPU10-11:        Python应用普通任务（由父进程管理）
// TrackStereo 仍然严格串行；数学算法、标定参数和输出内容均不改变。
// ============================================================================
struct PreparedFrame {
    double ts = 0.0;
    // 随帧同行的宿主单调钟纳秒（见 RawImageFrame.host_mono_ns）。与 ts 一起
    // 走完 g_img_queue → g_prepared_frame → g_output_queue：g_prepared_frame
    // 是「最新覆盖」单槽，两个字段必须同进同出，否则 ts 与戳会配错帧。
    uint64_t host_mono_ns = 0;
    double preprocess_ms = 0.0;
    cv::Mat left;
    cv::Mat right;
    std::vector<ORB_SLAM3::IMU::Point> imu;
};

static std::mutex g_prepared_mutex;
static std::condition_variable g_prepared_cv;
static bool g_prepared_ready = false;
static PreparedFrame g_prepared_frame;

struct OutputFrame {
    double ts = 0.0;
    // 取样帧的宿主单调钟纳秒（见 RawImageFrame.host_mono_ns）。落盘侧靠它
    // 把 slam 点与视频帧按时间对齐——ts 是相机传感器钟，与 hardware_ns 不同源。
    uint64_t host_mono_ns = 0;
    int frame_id = 0;
    double wait_ms = 0.0;
    double preprocess_ms = 0.0;
    double middle_ms = 0.0;
    double post_main_ms = 0.0;
    unsigned long long imu_samples = 0;
    int tracking_state = ORB_SLAM3::Tracking::NO_IMAGES_YET;
    bool map_changed = false;
    Sophus::SE3f Tcw;
    cv::Mat current_frame;
};

struct FaysTimingTotals {
    unsigned long long samples = 0;
    unsigned long long imu_samples = 0;
    double wait_ms = 0.0;
    double preprocess_ms = 0.0;
    double middle_ms = 0.0;
    double post_ms = 0.0;
};

static std::mutex g_output_mutex;
static std::condition_variable g_output_cv;
static std::deque<OutputFrame> g_output_queue;
static constexpr size_t kOutputQueueCapacity = 2;
static std::atomic<unsigned long long> g_output_dropped_count{0};
static std::mutex g_timing_mutex;
static FaysTimingTotals g_timing_totals;

static void appendFakeImu(
        double image_ts, std::vector<ORB_SLAM3::IMU::Point>& samples) {
    // Fake samples are synthesized directly in the image/camera clock domain.
    const double end_ts = image_ts;
    if (g_fake_imu_started_ts < 0.0) {
        g_fake_imu_started_ts = end_ts;
        g_fake_imu_last_ts = end_ts - 0.020;
    }
    if (end_ts <= g_fake_imu_last_ts) return;

    const size_t old_size = samples.size();
    for (double sample_ts = g_fake_imu_last_ts + kFakeImuStepS;
         sample_ts <= end_ts + 1e-9;
         sample_ts += kFakeImuStepS) {
        const double elapsed = sample_ts - g_fake_imu_started_ts;
        float ax = 0.0f;
        float ay = 0.0f;
        float az = 9.80665f;
        float gx = 0.0f;
        float gy = 0.0f;
        float gz = 0.0f;
        if (elapsed < kFakeImuExcitationS) {
            constexpr double kPi = 3.14159265358979323846;
            ax = static_cast<float>(
                0.80 * std::sin(2.0 * kPi * 0.70 * elapsed));
            ay = static_cast<float>(
                0.60 * std::cos(2.0 * kPi * 0.50 * elapsed));
            az += static_cast<float>(
                0.35 * std::sin(2.0 * kPi * 0.90 * elapsed));
            gx = static_cast<float>(
                0.18 * std::sin(2.0 * kPi * 0.60 * elapsed));
            gy = static_cast<float>(
                0.14 * std::cos(2.0 * kPi * 0.45 * elapsed));
            gz = static_cast<float>(
                0.10 * std::sin(2.0 * kPi * 0.35 * elapsed));
        }
        samples.emplace_back(ax, ay, az, gx, gy, gz, sample_ts);
    }
    g_fake_imu_last_ts = end_ts;
    g_imu_input_count.fetch_add(
        static_cast<unsigned long long>(samples.size() - old_size),
        std::memory_order_relaxed);
}

struct PoseOutputState {
    bool ready = false;
    bool origin_set = false;
    bool previous_quaternion_set = false;
    bool previous_output_set = false;
    bool tracking_gap = false;
    double first_valid_ts = -1.0;
    double origin_ts = 0.0;
    double previous_output_ts = 0.0;
    Sophus::SE3f reference_pose;
    Sophus::SE3f previous_output_pose;
    Eigen::Quaternionf previous_quaternion = Eigen::Quaternionf::Identity();
    Eigen::Quaternionf accumulated_delta = Eigen::Quaternionf::Identity();
    int accumulated_frames = 0;
    std::ofstream trajectory;
    std::ofstream raw_trajectory;
};

static PoseOutputState g_pose_output;

static bool finitePose(const Sophus::SE3f& pose) {
    return pose.matrix().allFinite();
}

static float quaternionDistanceRad(
        const Sophus::SE3f& first, const Sophus::SE3f& second) {
    const Eigen::Quaternionf first_q(first.rotationMatrix());
    const Eigen::Quaternionf second_q(second.rotationMatrix());
    const float dot = std::abs(first_q.dot(second_q));
    return 2.0f * std::acos(std::max(-1.0f, std::min(1.0f, dot)));
}

static void preprocessWorker() {
    if (!bindFaysStageThread(
            "fays-prep", "preprocess", configuredFaysPrepareCpu())) {
        g_running = false;
        g_prepared_cv.notify_all();
        g_output_cv.notify_all();
        return;
    }

    while (g_running) {
        // 最多只允许一张校正帧等待 TrackStereo。原始图像队列有界；队列满时
        // SDK 回调直接丢弃新帧，不阻塞回调线程，也不覆盖队列中的旧帧。
        {
            std::unique_lock<std::mutex> lk(g_prepared_mutex);
            g_prepared_cv.wait_for(
                lk, std::chrono::milliseconds(10),
                [] { return !g_running || !g_prepared_ready; });
            if (!g_running) break;
            if (g_prepared_ready) continue;
        }

        double ts = 0.0;
        int seq = 0;
        uint64_t frame_host_ns = 0;
        cv::Mat raw;
        {
            std::unique_lock<std::mutex> lk(g_img_mutex);
            g_img_cv.wait_for(
                lk, std::chrono::milliseconds(10),
                [] { return !g_running || !g_img_queue.empty(); });
            if (!g_running) break;
            if (g_img_queue.empty()) continue;
            RawImageFrame next = std::move(g_img_queue.front());
            g_img_queue.pop_front();
            ts = next.timestamp;
            seq = next.seq;
            frame_host_ns = next.host_mono_ns;
            raw = std::move(next.image);
            // 唤醒等待新图像的预处理线程；SDK 回调不在队列满时等待。
            g_img_cv.notify_all();
        }

        // 50fps 输入抽帧为 30fps：每 5 帧保留 3 帧（索引 0/1/2 保留，
        // 3/4 丢弃）。被丢弃帧的 IMU 样本留在 g_imu_buf 中，由下一个
        // 保留帧连同两段间隔一起预积分，保证 IMU 时间戳连续。
        {
            static std::atomic<unsigned long long> selection_index{0};
            const unsigned long long input_index =
                selection_index.fetch_add(1, std::memory_order_relaxed);
            if ((input_index % 5) >= 3) {
                g_stereo_frameselect_dropped_count.fetch_add(
                    1, std::memory_order_relaxed);
                continue;
            }
        }

        // 时间戳单调性守卫：SDK 原始时间戳偶发单帧回退，直接放行会让核心库
        // 清空 IMU 队列并重建地图（位姿停发 + 重做双目/IMU 初始化）。回退帧在
        // 这里丢弃，位置必须在下面 g_imu_buf 交换之前 —— 与上面抽帧同理，被丢
        // 弃帧的 IMU 样本留在缓冲区，由下一个保留帧连同两段间隔一起预积分。
        // 连续回退达到上限时判定为时钟真跳变，放行给 SLAM 并换基准（今天的行为）。
        {
            static double last_accepted_ts = -1.0;
            // 上一个**真正进入 SLAM** 的帧的 SDK 序号。与 seq 比较即可判定回退
            // 形态：seq < prev_seq = 相邻帧投递乱序（真丢了一帧数据）；
            // seq == prev_seq = SDK 重复投递同一帧（零损失）；seq > prev_seq =
            // 新帧配了旧时间戳（SDK 内部戳错）。三者成因与对策完全不同。
            static int last_accepted_seq = -1;
            static unsigned consecutive_drops = 0;
            static unsigned long long total_drops = 0;
            const ksq::FrameTimeVerdict verdict = ksq::ClassifyFrameTime(
                last_accepted_ts, ts, consecutive_drops);
            if (verdict == ksq::FrameTimeVerdict::DropStale) {
                ++consecutive_drops;
                ++total_drops;
                g_time_drop_count.fetch_add(1, std::memory_order_relaxed);
                // 不限频：这是崩溃链上游唯一的直接证据。限频会让「静默」与
                // 「停止了」无法区分（实测最坏 56 分钟 42 次，量级上不需要）。
                std::cerr << "[TIME_DROP] ts=" << ts
                          << " previous=" << last_accepted_ts
                          << " delta=" << (ts - last_accepted_ts)
                          << " seq=" << seq
                          << " prev_seq=" << last_accepted_seq
                          << " consecutive=" << consecutive_drops
                          << " total=" << total_drops << "\n";
                continue;
            }
            if (verdict == ksq::FrameTimeVerdict::Rebase) {
                std::cerr << "[TIME_REBASE] ts=" << ts
                          << " previous=" << last_accepted_ts
                          << " delta=" << (ts - last_accepted_ts)
                          << " seq=" << seq
                          << " prev_seq=" << last_accepted_seq
                          << " consecutive=" << consecutive_drops
                          << " total=" << total_drops << "\n";
            }
            consecutive_drops = 0;
            last_accepted_ts = ts;
            last_accepted_seq = seq;
        }

        PreparedFrame prepared;
        prepared.ts = ts;
        prepared.host_mono_ns = frame_host_ns;
        const auto preprocess_started = std::chrono::steady_clock::now();
        try {
            const int half_h = raw.rows / 2;
            cv::Mat left = raw(cv::Rect(0, 0, raw.cols, half_h)).clone();
            cv::Mat right = raw(cv::Rect(0, half_h, raw.cols, half_h)).clone();
            if (raw.channels() != 1) {
                cv::cvtColor(left, left, cv::COLOR_BGR2GRAY);
                cv::cvtColor(right, right, cv::COLOR_BGR2GRAY);
            }
            cv::remap(left, prepared.left, g_map1x, g_map1y, cv::INTER_LINEAR);
            cv::remap(right, prepared.right, g_map2x, g_map2y, cv::INTER_LINEAR);
        } catch (const cv::Exception& exc) {
            std::cerr << "[SLAM] preprocess frame dropped: " << exc.what() << "\n";
            continue;
        }

        if (g_fake_imu) {
            prepared.imu.reserve(32);
            appendFakeImu(ts, prepared.imu);
        } else {
            // 正式路径保留交换并清空真实 IMU 缓冲的原有处理逻辑。
            std::vector<AtrakIMU> raw_imu;
            {
                std::lock_guard<std::mutex> lk(g_imu_mutex);
                raw_imu.swap(g_imu_buf);
            }
            prepared.imu.reserve(raw_imu.size());
            for (const AtrakIMU& imu : raw_imu) {
                const double imu_ts =
                    imu.timestamp * 1e-9 - g_cam_imu_timeshift_s;
                prepared.imu.emplace_back(
                    static_cast<float>(imu.acc[0]),
                    static_cast<float>(imu.acc[1]),
                    static_cast<float>(imu.acc[2]),
                    static_cast<float>(imu.gyro[0]),
                    static_cast<float>(imu.gyro[1]),
                    static_cast<float>(imu.gyro[2]),
                    imu_ts);
            }
        }
        prepared.preprocess_ms = std::chrono::duration<double, std::milli>(
            std::chrono::steady_clock::now() - preprocess_started).count();

        {
            std::lock_guard<std::mutex> lk(g_prepared_mutex);
            if (!g_running) break;
            g_prepared_frame = std::move(prepared);
            g_prepared_ready = true;
        }
        g_prepared_cv.notify_all();
    }
}

static bool enqueueOutput(OutputFrame&& output) {
    std::lock_guard<std::mutex> lk(g_output_mutex);
    if (!g_running) return false;
    if (g_output_queue.size() >= kOutputQueueCapacity) {
        // TrackStereo 已完成，但发布队列满；丢弃新结果并继续处理后续帧。
        // 返回 true，避免把“结果丢弃”误判成 SLAM 停止。
        g_output_dropped_count.fetch_add(1, std::memory_order_relaxed);
        return true;
    }
    g_output_queue.emplace_back(std::move(output));
    g_output_cv.notify_all();
    return true;
}

static void processPoseOutput(const OutputFrame& output) {
    PoseOutputState& state = g_pose_output;
    const double ts = output.ts;
    const Sophus::SE3f Twc = output.Tcw.inverse();
    const bool tracking_ok =
        output.tracking_state == ORB_SLAM3::Tracking::OK;

    // TrackStereo may return identity/stale estimates while initialization or
    // relocalization is in progress.  Hold the last 6DoF sample; never turn a
    // tracking-state transition into a line back to the map origin.
    if (!tracking_ok || !finitePose(Twc)) {
        if (state.origin_set && !state.tracking_gap) {
            std::cerr << "[POSE_HOLD] tracking_state="
                      << output.tracking_state
                      << " finite=" << (finitePose(Twc) ? "yes" : "no")
                      << "\n";
        }
        if (state.origin_set) state.tracking_gap = true;
        return;
    }

    // IMU init + Enter-to-start。状态只由output线程访问，保持逐帧顺序。
    if (!state.ready && Twc.translation().norm() > 0.01) {
        if (state.first_valid_ts < 0) {
            state.first_valid_ts = ts;
            std::cout << "[IMU] Init (2s)..." << std::endl;
        } else if (ts - state.first_valid_ts > 2.0) {
            state.ready = true;
            state.reference_pose = Twc;
            std::cout << "\n=== READY. Press ENTER to set origin. ===\n\n";
        }
    }

    char c;
    while (read(STDIN_FILENO, &c, 1) > 0 && (c == '\n' || c == '\r')) {
        if (state.ready && !state.origin_set) {
            state.reference_pose = Twc;
            state.origin_set = true;
            state.origin_ts = ts;
            state.previous_output_set = false;
            state.tracking_gap = false;
            state.previous_quaternion_set = false;
            state.accumulated_delta = Eigen::Quaternionf::Identity();
            state.accumulated_frames = 0;
            std::cout << ">>> ORIGIN SET <<<\n";
        } else if (state.origin_set) {
            state.reference_pose = Twc;
            state.origin_ts = ts;
            state.previous_output_set = false;
            state.tracking_gap = false;
            state.previous_quaternion_set = false;
            state.accumulated_delta = Eigen::Quaternionf::Identity();
            state.accumulated_frames = 0;
            std::cout << ">>> NEW ORIGIN <<<\n";
        }
    }

    if (!state.origin_set) return;

    Sophus::SE3f Twc_out = state.reference_pose.inverse() * Twc;
    if (!finitePose(Twc_out)) {
        state.tracking_gap = true;
        std::cerr << "[POSE_HOLD] tracking_state="
                  << output.tracking_state << " finite=no\n";
        return;
    }

    if (state.previous_output_set) {
        const double interval = ts - state.previous_output_ts;
        const double guarded_interval = std::min(
            kPoseMaxGuardIntervalS, std::max(0.0, interval));
        const float translation_step = (
            Twc_out.translation()
            - state.previous_output_pose.translation()).norm();
        const float translation_limit =
            kPoseStepAllowanceM
            + kPoseMaxLinearSpeedMps
                * static_cast<float>(guarded_interval);
        const float angle_step = quaternionDistanceRad(
            state.previous_output_pose, Twc_out);
        const float angle_limit =
            kPoseAngleAllowanceRad
            + kPoseMaxAngularSpeedRadS
                * static_cast<float>(guarded_interval);

        const char* rebase_reason = nullptr;
        if (state.tracking_gap) {
            rebase_reason = "tracking_recovery";
        } else if (output.map_changed) {
            rebase_reason = "map_change";
        } else if (!(interval > 0.0) || !std::isfinite(interval)) {
            rebase_reason = "non_monotonic_time";
        } else if (translation_step > translation_limit) {
            rebase_reason = "translation_jump";
        } else if (angle_step > angle_limit) {
            rebase_reason = "rotation_jump";
        }

        if (rebase_reason != nullptr) {
            // Preserve the last emitted pose while adopting the newly
            // optimized ORB world frame.  Subsequent real motion is measured
            // in that frame, so UI, trajectory and HDF5 stay continuous.
            state.reference_pose =
                Twc * state.previous_output_pose.inverse();
            Twc_out = state.reference_pose.inverse() * Twc;
            std::cerr << std::fixed << std::setprecision(6)
                      << "[POSE_REBASE] reason=" << rebase_reason
                      << " dt_s=" << interval
                      << " translation_step_m=" << translation_step
                      << " translation_limit_m=" << translation_limit
                      << " angle_step_deg="
                      << angle_step * 180.0f / static_cast<float>(M_PI)
                      << " angle_limit_deg="
                      << angle_limit * 180.0f / static_cast<float>(M_PI)
                      << "\n";
        }
    }
    state.tracking_gap = false;

    const Eigen::Vector3f raw_position = Twc_out.translation();
    const Eigen::Quaternionf raw_quaternion(Twc_out.rotationMatrix());

    // S80M 坐标系修正: R_z(-90°) → R_y(+90°)
    const Eigen::Matrix3f correction =
        Eigen::AngleAxisf(M_PI / 2, Eigen::Vector3f::UnitY()).toRotationMatrix()
      * Eigen::AngleAxisf(-M_PI / 2, Eigen::Vector3f::UnitZ()).toRotationMatrix();
    const Eigen::Vector3f position = correction * raw_position;
    const Eigen::Quaternionf quaternion =
        Eigen::Quaternionf(correction) * raw_quaternion;

    if (state.raw_trajectory.is_open()) {
        state.raw_trajectory
            << std::setprecision(6) << (ts - state.origin_ts) << " "
            << std::setprecision(9)
            << raw_position.x() << " " << raw_position.y() << " "
            << raw_position.z() << " " << raw_quaternion.x() << " "
            << raw_quaternion.y() << " " << raw_quaternion.z() << " "
            << raw_quaternion.w() << "\n";
    }

    if (!state.previous_quaternion_set) {
        state.previous_quaternion = quaternion;
        state.previous_quaternion_set = true;
    }
    const Eigen::Quaternionf delta =
        state.previous_quaternion.conjugate() * quaternion;
    state.previous_quaternion = quaternion;
    state.accumulated_delta = delta * state.accumulated_delta;
    ++state.accumulated_frames;

    if (state.accumulated_frames >= 10) {
        const float clamped_w = std::max(
            -1.0f, std::min(1.0f, state.accumulated_delta.w()));
        const float delta_angle_deg =
            2.0f * std::acos(clamped_w) * 180.0f / M_PI;
        Eigen::Vector3f delta_axis(
            state.accumulated_delta.x(), state.accumulated_delta.y(),
            state.accumulated_delta.z());
        const float axis_norm = delta_axis.norm();
        if (axis_norm > 1e-9f) {
            delta_axis /= axis_norm;
        } else {
            delta_axis = Eigen::Vector3f(0, 0, 1);
        }
        delta_axis = correction * delta_axis;

        std::cout << std::fixed << std::setprecision(4)
                  << "[" << (ts - state.origin_ts) << "] XYZ:("
                  << position.x() << "," << position.y() << ","
                  << position.z() << ") Quat:(w=" << quaternion.w()
                  << ",x=" << quaternion.x() << ",y=" << quaternion.y()
                  << ",z=" << quaternion.z() << ") Host:("
                  << output.host_mono_ns << ")\n"
                  << "       Δq(" << state.accumulated_frames << "f):"
                  << std::setprecision(2) << delta_angle_deg << "° axis=("
                  << std::setprecision(3) << delta_axis.x() << ","
                  << delta_axis.y() << "," << delta_axis.z() << ")\n";
        state.accumulated_delta = Eigen::Quaternionf::Identity();
        state.accumulated_frames = 0;
    } else {
        std::cout << std::fixed << std::setprecision(4)
                  << "[" << (ts - state.origin_ts) << "] XYZ:("
                  << position.x() << "," << position.y() << ","
                  << position.z() << ") Quat:(w=" << quaternion.w()
                  << ",x=" << quaternion.x() << ",y=" << quaternion.y()
                  << ",z=" << quaternion.z() << ") Host:("
                  << output.host_mono_ns << ")\n";
    }
    // stdout is a pipe in the production launcher. Flush once per complete
    // pose so Python receives frames continuously instead of in stdio-sized
    // bursts; keep trajectory/recording output unchanged.
    std::cout.flush();

    if (state.trajectory.is_open()) {
        state.trajectory
            << std::setprecision(6) << (ts - state.origin_ts) << " "
            << std::setprecision(9)
            << position.x() << " " << position.y() << " " << position.z()
            << " " << quaternion.x() << " " << quaternion.y() << " "
            << quaternion.z() << " " << quaternion.w() << "\n";
    }
    state.previous_output_pose = Twc_out;
    state.previous_output_ts = ts;
    state.previous_output_set = true;
}

static void outputWorker() {
    if (!bindFaysStageThread(
            "fays-output", "output", configuredFaysTrackCpu())) {
        g_running = false;
        g_prepared_cv.notify_all();
        g_output_cv.notify_all();
        return;
    }

    while (true) {
        OutputFrame output;
        {
            std::unique_lock<std::mutex> lk(g_output_mutex);
            g_output_cv.wait_for(
                lk, std::chrono::milliseconds(10),
                [] { return !g_running || !g_output_queue.empty(); });
            if (g_output_queue.empty()) {
                if (!g_running) break;
                continue;
            }
            output = std::move(g_output_queue.front());
            g_output_queue.pop_front();
        }
        g_output_cv.notify_all();

        const auto output_started = std::chrono::steady_clock::now();
        processPoseOutput(output);

        // Current Frame 已在 Tracking 线程按 20fps 生成；这里只负责 JPEG
        // 和 meta 原子发布。SLAM 位姿处理仍保持 SDK 原始帧率。
        if (!output.current_frame.empty()) {
            std::vector<uchar> jpeg_buf;
            cv::imencode(
                ".jpg", output.current_frame, jpeg_buf,
                {cv::IMWRITE_JPEG_QUALITY, 60});
            write_atomic(
                g_ipc_paths.current_frame_tmp.c_str(),
                g_ipc_paths.current_frame.c_str(),
                jpeg_buf.data(), jpeg_buf.size());

            char meta[256];
            snprintf(
                meta, sizeof(meta),
                "{\"w\":%d,\"h\":%d,\"ts\":%.6f,\"frame_id\":%d}\n",
                output.current_frame.cols, output.current_frame.rows,
                output.ts, output.frame_id);
            write_atomic(
                g_ipc_paths.meta_tmp.c_str(), g_ipc_paths.meta.c_str(),
                meta, strlen(meta));
        }

        const double output_ms = std::chrono::duration<double, std::milli>(
            std::chrono::steady_clock::now() - output_started).count();
        {
            std::lock_guard<std::mutex> lk(g_timing_mutex);
            ++g_timing_totals.samples;
            g_timing_totals.imu_samples += output.imu_samples;
            g_timing_totals.wait_ms += output.wait_ms;
            g_timing_totals.preprocess_ms += output.preprocess_ms;
            g_timing_totals.middle_ms += output.middle_ms;
            g_timing_totals.post_ms += output.post_main_ms + output_ms;
        }
        // 完成FPS包含位姿、Current Frame、JPEG和meta的全部输出路径。
        recordRateEvent(g_process_rate_window);
    }
}

// SDK 在打开设备时不会自动应用配置里的 stereo_fps；读取配置后必须
// 通过官方接口显式设置，否则这条链路会回到 UVC 默认的 25 FPS。
static int readConfigStereoFps(const std::string& config_path) {
    std::ifstream in(config_path);
    if (!in) return 25;
    std::string line;
    while (std::getline(in, line)) {
        const size_t pos = line.find(':');
        if (pos == std::string::npos) continue;
        std::string key = line.substr(0, pos);
        const size_t first = key.find_first_not_of(" \t\r\n");
        const size_t last = key.find_last_not_of(" \t\r\n");
        if (first == std::string::npos) continue;
        key = key.substr(first, last - first + 1);
        if (key != "stereo_fps") continue;
        const std::string value = line.substr(pos + 1);
        const size_t value_first = value.find_first_not_of(" \t\r\n");
        if (value_first == std::string::npos) return 25;
        const int fps = std::atoi(value.c_str() + value_first);
        return fps > 0 ? fps : 25;
    }
    return 25;
}

static std::string readConfigStereoDevPort(const std::string& config_path) {
    std::ifstream in(config_path);
    if (!in) return "";
    std::string line;
    while (std::getline(in, line)) {
        const size_t pos = line.find(':');
        if (pos == std::string::npos) continue;
        std::string key = line.substr(0, pos);
        const size_t first = key.find_first_not_of(" \t\r\n");
        const size_t last = key.find_last_not_of(" \t\r\n");
        if (first == std::string::npos) continue;
        key = key.substr(first, last - first + 1);
        if (key != "stereo_dev_port") continue;
        const std::string value = line.substr(pos + 1);
        const size_t value_first = value.find_first_not_of(" \t\r\n");
        if (value_first == std::string::npos) return "";
        std::string port = value.substr(value_first);
        const size_t end = port.find_first_of(" \t\r\n");
        if (end != std::string::npos) port.resize(end);
        return port;
    }
    return "";
}

// S80M 的 UVC 节点只提供 25/50fps 两个离散帧间隔，而 SDK 的
// FAYS_VIK_SetStereoFPS 接口未开放。因此必须在 SDK 打开设备前直接对
// V4L2 节点设置帧间隔；实测 SDK 打开后不会重置该设置。
static bool setV4l2StereoFps(const std::string& device, int fps) {
    if (device.empty() || fps <= 0) return false;
    const int fd = ::open(device.c_str(), O_RDWR | O_NONBLOCK);
    if (fd < 0) {
        std::cerr << "[FAYS-CALIB] WARN open " << device
                  << " failed: " << std::strerror(errno) << "\n";
        return false;
    }
    struct v4l2_streamparm parm;
    std::memset(&parm, 0, sizeof(parm));
    parm.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (::ioctl(fd, VIDIOC_G_PARM, &parm) != 0) {
        std::cerr << "[FAYS-CALIB] WARN VIDIOC_G_PARM " << device
                  << " failed: " << std::strerror(errno) << "\n";
        ::close(fd);
        return false;
    }
    parm.parm.capture.timeperframe.numerator = 1;
    parm.parm.capture.timeperframe.denominator =
        static_cast<unsigned int>(fps);
    if (::ioctl(fd, VIDIOC_S_PARM, &parm) != 0) {
        std::cerr << "[FAYS-CALIB] WARN VIDIOC_S_PARM(" << device
                  << ", " << fps << "fps) failed: "
                  << std::strerror(errno) << "\n";
        ::close(fd);
        return false;
    }
    ::close(fd);
    std::cerr << "[FAYS-CALIB] v4l2 stereo fps=" << fps
              << " device=" << device << "\n";
    return true;
}

// ============================================================================
int main(int argc, char** argv) {
    std::set_terminate(terminateWithDiagnostics);
    if (argc < 4) {
        std::cerr << "Usage: " << argv[0] << " <vocab> <orb.yaml> <cam.yaml> [traj.txt]\n";
        return 1;
    }
    std::string vp=argv[1], oc=argv[2], cc=argv[3], tf=(argc>=5)?argv[4]:"";
    for (const auto& required : {vp, oc, cc}) {
        std::ifstream stream(required);
        if (!stream.good()) {
            std::cerr << "[ERR] Required runtime YAML/vocabulary does not exist "
                      << "or is unreadable: " << required << "\n";
            return 1;
        }
    }
    if (!configureIpcPaths()) return 1;
    const char* fake_imu_env = std::getenv("KSQ_FAYS_FAKE_IMU");
    g_fake_imu = fake_imu_env && std::strcmp(fake_imu_env, "1") == 0;
    if (g_fake_imu) {
        std::cerr
            << "[FAKE-IMU] DEBUG ONLY: 3s excitation, then constant "
            << "acc=(0,0,9.80665) gyro=(0,0,0) at 1000 Hz\n";
    }

    signal(SIGINT, sigint_handler); signal(SIGTERM, sigint_handler);
    // Must run before the SDK and any ORB thread exists: the handler is only
    // async-signal-safe, and warming up backtrace() has to happen while the
    // process still has exactly one thread and no locks held.
    installFatalSignalHandlers();
    atexit(do_cleanup);
    try { g_debug_capture.start(); }
    catch (const std::exception& error) {
        std::cerr << "[Connect-Debug] ERROR start failed: " << error.what() << "\n";
        return 1;
    }

    // SDK 不会自动应用配置里的 stereo_fps（SetStereoFPS 未开放），
    // 因此先在 V4L2 层把双目节点设为目标帧率，再打开 SDK。
    {
        const std::string stereo_dev = readConfigStereoDevPort(cc);
        const int stereo_fps = readConfigStereoFps(cc);
        setV4l2StereoFps(stereo_dev, stereo_fps);
    }

    // 先打开 SDK 并读取这台机器内的出厂标定，再生成本次运行专用的 ORB
    // 配置。ORB-SLAM 必须在这之后构造，否则只会使用启动参数中的静态模板值。
    g_sdk_config = cc;
    void* h = nullptr;
    if (FAYS_VIK_CreateHandleWithConfig(&h, cc.c_str()) != EXIT_SUCCESS) {
        std::cerr << "[ERR] Camera open failed\n"; return 1;
    }
    g_handle = h;
    // SDK 不会在此路径自动应用设备配置里的 stereo_fps；保留官方调用
    // 作为能力探测。当前 3.5.2-r1 x86_64 库将该接口标为未开放并直接
    // 返回失败，因此它不能改变当前设备的实际流速。
    {
        const int stereo_fps = readConfigStereoFps(cc);
        if (FAYS_VIK_SetStereoFPS(h, stereo_fps) != EXIT_SUCCESS) {
            std::cerr << "[FAYS-CALIB] WARN SetStereoFPS("
                      << stereo_fps << ") failed\n";
        } else {
            std::cerr << "[FAYS-CALIB] stereo_fps="
                      << stereo_fps << "\n";
        }
    }
    const std::string run_directory = currentDirectory();
    std::string calibration_directory;
    RuntimeCalibration runtime_calibration;
    if (!prepareFactoryCalibrationDirectory(run_directory, &calibration_directory) ||
        !configureSdkFactoryCalibration(
            h, calibration_directory, &runtime_calibration)) {
        do_cleanup();
        return 1;
    }
    const std::string runtime_orb_yaml = calibration_directory +
        "/orb_runtime_factory_" + runtime_calibration.serial + "_" +
        std::to_string(getpid()) + ".yaml";
    if (!writeRuntimeOrbYaml(oc, runtime_orb_yaml, runtime_calibration)) {
        do_cleanup();
        return 1;
    }

    // 不启动 Pangolin/HighGUI Viewer；Current Frame 由 FrameDrawer 直接
    // 输出到上位机主界面，避免地图、关键帧和独立窗口的渲染开销。
    ORB_SLAM3::System SLAM(
        vp, runtime_orb_yaml, ORB_SLAM3::System::IMU_STEREO, false, 0, tf);

    PrintDeviceInfo(h);
    std::cout << "SDK:" << FAYS_VIK_GetVersion(h) << "\n";

    // 非阻塞 stdin (Enter 检测)
    int flags = fcntl(STDIN_FILENO, F_GETFL, 0);
    fcntl(STDIN_FILENO, F_SETFL, flags | O_NONBLOCK);

    // 注册回调
    FAYS_VIK_RegisterStereoImageCallback(h, stereoCallback);
    FAYS_VIK_RegisterImuCallback(h, imuCallback);
    std::thread raw_stream_thread(rawStreamServer);

    // Enter-to-start 状态和轨迹文件由CPU8输出线程串行维护。
    std::cout << "\n[INIT] Waiting for IMU initialization...\n"
              << "       Move camera for 2s, then press ENTER.\n\n";

    if (!tf.empty()) {
        g_pose_output.trajectory.open(tf);
        g_pose_output.trajectory << std::fixed;
    }
    std::string tf_raw = tf.empty() ? "" : tf.substr(0,tf.size()-4)+"_raw.txt";
    if (!tf_raw.empty()) {
        g_pose_output.raw_trajectory.open(tf_raw);
        g_pose_output.raw_trajectory << std::fixed;
    }
    std::cout << "[FILE] traj=" << tf << "\n[FILE] raw=" << tf_raw << "\n";
    // 等第一帧到达后打印图像信息 (在 stereoCallback 里记录)
    { int wait=0; while(g_running && g_first_img && wait<300) { std::this_thread::sleep_for(std::chrono::milliseconds(10)); wait++; }
      std::cout << "[IMAGE] SDK callback: " << g_img_w << "x" << g_img_h << " ch=" << g_img_ch
                << " (left=" << g_img_w << "x" << (g_img_h/2) << " right=" << g_img_w << "x" << (g_img_h/2) << ")\n"; }

    // ORB-SLAM3及SDK后台线程继承启动时CPU9域；Viewer 已禁用。此后同步
    // TrackStereo主线程和输出线程固定CPU8；CPU5预处理线程自行绑定。
    // Frame内部两个ORB提取线程由ORB核心读取启动器环境变量后固定CPU6/7。
    if (!bindFaysStageThread(
            "fays-track", "track", configuredFaysTrackCpu())) {
        g_running = false;
    }
    std::thread preprocess_thread(preprocessWorker);
    std::thread output_thread(outputWorker);

    auto t0 = std::chrono::steady_clock::now();
    auto fps_t0 = t0;
    unsigned long long fps_last_imu =
        g_imu_input_count.load(std::memory_order_relaxed);
    FaysTimingTotals fps_last_timing;
    {
        std::lock_guard<std::mutex> lk(g_timing_mutex);
        fps_last_timing = g_timing_totals;
    }
    int frame_n=0;
    std::atomic<bool> mp_cleanup_running{false};
    double next_mp_cleanup_s = kMpCleanupFirstTriggerS;
    double next_inactive_cleanup_retry_s = 0.0;
    std::thread mp_cleanup_thread;

    while (!SLAM.isShutDown() && g_running) {
        const auto wait_started = std::chrono::steady_clock::now();
        PreparedFrame prepared;
        {
            std::unique_lock<std::mutex> lk(g_prepared_mutex);
            while (g_running && !g_prepared_ready) {
                g_prepared_cv.wait_for(lk, std::chrono::milliseconds(10));
            }
            if (!g_running) break;
            prepared = std::move(g_prepared_frame);
            g_prepared_ready = false;
        }
        g_prepared_cv.notify_all();
        const double frame_wait_ms = std::chrono::duration<double, std::milli>(
            std::chrono::steady_clock::now() - wait_started).count();

        const double ts = prepared.ts;
        const uint64_t frame_host_ns = prepared.host_mono_ns;
        cv::Mat L = std::move(prepared.left);
        cv::Mat R = std::move(prepared.right);

        // CPU5已完成图像和IMU准备；CPU8从TrackStereo入口开始计入中间计算。
        std::vector<ORB_SLAM3::IMU::Point> vImu = std::move(prepared.imu);
        const unsigned long long imu_samples =
            static_cast<unsigned long long>(vImu.size());
        const auto middle_started = std::chrono::steady_clock::now();
        Sophus::SE3f Tcw;
        try {
            Tcw = SLAM.TrackStereo(L, R, ts, vImu);
        } catch (...) {
            std::cerr << "[TRACK_ERROR] phase=TrackStereo"
                      << " frame=" << frame_n
                      << " timestamp=" << std::fixed
                      << std::setprecision(6) << ts
                      << " imu_samples=" << imu_samples << "\n";
            reportNativeException("TrackStereo", std::current_exception());
            printNativeBacktrace("TrackStereo");
            g_running = false;
            break;
        }
        const auto post_started = std::chrono::steady_clock::now();
        const double middle_ms = std::chrono::duration<double, std::milli>(
            post_started - middle_started).count();

        // Current Frame 保留原 Viewer 的左目、跟踪点和状态文字，但只按
        // 主界面显示频率生成；不再生成未使用的稀疏深度和地图视图。
        OutputFrame output;
        output.ts = ts;
        output.host_mono_ns = frame_host_ns;
        output.frame_id = frame_n;
        output.wait_ms = frame_wait_ms;
        output.preprocess_ms = prepared.preprocess_ms;
        output.middle_ms = middle_ms;
        output.imu_samples = imu_samples;
        output.tracking_state = SLAM.GetTrackingState();
        output.map_changed = SLAM.MapChanged();
        output.Tcw = Tcw;
        static auto last_current_frame =
            std::chrono::steady_clock::time_point{};
        const auto current_frame_now = std::chrono::steady_clock::now();
        if (current_frame_now - last_current_frame >=
                std::chrono::milliseconds(50)) {
            last_current_frame = current_frame_now;
            output.current_frame = SLAM.DrawCurrentFrame();
        }

        output.post_main_ms = std::chrono::duration<double, std::milli>(
            std::chrono::steady_clock::now() - post_started).count();
        if (!enqueueOutput(std::move(output))) break;

        auto now=std::chrono::steady_clock::now();
        double el=std::chrono::duration<double>(now-t0).count();
        ++frame_n;
        const unsigned long long queued_bad_mp =
            ORB_SLAM3::GetQueuedBadMapPointPayloadCount();
        const unsigned long long queued_bad_kf =
            ORB_SLAM3::GetQueuedBadKeyFramePayloadCount();
        const bool periodic_cleanup_due = el >= next_mp_cleanup_s;
        const bool inactive_high_water =
            queued_bad_mp >= kInactiveCleanupHighWater;
        const bool threshold_cleanup_due =
            inactive_high_water && el >= next_inactive_cleanup_retry_s;
        if ((periodic_cleanup_due || threshold_cleanup_due) &&
            !mp_cleanup_running.exchange(true, std::memory_order_acq_rel)) {
            if (mp_cleanup_thread.joinable())
                mp_cleanup_thread.join();
            if (periodic_cleanup_due) {
                do {
                    next_mp_cleanup_s += kMpCleanupIntervalS;
                } while (next_mp_cleanup_s <= el);
            }
            if (threshold_cleanup_due)
                next_inactive_cleanup_retry_s =
                    el + kInactiveCleanupRetryS;
            const std::string cleanup_reason = threshold_cleanup_due
                ? "threshold" : "periodic";
            try {
                mp_cleanup_thread = std::thread([
                        el, cleanup_reason, queued_bad_mp, queued_bad_kf,
                        &mp_cleanup_running]() {
                    try {
                        bindFaysStageThread(
                            "inactive-clean", "background",
                            kFaysCleanupCpu);
                        const unsigned long long rss_before = currentRssKb();
                        const auto started =
                            std::chrono::steady_clock::now();
                        const ORB_SLAM3::BadMapPointCleanupStats mp_stats =
                            ORB_SLAM3::CompactBadMapPointPayloads(
                                kMpCleanupGraceS,
                                kInactiveCleanupBatch);
                        const ORB_SLAM3::BadKeyFrameCleanupStats kf_stats =
                            ORB_SLAM3::CompactBadKeyFramePayloads(
                                kKfCleanupGraceS,
                                kInactiveCleanupBatch);
                        const double duration_ms =
                            std::chrono::duration<double, std::milli>(
                                std::chrono::steady_clock::now() -
                                started).count();
                        const unsigned long long rss_after = currentRssKb();
                        std::cerr << "[MP_CLEANUP]"
                                  << " passes=" << mp_stats.passes
                                  << " candidates=" << mp_stats.candidates
                                  << " compacted=" << mp_stats.compacted
                                  << " queued=" << mp_stats.queued
                                  << " duration_ms=" << std::fixed
                                  << std::setprecision(3) << duration_ms
                                  << " rss_before_kb=" << rss_before
                                  << " rss_after_kb=" << rss_after
                                  << " trigger_elapsed_s="
                                  << std::setprecision(3) << el
                                  << " reason=" << cleanup_reason
                                  << " threshold="
                                  << kInactiveCleanupHighWater
                                  << " mp_queued_before=" << queued_bad_mp
                                  << " mp_queued_after=" << mp_stats.queued
                                  << " kf_candidates=" << kf_stats.candidates
                                  << " kf_compacted=" << kf_stats.compacted
                                  << " kf_queued_before=" << queued_bad_kf
                                  << " kf_queued_after=" << kf_stats.queued
                                  << "\n";
                    } catch (const std::exception& error) {
                        std::cerr << "[MP_CLEANUP_ERR] "
                                  << error.what() << "\n";
                    }
                    mp_cleanup_running.store(
                        false, std::memory_order_release);
                });
            } catch (const std::system_error& error) {
                mp_cleanup_running.store(false, std::memory_order_release);
                std::cerr << "[MP_CLEANUP_ERR] thread_start="
                          << error.what() << "\n";
            }
        }
        if (frame_n%100==0)
            std::cerr<<"[STAT] frames="<<frame_n<<" fps="<<std::fixed<<std::setprecision(1)<<(frame_n/el)<<"\n";

        // 原始统计每秒送到 Python；上位机也按 1 秒窗口统一展示。
        double fps_elapsed=std::chrono::duration<double>(now-fps_t0).count();
        if (fps_elapsed>=1.0) {
            unsigned long long imu_total=
                g_imu_input_count.load(std::memory_order_relaxed);
            FaysTimingTotals timing_total;
            {
                std::lock_guard<std::mutex> lk(g_timing_mutex);
                timing_total = g_timing_totals;
            }
            const unsigned long long process_delta =
                timing_total.samples - fps_last_timing.samples;
            double image_fps=slidingRate(g_stereo_rate_window, now);
            double imu_hz=(imu_total-fps_last_imu)/fps_elapsed;
            double process_fps=slidingRate(g_process_rate_window, now);
            const auto stage_average = [process_delta](
                    double current, double previous) {
                return process_delta > 0
                    ? std::max(0.0, current - previous) / process_delta
                    : 0.0;
            };
            const double wait_ms = stage_average(
                timing_total.wait_ms, fps_last_timing.wait_ms);
            const double preprocess_ms = stage_average(
                timing_total.preprocess_ms, fps_last_timing.preprocess_ms);
            const double middle_avg_ms = stage_average(
                timing_total.middle_ms, fps_last_timing.middle_ms);
            const double post_ms = stage_average(
                timing_total.post_ms, fps_last_timing.post_ms);
            std::cerr<<"[FPS_DATA] image="<<std::fixed<<std::setprecision(3)<<image_fps
                     <<" imu="<<imu_hz<<" process="<<process_fps
                     <<std::setprecision(2)
                     <<" wait_ms="<<wait_ms
                     <<" preprocess_ms="<<preprocess_ms
                     <<" middle_ms="<<middle_avg_ms
                     <<" post_ms="<<post_ms<<"\n";
            const ORB_SLAM3::RuntimeMapDiagnostics diagnostic =
                ORB_SLAM3::GetRuntimeMapDiagnostics();
            const HeapBreakdown heap = currentHeapBreakdown();
            std::ostringstream diagnostic_line;
            diagnostic_line
                << "[ORB_MAP_DIAG]"
                << " state=" << diagnostic.state
                << " inliers=" << diagnostic.inliers
                << " maps=" << diagnostic.maps
                << " active_kf=" << diagnostic.activeKf
                << " active_mp=" << diagnostic.activeMp
                << " local_kf=" << diagnostic.localKf
                << " local_mp=" << diagnostic.localMp
                << " lm_queue=" << diagnostic.lmQueue
                << " kf_created=" << diagnostic.kfCreated
                << " mp_created=" << diagnostic.mpCreated;
            std::cerr << diagnostic_line.str() << "\n";
            // Deliberately a separate record.  protocol.py's
            // parse_orb_map_diagnostic_line rejects the line unless its field
            // set matches ORB_MAP_DIAGNOSTIC_SPECS exactly, so an extra field
            // on [ORB_MAP_DIAG] silently drops the whole diagnostic (and with
            // it the CSV telemetry the panel reads).  A second record keeps
            // this instrument decoupled from the Python schema: either side
            // can be rolled back without blinding the other.
            std::cerr << "[ORB_HEAP]"
                      << " in_use_kb=" << heap.inUseKb
                      << " arena_kb=" << heap.arenaKb
                      << " mmap_kb=" << heap.mmapKb
                      << " free_kb=" << heap.freeKb
                      << " kf_created=" << diagnostic.kfCreated
                      << " mp_created=" << diagnostic.mpCreated
                      << "\n";
            fps_last_imu=imu_total;
            fps_last_timing=timing_total;
            fps_t0=now;
        }
    }

    g_running=false;
    g_img_cv.notify_all();
    g_prepared_cv.notify_all();
    g_output_cv.notify_all();
    // Release camera input before waiting for ORB workers or map cleanup.
    do_cleanup();
    if (mp_cleanup_thread.joinable()) mp_cleanup_thread.join();
    if (preprocess_thread.joinable()) preprocess_thread.join();
    if (raw_stream_thread.joinable()) raw_stream_thread.join();
    if (output_thread.joinable()) output_thread.join();
    SLAM.Shutdown();
    if (g_pose_output.trajectory.is_open()) g_pose_output.trajectory.close();
    if (g_pose_output.raw_trajectory.is_open()) {
        g_pose_output.raw_trajectory.close();
    }
    std::cerr << "[FAYS-QUEUE] stereo_enqueued="
              << g_stereo_enqueued_count.load(std::memory_order_relaxed)
              << " stereo_dropped_new="
              << g_stereo_dropped_count.load(std::memory_order_relaxed)
              << " stereo_frameselect_dropped="
              << g_stereo_frameselect_dropped_count.load(
                     std::memory_order_relaxed)
              << " time_drop="
              << g_time_drop_count.load(std::memory_order_relaxed)
              << " output_dropped_new="
              << g_output_dropped_count.load(std::memory_order_relaxed)
              << " mode=drop-new"
              << "\n";
    std::cout<<"[SLAM] Done.\n"; return 0;
}
