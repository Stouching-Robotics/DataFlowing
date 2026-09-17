"""Protect baseline-before-matching and the empty-correspondence boundary."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class StereoBaselineTests(unittest.TestCase):
    def test_rectified_constructor_initializes_baseline_from_current_intrinsics(self):
        source = (ROOT/'ORB-SLAM/src/Frame.cc').read_text()
        start = source.index('Frame::Frame(const cv::Mat &imLeft, const cv::Mat &imRight,')
        end = source.index('Frame::Frame(', start + 12)
        constructor = source[start:end]
        self.assertRegex(constructor, r'mbf\(bf\),\s*mb\(bf / K\.at<float>\(0, 0\)\)')
        self.assertLess(constructor.index('mb(bf /'), constructor.index('ComputeStereoMatches();'))
        self.assertNotIn('mb = mbf/fx;', constructor)
        self.assertIn('!std::isfinite(mb) || mb <= 0.0f', constructor)

    def test_no_matches_has_no_median(self):
        source = (ROOT/'ORB-SLAM/src/Frame.cc').read_text()
        function = source.split('void Frame::ComputeStereoMatches()', 1)[1].split('void Frame::ComputeStereoFromRGBD', 1)[0]
        self.assertLess(function.index('if (vDistIdx.empty()) return;'), function.index('vDistIdx[vDistIdx.size()/2]'))

    def test_diagnostic_uses_storage_poisoning_and_real_frame_constructor(self):
        source = (ROOT/'ORB-SLAM/Examples/fays/diagnostics/stereo_baseline_probe.cc').read_text()
        self.assertIn('offsetof(F,mb)', source)
        self.assertIn('new(&storage) F(', source)
        self.assertIn('valid_depth=', source)
