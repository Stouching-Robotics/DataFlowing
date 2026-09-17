"""Ground-truth evaluation must not conceal scale drift or mocap gaps."""
import importlib.util
from pathlib import Path
import unittest
import numpy as np
from scipy.spatial.transform import Rotation

PATH=Path(__file__).resolve().parents[1]/'ORB-SLAM/Examples/fays/offline/evaluate.py'
spec=importlib.util.spec_from_file_location('offline_evaluation',PATH)
evalmod=importlib.util.module_from_spec(spec);spec.loader.exec_module(evalmod)


class OfflineEvaluationTests(unittest.TestCase):
    def trajectory(self):
        t=np.linspace(0,3,301)
        p=np.column_stack((np.sin(t),np.cos(2*t),t/2))
        q=Rotation.from_euler('z',t/3).as_quat()
        return np.column_stack((t,p,q))

    def test_rigid_pose_transform_has_zero_ate_and_rpe(self):
        gt=self.trajectory();estimate=gt.copy()
        r=Rotation.from_euler('xyz',[.2,.4,-.3]);offset=np.array([4.,-2.,1.])
        estimate[:,1:4]=r.apply(gt[:,1:4])+offset
        estimate[:,4:8]=(r*Rotation.from_quat(gt[:,4:8])).as_quat()
        result,_=evalmod.evaluate_trajectory(estimate,gt)
        for name in ('ate_translation_m','rpe_1s_translation_m','absolute_rotation_deg','rpe_1s_rotation_deg'):
            self.assertLess(result[name]['rmse'],1e-10)

    def test_scale_error_is_reported_not_corrected(self):
        gt=self.trajectory();estimate=gt.copy();estimate[:,1:4]*=2
        result,_=evalmod.evaluate_trajectory(estimate,gt)
        self.assertGreater(result['ate_translation_m']['rmse'],.1)
        self.assertAlmostEqual(result['diagnostic_optimal_scale_not_applied'],.5)

    def test_no_interpolation_through_groundtruth_dropout(self):
        gt=self.trajectory();gt=gt[(gt[:,0]<1)|(gt[:,0]>2)]
        mask,_,_=evalmod.match_truth(np.array([.5,1.5,2.5]),gt)
        np.testing.assert_array_equal(mask,[True,False,True])

    def test_one_second_rpe_uses_nearest_frame_not_only_ceiling(self):
        gt=self.trajectory()
        estimate=gt[::5].copy()
        # Real datasets have timestamp jitter around the nominal frame rate.
        estimate[1:-1,0]+=np.sin(np.arange(len(estimate)-2))*.0001
        result,_=evalmod.evaluate_trajectory(estimate,gt)
        self.assertGreaterEqual(result['rpe_1s_translation_m']['count'],39)

    def test_rejects_non_monotonic_estimate(self):
        gt=self.trajectory();estimate=gt.copy();estimate[4,0]=estimate[3,0]
        with self.assertRaises(ValueError):evalmod.evaluate_trajectory(estimate,gt)

    def test_alignment_cannot_reflect_coordinate_system(self):
        gt=self.trajectory();estimate=gt[:,1:4].copy();estimate[:,0]*=-1
        r,_,_=evalmod.rigid_alignment(estimate,gt[:,1:4])
        self.assertAlmostEqual(np.linalg.det(r),1.)
