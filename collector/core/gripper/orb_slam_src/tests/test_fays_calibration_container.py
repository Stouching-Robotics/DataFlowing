"""Check the shared calibration container without opening any SDK device."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'ORB-SLAM/Examples/fays/fayssense_orb_slam.cc'


class CalibrationContainerTests(unittest.TestCase):
    def test_both_artifacts_use_same_container(self):
        source = SOURCE.read_text()
        self.assertIn('const std::string dump_directory = calibration_directory +', source)
        self.assertIn('const std::string runtime_orb_yaml = calibration_directory +', source)
        self.assertIn('h, calibration_directory, &runtime_calibration)', source)
        self.assertIn('vp, runtime_orb_yaml, ORB_SLAM3::System::IMU_STEREO', source)
        self.assertNotIn('const std::string dump_directory = run_directory +', source)
        self.assertNotIn('const std::string runtime_orb_yaml = run_directory +', source)

    @unittest.skipUnless(shutil.which('g++'), 'C++ compiler unavailable')
    def test_actual_directory_helper(self):
        source = SOURCE.read_text()
        start = source.index('static bool prepareFactoryCalibrationDirectory(')
        end = source.index('static std::string findLiveDump(', start)
        with tempfile.TemporaryDirectory(prefix='ksq-calib-test-', dir='/var/tmp') as tmp:
            root = Path(tmp)
            cpp = root / 'test.cc'
            cpp.write_text('''#include <string>
#include <iostream>
#include <cstring>
#include <cerrno>
#include <sys/stat.h>
''' + source[start:end] + '''
int main(int argc, char** argv) {
    if(argc != 2) return 2;
    std::string directory;
    if(!prepareFactoryCalibrationDirectory(argv[1], &directory)) return 1;
    std::cout << directory;
}
''')
            binary = root / 'probe'
            subprocess.run(['g++', '-std=c++14', str(cpp), '-o', str(binary)], check=True, capture_output=True)
            run = root / 'run with spaces'; run.mkdir()
            expected = run / 'fays_factory_calib'
            for _ in range(2):
                result = subprocess.run([str(binary), str(run)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, str(expected))
                self.assertTrue(expected.is_dir())
            self.assertEqual(expected.stat().st_mode & 0o777, 0o700)
            expected.rmdir(); expected.write_text('do not overwrite')
            self.assertNotEqual(subprocess.run([str(binary), str(run)], capture_output=True).returncode, 0)
            self.assertEqual(expected.read_text(), 'do not overwrite')
            self.assertNotEqual(subprocess.run([str(binary), ''], capture_output=True).returncode, 0)


if __name__ == '__main__':
    unittest.main()
