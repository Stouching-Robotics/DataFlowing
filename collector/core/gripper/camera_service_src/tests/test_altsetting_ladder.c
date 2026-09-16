/* Deterministic tests for the altsetting ladder. No USB devices involved.
 *
 * The ladder is the thing that lets one binary work both on controllers that
 * can carry DECXIN's high-bandwidth isochronous setting and on those that
 * cannot, so its two failure modes are worth pinning down:
 *
 *   - silently accepting a pair the camera cannot do. alt and payload are one
 *     unit (alt7's endpoint *is* 1280 B/packet); pairing 7 with 944 is exactly
 *     the configuration that took the whole gripper down on 2026-09-15 with
 *     "unsupported DECXIN mode" -> rc=2.
 *   - silently refusing to step down, which leaves the camera wedged at a
 *     setting its controller cannot carry.
 *   - stepping down once and then jamming, because the counter that triggered
 *     the step is never cleared back below its threshold: the comparison is an
 *     equality against the threshold, so a counter left standing on it never
 *     fires again and every rung below the second is unreachable.
 *
 * The state-file name is also pinned, because uvc_camera_service.py has to
 * derive the same key in Python to read the result back; the two are only kept
 * in step by this assertion and its Python counterpart.
 */
#undef NDEBUG
#include <assert.h>
#include <dirent.h>
#include <sys/stat.h>
#include <unistd.h>

#define main camera_service_main
#include "../src/camera_service.c"
#undef main

static camera_config_t make_camera(int vid, int pid, int width, int height) {
    camera_config_t config;
    set_defaults(&config);
    snprintf(config.name, sizeof(config.name), "unit_1_test");
    config.vid = vid;
    config.pid = pid;
    config.usb_bus = 1;
    snprintf(config.usb_port_path, sizeof(config.usb_port_path), "2.2.2");
    config.width = width;
    config.height = height;
    config.fps = 30;
    snprintf(config.format, sizeof(config.format), "MJPEG");
    snprintf(config.socket_path, sizeof(config.socket_path), "/tmp/x.sock");
    snprintf(config.usb_serial, sizeof(config.usb_serial), "DECXIN-01");
    snprintf(config.usb_controller, sizeof(config.usb_controller),
             "0000:74:00.4");
    return config;
}

static camera_config_t make_decxin(void) {
    camera_config_t config = make_camera(0x1bcf, 0x2d4f, 1280, 960);
    config.forced_altsetting = 7;
    config.forced_payload = 1280;
    return config;
}

static void test_accepts_every_rung_and_nothing_else(void) {
    camera_config_t config = make_decxin();

    assert(validate_config(&config, 0) == 0); /* alt7/1280, the preferred rung */
    config.forced_altsetting = 6;
    config.forced_payload = 944;
    assert(validate_config(&config, 0) == 0); /* alt6/944, the fallback rung */

    /* Alt and payload are a matched pair, so a mixed pair must be refused even
     * though both numbers appear in the table on their own. */
    config.forced_altsetting = 7;
    config.forced_payload = 944;
    assert(validate_config(&config, 0) == -1);
    config.forced_altsetting = 6;
    config.forced_payload = 1280;
    assert(validate_config(&config, 0) == -1);
    /* A pair nobody has measured must not be accepted on faith either. */
    config.forced_altsetting = 5;
    config.forced_payload = 800;
    assert(validate_config(&config, 0) == -1);

    config = make_camera(0x0c45, 0x636f, 640, 480);
    config.forced_altsetting = 3;
    config.forced_payload = 800;
    assert(validate_config(&config, 0) == 0);
    config.forced_altsetting = 4; /* the pre-2026-08-28 1600 B setting */
    config.forced_payload = 1600;
    assert(validate_config(&config, 0) == -1);
}

