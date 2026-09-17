"""No hardware: signal safety, bounded SDK fd ownership and fail-closed timeout."""
from contextlib import nullcontext
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]        # core/gripper/orb_slam_src/
# 上位机那侧的源码（gripper_version1/、scripts/）只在 online/ 里、不入库。本树里
# 没有它们，所以纯 clone 上整模块跳过而不是报错 —— 迁过来的是**读 ORB 源码**那
# 部分契约，上位机部分只是同模块里的邻居。
LEGACY = ROOT.parents[2] / "online"
if not (LEGACY / "gripper_version1").is_dir():
    raise unittest.SkipTest("需要 online/ 的夹爪上位机源码（该目录不入库）")
sys.path.insert(0, str(LEGACY / 'gripper_version1'))
import fays_serial_probe


class FaysSdkShutdownTests(unittest.TestCase):
    def compile_run(self, source):
        if not shutil.which('g++'):
            self.skipTest('C++ compiler unavailable')
        with tempfile.TemporaryDirectory() as d:
            cpp = Path(d) / 'test.cc'
            binary = Path(d) / 'test'
            cpp.write_text(source)
            subprocess.run(['g++', '-std=c++14', '-pthread', '-I', str(ROOT / 'ORB-SLAM/Examples/fays'), str(cpp), '-o', str(binary)], check=True, capture_output=True)
            return subprocess.run([str(binary), d], check=True, capture_output=True, text=True, timeout=5)

    def test_signal_handler_never_reenters_sdk_or_orb(self):
        source = (ROOT / 'ORB-SLAM/Examples/fays/fayssense_orb_slam.cc').read_text()
        handler = source.split('void sigint_handler(int) {', 1)[1].split('\n}', 1)[0]
        for forbidden in ('Shutdown(', 'DestroyHandle(', 'do_cleanup(', 'notify_', 'join('):
            self.assertNotIn(forbidden, handler)
        self.compile_run('''#include <atomic>
#include <csignal>
#include <unistd.h>
#include <cassert>
std::atomic<bool> g_running{true};
void sigint_handler(int) {''' + handler + '''
}
int main() {
    signal(SIGTERM, sigint_handler);
    raise(SIGTERM);
    assert(!g_running.load());
    raise(SIGTERM);
    assert(!g_running.load());
}
''')
        cleanup = source[source.index('    g_running=false;\n    g_img_cv.notify_all();'):]
        self.assertLess(cleanup.index('do_cleanup();'), cleanup.index('SLAM.Shutdown();'))

    def test_streamoff_only_uses_existing_matching_process_fd(self):
        self.compile_run(r'''#include <sys/stat.h>
#include <sys/ioctl.h>
#include <linux/videodev2.h>
#include <fcntl.h>
#include <unistd.h>
#include <cassert>
#include <cstring>
#include <cerrno>
#include <fstream>
#include <string>
int sdk_fd = -1, calls = 0;
bool present = false;
int fake_stat(const char* path, struct stat* value) {
    assert(std::string(path) == "/dev/video2");
    *value = {}; value->st_mode = S_IFCHR; value->st_rdev = 42; return 0;
}
int fake_fstat(int fd, struct stat* value) {
    *value = {}; value->st_mode = S_IFCHR;
    value->st_rdev = present && fd == sdk_fd ? 42 : 99; return 0;
}
int fake_ioctl(int fd, unsigned long request, void* arg) {
    assert(fd != sdk_fd && fcntl(fd, F_GETFD) >= 0);
    assert(request == VIDIOC_STREAMOFF);
    assert(*static_cast<v4l2_buf_type*>(arg) == V4L2_BUF_TYPE_VIDEO_CAPTURE);
    ++calls;
    if (calls == 1) { errno = EINTR; return -1; }
    return 0;
}
#define stat(...) fake_stat(__VA_ARGS__)
#define fstat(...) fake_fstat(__VA_ARGS__)
#define ioctl(...) fake_ioctl(__VA_ARGS__)
#include "fays_sdk_shutdown.h"
int main(int argc, char** argv) {
    const std::string config = std::string(argv[1]) + "/sdk.yaml";
    auto write_config = [&](const std::string& value) {
        std::ofstream out(config); out << "imu_dev_port: " << value << "\n";
    };
    for (auto value : {"NULL", "/dev/ttyACM0", "/dev/video2bad", "/dev/video", "../dev/video2"}) {
        write_config(value); ksq_fays::stopOwnedImuStream(config); assert(calls == 0);
    }
    write_config("\"/dev/video2\" # comment");
    sdk_fd = open("/dev/null", O_RDONLY);
    assert(sdk_fd >= 0);
    ksq_fays::stopOwnedImuStream(config); assert(calls == 0);
    present = true;
    ksq_fays::stopOwnedImuStream(config); assert(calls == 2);
    assert(fcntl(sdk_fd, F_GETFD) >= 0); // original descriptor belongs to SDK
    close(sdk_fd);
}
''')

    def test_timeout_with_valid_serial_still_fails_and_keeps_teardown_diagnostic(self):
        error = subprocess.TimeoutExpired(['probe'], 1,
            output=b'device.serial=3500000262300089\n',
            stderr=b'[FAYS-CLEANUP] destroying SDK\n')
        with mock.patch.object(fays_serial_probe, 'render_probe_config', return_value='/tmp/test-sdk.yaml'), \
                mock.patch.object(fays_serial_probe, 'device_access_guard', return_value=nullcontext()), \
                mock.patch.object(fays_serial_probe, 'fays_device_guard', return_value=nullcontext()), \
                mock.patch.object(fays_serial_probe.subprocess, 'run', side_effect=error):
            with self.assertRaises(RuntimeError) as raised:
                fays_serial_probe.probe_product_serial(
                    {'stereo_dev_port': '/dev/video0', 'imu_dev_port': '/dev/video2'},
                    probe_binary='/bin/true', timeout=1)
        self.assertIn('资源释放', str(raised.exception))
        self.assertIn('destroying SDK', str(raised.exception))
        self.assertIs(raised.exception.__cause__, error)

    def test_stop_keeps_native_cleanup_logs_without_queueing_ui_events(self):
        from io import StringIO
        import json
        import queue
        import threading
        from types import SimpleNamespace
        from slam.process_controller import SlamProcessController
        controller = object.__new__(SlamProcessController)
        controller._clock = lambda: 1.0
        controller._wall_clock = lambda: 2.0
        lines = ['[FAYS-CLEANUP] sdk_destroyed result=0', '[SLAM] Done.']
        process = SimpleNamespace(stdout=StringIO('\n'.join(lines) + '\n'))
        log = StringIO()
        stopped = threading.Event(); stopped.set()
        events = queue.Queue(maxsize=1)
        controller._drain_stdout(process, events, stopped, log)
        self.assertTrue(events.empty())
        self.assertEqual([json.loads(line)['line'] for line in log.getvalue().splitlines()], lines)
