"""Compile/test the opt-in diagnostic shim against a fake SDK, never hardware."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
DIAG = ROOT / 'ORB-SLAM/Examples/fays/diagnostics'
SDK = ROOT / 'FaysSense_VI_Kit_Release/include'
spec = importlib.util.spec_from_file_location('input_trace_analysis', DIAG / 'analyze_input_trace.py')
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)

FAKE = r'''
#include "fays_atrak/fays_atrak_types.h"
#include <functional>
#include <sys/ioctl.h>
#include <linux/videodev2.h>
#include <cstdarg>
#include <vector>
#include <cerrno>
#include <unistd.h>
#include <cstdlib>
extern "C" int ioctl(int, unsigned long req, ...) noexcept {
 va_list a; va_start(a,req); auto b=va_arg(a,v4l2_buffer*); va_end(a);
 static unsigned seq=0;
 b->sequence=++seq; b->timestamp.tv_sec=1; b->timestamp.tv_usec=seq*20000;
 b->flags = seq == 3 ? V4L2_BUF_FLAG_ERROR : 0;
 return 0;
}
extern "C" bool read_frame(void*, int fd, std::vector<void*>&, AtrakImage* image, unsigned long& ts)
asm("_ZN17FaysActiveTracker5ViKit9ReadFrameEiRSt6vectorIPvSaIS2_EEP10AtrakImageRm");
extern "C" bool read_frame(void*, int fd, std::vector<void*>&, AtrakImage* image, unsigned long& ts) {
 if (fd < 0) { errno=EIO; return false; }
 v4l2_buffer buffer{}; if (ioctl(fd,VIDIOC_DQBUF,&buffer)) std::abort();
 usleep(3000); ts += 20000000; image->timestamp=ts; ++image->seq; errno=0; return true;
}
int FAYS_VIK_RegisterStereoImageCallback(void* h, std::function<void(AtrakImage*)> cb) {
 if (!cb) return 23;
 unsigned char byte=7; AtrakImage im{}; im.data=&byte;
 unsigned long ts=1000000000; std::vector<void*> b;
 for (int i=0; i<5; ++i) {
  if (!read_frame(h, 7, b, &im, ts)) std::abort();
  cb(&im);
 }
 if (read_frame(h, -1, b, &im, ts) || errno != EIO) std::abort();
 if (byte != 12) std::abort();
 return 17;
}
'''
APP = r'''
#include "fays_atrak/fays_atrak_types.h"
#include <functional>
#include <unistd.h>
#include <cstdio>
#include <cstdlib>
int FAYS_VIK_RegisterStereoImageCallback(void*, std::function<void(AtrakImage*)>);
int main() {
 int count=0; void* h=reinterpret_cast<void*>(123);
 if (FAYS_VIK_RegisterStereoImageCallback(h, {}) != 23) return 2;
 int status=FAYS_VIK_RegisterStereoImageCallback(h,[&](AtrakImage* im) {
   ++count; ++*im->data;
   if (im->seq == 3) usleep(15000);
 });
 if (status != 17 || count != 5) return 3;
 if (std::getenv("FAKE_FORCE_EXIT")) _exit(0);
 std::puts("unchanged: status=17 callbacks=5 pixels=12");
}
'''


@unittest.skipUnless(shutil.which('c++'), 'C++ compiler needed for shim ABI test')
class TraceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='ksq-input-trace-test-', dir='/var/tmp')
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.base = Path(cls.tmp.name)
        (cls.base / 'fake.cc').write_text(FAKE)
        (cls.base / 'app.cc').write_text(APP)
        common = ['c++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(SDK)]
        commands = [
            common + ['-shared', '-fPIC', str(DIAG/'input_trace.cc'), '-ldl', '-o', str(cls.base/'trace.so')],
            common + ['-shared', '-fPIC', str(cls.base/'fake.cc'), '-o', str(cls.base/'libfake.so')],
            common + [str(cls.base/'app.cc'), '-L'+str(cls.base), '-lfake', '-Wl,-rpath,'+str(cls.base), '-o', str(cls.base/'app')],
        ]
        for command in commands:
            subprocess.run(command, check=True, capture_output=True, timeout=30)

    def run_trace(self, enabled=True, force_exit=False):
        d = self.base/self._testMethodName
        d.mkdir()
        env = dict(os.environ, LD_PRELOAD=str(self.base/'trace.so'))
        env.pop('KSQ_FAYS_TRACE_DIR', None)
        if enabled:
            env['KSQ_FAYS_TRACE_DIR'] = str(d)
        if force_exit:
            env['FAKE_FORCE_EXIT'] = '1'
        result = subprocess.run([str(self.base/'app')], env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result, list(d.glob('*.bin'))

    def test_enabled_preserves_calls_pixels_return_values_and_errno(self):
        result, paths = self.run_trace()
        self.assertIn('unchanged:', result.stdout)
        self.assertEqual(len(paths), 1)
        report = analysis.analyze(paths[0])
        self.assertEqual(report['events'], 16)
        self.assertFalse(report['saturated'])
        cb = next(g for g in report['groups'] if g['kind'] == 'callback')
        self.assertEqual(cb['count'], 5)
        self.assertEqual(cb['sensor_interval_histogram_ms'], {20.0: 4})
        self.assertGreater(cb['wall_ms']['max'], 10)
        self.assertGreater(cb['non_cpu_ms']['max'], 10)
        dq = next(g for g in report['groups'] if g['kind'] == 'dqbuf')
        self.assertEqual(dq['buffer_error_flag_count'], 1)
        self.assertEqual(dq['buffer_sequence_missing'], 0)
        self.assertEqual(sum(g['failures'] for g in report['groups']), 1)
        json.dumps(report)  # Persistable without numpy/custom types.

    def test_disabled_creates_no_trace(self):
        result, paths = self.run_trace(enabled=False)
        self.assertIn('unchanged:', result.stdout)
        self.assertFalse(paths)
        self.assertNotIn('FAYS-INPUT-TRACE', result.stderr)

    def test_shared_mapping_survives_exit_without_destructors(self):
        _, paths = self.run_trace(force_exit=True)
        self.assertEqual(analysis.analyze(paths[0])['events'], 16)

    def test_analyzer_rejects_truncated_file(self):
        path = self.base/'bad.bin'
        path.write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError, 'truncated'):
            analysis.analyze(path)


if __name__ == '__main__':
    unittest.main()