static void test_steps_down_one_rung_and_then_stops(void) {
    camera_state_t camera;
    memset(&camera, 0, sizeof(camera));
    camera.config = make_decxin();

    assert(mode_index_for(&camera.config) == 0);
    /* Both counters are driven to non-zero first: stepping down has to clear
     * *each* of them. The thresholds are tested with ==, so a counter left
     * standing at the threshold never fires again and that whole trigger path
     * gets one step and then jams -- the rungs below it might as well not
     * exist. Stalls and failed opens are separate paths and each clears its
     * own; a downgrade is a fresh start for both. */
    camera.stalls_at_mode = 99;
    camera.open_failures_at_mode = 99;
    assert(maybe_downgrade_altsetting(&camera) == 1);
    assert(camera.config.forced_altsetting == 6);
    assert(camera.config.forced_payload == 944);
    assert(mode_index_for(&camera.config) == 1);
    assert(camera.stalls_at_mode == 0);
    assert(camera.open_failures_at_mode == 0);

    /* alt6 is the last rung: a further stall must not invent a worse one. */
    assert(maybe_downgrade_altsetting(&camera) == 0);
    assert(camera.config.forced_altsetting == 6);
    assert(camera.config.forced_payload == 944);

    /* Sightac has a single rung, so it never steps anywhere. */
    camera.config = make_camera(0x0c45, 0x636f, 640, 480);
    camera.config.forced_altsetting = 3;
    camera.config.forced_payload = 800;
    assert(maybe_downgrade_altsetting(&camera) == 0);
    assert(camera.config.forced_altsetting == 3);

    /* A pair that is in no table at all must not be walked off the end. */
    camera.config = make_decxin();
    camera.config.forced_payload = 944; /* 7/944: refused by validate_config */
    assert(mode_index_for(&camera.config) == 2);
    assert(maybe_downgrade_altsetting(&camera) == 0);
}

static void test_downgrade_is_remembered_under_a_stable_key(void) {
    char parent[] = "/tmp/ksq-ladder-test-XXXXXX";
    char state_dir[PATH_MAX];
    char expected[PATH_MAX];
    char contents[128];
    camera_state_t camera;
    FILE *file;
    size_t read_bytes;

    assert(mkdtemp(parent) != NULL);
    /* A subdirectory that does not exist yet: persist_mode_choice creates it. */
    snprintf(state_dir, sizeof(state_dir), "%s/camera_modes", parent);
    memset(&camera, 0, sizeof(camera));
    camera.config = make_decxin();
    snprintf(camera.config.state_dir, sizeof(camera.config.state_dir), "%s",
             state_dir);

    assert(maybe_downgrade_altsetting(&camera) == 1);

    /* Key = vid_pid_controller_serial with everything outside [A-Za-z0-9-]
     * replaced by '_'. uvc_camera_service._mode_state_key must produce this
     * exact string for the learned value to be read back. */
    snprintf(expected, sizeof(expected), "%s/1bcf_2d4f_0000_74_00_4_DECXIN-01.mode",
             state_dir);
    file = fopen(expected, "r");
    assert(file != NULL);
    read_bytes = fread(contents, 1, sizeof(contents) - 1, file);
    contents[read_bytes] = '\0';
    fclose(file);
    assert(strcmp(contents, "altsetting=6\npayload=944\n") == 0);

    /* No temp file may be left behind next to the result. */
    {
        DIR *dir = opendir(state_dir);
        struct dirent *entry;
        int seen = 0;
        assert(dir != NULL);
        while ((entry = readdir(dir)) != NULL) {
            if (entry->d_name[0] == '.') {
                continue;
            }
            ++seen;
            assert(strstr(entry->d_name, ".tmp.") == NULL);
        }
        closedir(dir);
        assert(seen == 1);
    }

    unlink(expected);
    rmdir(state_dir);
    rmdir(parent);
}

static void test_missing_state_dir_is_not_fatal(void) {
    camera_state_t camera;
    memset(&camera, 0, sizeof(camera));
    camera.config = make_decxin();
    /* Empty state_dir means "do not remember anything" and must still step
     * down in memory for the rest of this session. */
    assert(camera.config.state_dir[0] == '\0');
    assert(maybe_downgrade_altsetting(&camera) == 1);
    assert(camera.config.forced_altsetting == 6);
}

int main(void) {
    /* validate_config reports refusals on stderr; that is the production
     * behaviour and is expected here, so keep the test output readable. */
    freopen("/dev/null", "w", stderr);
    test_accepts_every_rung_and_nothing_else();
    test_steps_down_one_rung_and_then_stops();
    test_downgrade_is_remembered_under_a_stable_key();
    test_missing_state_dir_is_not_fatal();
    printf("altsetting ladder: all assertions passed\n");
    return 0;
}
