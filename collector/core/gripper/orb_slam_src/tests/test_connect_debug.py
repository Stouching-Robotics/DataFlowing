from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
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
from runtime.connect_debug import prepare_debug_capture, debug_runtime_environment
from runtime.fays_debug_samples import iter_debug_samples, RAW_IMU, RAW_PACKET_IMU


class ConnectDebugTests(unittest.TestCase):
    def test_per_connect_directory_and_normal_mode_isolation(self):
        with tempfile.TemporaryDirectory() as root:
            a = prepare_debug_capture(root, 0); b = prepare_debug_capture(root, 1)
            self.assertNotEqual(a, b)
            self.assertEqual(json.loads((Path(a)/'request.json').read_text())['slot'], 0)
            original = {'KSQ_FAYS_DEBUG_DIR': 'stale', 'KEEP': 'value'}
            clean = debug_runtime_environment(original, None, general_cpus=(10,11))
            self.assertEqual(clean, {'KEEP': 'value'})
            debug = debug_runtime_environment(original, a, general_cpus=(10,11))
            self.assertEqual(debug['KSQ_FAYS_DEBUG_DIR'], a)
            self.assertEqual(debug['KSQ_FAYS_DEBUG_CPUS'], '10,11')
            self.assertEqual(original['KSQ_FAYS_DEBUG_DIR'], 'stale')

    def test_disk_check_fails_before_capture(self):
        with tempfile.TemporaryDirectory() as root, mock.patch('runtime.connect_debug.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(RuntimeError, '2 GiB'):
                prepare_debug_capture(root, 0)

    def test_debug_button_reuses_connect_and_cannot_disconnect_active_rig(self):
        import ast
        path = LEGACY/'gripper_version1/upper_computer_fays_opencv48.py'
        source = path.read_text(); tree = ast.parse(source)
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_toggle_connection')
        namespace = {}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
        ctx = SimpleNamespace(connection=SimpleNamespace(snapshot=lambda: SimpleNamespace(connecting=False, connected=True)))
        app = SimpleNamespace(_ctx=lambda slot: ctx)
        namespace['_toggle_connection'](app, 0, debug=True) # returns without disconnect
        self.assertIn('self._toggle_connection(s, debug=True)', source)
        native = (ROOT/'ORB-SLAM/Examples/fays/fayssense_orb_slam.cc').read_text()
        self.assertLess(native.index('g_debug_capture.imu(imu)'), native.index('if (!std::isfinite(imu.acc[axis])'))
        self.assertLess(native.index('g_debug_capture.stereo(raw, *img)'), native.index('    pushRawStereo(raw, *img)'))
        self.assertIn('general_cpus=c.policy.roles["general"]', source)


class NativeDebugCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('g++') or not shutil.which('pkg-config'):
            raise unittest.SkipTest('compiler/OpenCV development tools unavailable')
        cls.tmp = tempfile.TemporaryDirectory(prefix='ksq-native-debug-test-')
        root = Path(cls.tmp.name); cls.binary = root/'native'
        bridge = (ROOT/'ORB-SLAM/Examples/fays/fayssense_orb_slam.cc').read_text()
        structs = bridge[bridge.index('static constexpr uint32_t kRawStreamMagic'):bridge.index('#include "fays_debug_capture.h"')]
        source = r'''#include <atomic>
#include <mutex>
#include <thread>
#include <condition_variable>
#include <deque>
#include <vector>
#include <fstream>
#include <iostream>
#include <sstream>
#include <iomanip>
#include <chrono>
#include <cstdlib>
#include <pthread.h>
#include <sched.h>
#include <opencv2/core.hpp>
#include "fays_atrak/fays_atrak_types.h"
''' + structs + r'''
#include "fays_debug_capture.h"
int main(int argc, char** argv) {
    FaysDebugCapture capture;
    capture.start();
    if (std::string(argv[1]) == "duration") {
        std::this_thread::sleep_for(std::chrono::milliseconds(250));
    } else if (std::string(argv[1]) == "overflow") {
        cv::Mat big(8192,4096,CV_8UC1,cv::Scalar(7));
        AtrakImage image{};
        capture.stereo(big,image);
    } else {
        for(int n=0; n<200; ++n) {
            AtrakIMU imu{}; imu.timestamp=1000000000000ULL+n*1000000ULL;
            imu.acc[2]=9.81; imu.gyro[0]=0.0123; capture.imu(imu);
            if (n%4==0) {
                cv::Mat pixels(8,8,CV_8UC1,cv::Scalar(n));
                AtrakImage image{}; image.timestamp=imu.timestamp; image.seq=n/4;
                capture.stereo(pixels,image);
            }
        }
    }
    capture.stop(); capture.stop();
}
'''
        cpp=root/'test.cc';cpp.write_text(source)
        if Path('/usr/local/include/opencv4/opencv2/core.hpp').is_file():
            flags=['-I/usr/local/include/opencv4','-L/usr/local/lib','-Wl,-rpath,/usr/local/lib','-lopencv_core']
        else:
            flags=subprocess.check_output(['pkg-config','--cflags','--libs','opencv4'],text=True).split()
        r=subprocess.run(['g++','-std=c++14','-pthread',str(cpp),'-I',str(ROOT/'ORB-SLAM/Examples/fays'),'-I',str(ROOT/'FaysSense_VI_Kit_Release/include'),*flags,'-o',str(cls.binary)],capture_output=True,text=True)
        if r.returncode: raise RuntimeError(r.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_native(self, directory, mode, enabled=True):
        env=dict(os.environ)
        for k in ('KSQ_FAYS_DEBUG_DIR','KSQ_FAYS_DEBUG_SECONDS','KSQ_FAYS_DEBUG_CPUS'): env.pop(k,None)
        if enabled:
            env.update(KSQ_FAYS_DEBUG_DIR=str(directory),KSQ_FAYS_DEBUG_SECONDS='0.05' if mode=='duration' else '300',KSQ_FAYS_DEBUG_CPUS=str(min(os.sched_getaffinity(0))))
        return subprocess.run([str(self.binary),mode],env=env,capture_output=True,text=True,timeout=10,check=True)

    def test_native_roundtrip_preserves_every_sample(self):
        with tempfile.TemporaryDirectory() as d:
            self.run_native(d,'normal')
            packets=list(iter_debug_samples(d))
            imus=[(h,p) for h,p in packets if h[2]==RAW_PACKET_IMU]
            images=[(h,p) for h,p in packets if h[2]!=RAW_PACKET_IMU]
            self.assertEqual((len(imus),len(images)),(200,50))
            for n,(h,p) in enumerate(imus):
                self.assertEqual(h[6],1000000000000+n*1000000)
                self.assertEqual(RAW_IMU.unpack(p),(0.,0.,9.81,0.0123,0.,0.))
            for n,(h,p) in enumerate(images): self.assertEqual(p,bytes([n*4])*64)
            meta=json.loads((Path(d)/'capture.json').read_text())
            self.assertTrue(meta['complete']);self.assertEqual(meta['imu_samples'],200)
            # Offline export validates the real written binary, not a mock file.
            subprocess.run([sys.executable,str(LEGACY/'scripts/analyze_connect_debug.py'),d],check=True,capture_output=True)
            report=json.loads((Path(d)/'analysis/summary.json').read_text())
            self.assertEqual(report['imu_sensor_timing']['samples'],200)
            self.assertIsNone(report['parse_error'])

    def test_duration_stops_capture_only(self):
        with tempfile.TemporaryDirectory() as d:
            self.run_native(d,'duration')
            meta=json.loads((Path(d)/'capture.json').read_text())
            self.assertEqual(meta['reason'],'duration_limit');self.assertTrue(meta['complete'])

    def test_overflow_is_explicitly_incomplete(self):
        with tempfile.TemporaryDirectory() as d:
            self.run_native(d,'overflow')
            meta=json.loads((Path(d)/'capture.json').read_text())
            self.assertEqual(meta['reason'],'queue_overflow');self.assertFalse(meta['complete'])

    def test_normal_connect_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            self.run_native(d,'normal',enabled=False)
            self.assertEqual(list(Path(d).iterdir()),[])

    def test_truncated_sample_is_not_silently_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d)/'samples-0000.bin').write_bytes(b'broken')
            with self.assertRaisesRegex(ValueError,'truncated header'): list(iter_debug_samples(d))
