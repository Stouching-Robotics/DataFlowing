import math
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from app.processing import JobContext, ModuleSkip
from app.processing.modules.umi_slam_action import UmiSlamActionModule
from app.umi_slam_action import (
    derive_action,
    matrix_to_rotvec,
    quat_to_matrix,
)


def _yaw_quat(angle: float) -> list[float]:
    """绕 Z 轴 angle 弧度的四元数 (x, y, z, w)。"""
    return [0.0, 0.0, math.sin(angle / 2.0), math.cos(angle / 2.0)]


def _pose(x, y, z, quat=None) -> list[float]:
    return [float(x), float(y), float(z)] + (quat or [0.0, 0.0, 0.0, 1.0])


class RotationMathTests(unittest.TestCase):
    def test_identity_and_quarter_turn(self):
        # 单位四元数 → 单位阵 → 零旋转向量
        self.assertTrue(np.allclose(matrix_to_rotvec(quat_to_matrix([0, 0, 0, 1])),
                                    np.zeros(3), atol=1e-9))

        # 绕 X 轴 90°
        q = [math.sin(math.pi / 4), 0.0, 0.0, math.cos(math.pi / 4)]
        self.assertTrue(np.allclose(matrix_to_rotvec(quat_to_matrix(q)),
                                    [math.pi / 2, 0.0, 0.0], atol=1e-6))

    def test_near_180_degrees_is_finite(self):
        """近 180° 时 sin(角)→0,标准轴公式会除零 —— 必须走退化分支。"""
        for angle in (math.pi, math.pi - 1e-7, math.pi - 1e-4):
            q = [0.0, 0.0, math.sin(angle / 2.0), math.cos(angle / 2.0)]
            rotvec = matrix_to_rotvec(quat_to_matrix(q))
            self.assertTrue(np.all(np.isfinite(rotvec)),
                            f"angle={angle} produced non-finite {rotvec}")
            self.assertAlmostEqual(float(np.linalg.norm(rotvec)), angle, places=5)

    def test_zero_quaternion_returns_none(self):
        self.assertIsNone(quat_to_matrix([0.0, 0.0, 0.0, 0.0]))


def _traj(poses, dt=1/30, offset=1000.0):
    """把逐帧位姿包成 slam_trajectory 的格子。

    每格是 ``[slam_t, x, y, z, qx, qy, qz, qw]`` 的整数倍;slam 时间戳与
    数据时间戳差一个常数偏移(采集端时钟独立)。
    """
    return [[ (i * dt + offset) ] + list(pose) for i, pose in enumerate(poses)]


def _stamps(n, dt=1/30):
    return [i * dt for i in range(n)]


class DeriveActionTests(unittest.TestCase):
    def test_pure_translation_matches_analytic_delta(self):
        """沿 +X 每帧 10mm、姿态恒为身份 → Δp 应为 [0.01, 0, 0]。"""
        poses = [_pose(0.01 * i, 0, 0) for i in range(6)]
        out = derive_action(_traj(poses), _stamps(6), [[0.0, 0, 0]] * 6, 6)
        self.assertIsNotNone(out)
        for frame in range(5):
            self.assertTrue(np.allclose(out[frame]["action"][:3], [0.01, 0, 0],
                                        atol=1e-9))
            self.assertTrue(np.allclose(out[frame]["action"][3:6], [0, 0, 0],
                                        atol=1e-9))

    def test_local_frame_delta_follows_current_orientation(self):
        """世界系位移须换算到**当前**夹爪局部系,而不是原样透传。"""
        step = 0.02
        quat = [0.0, 0.0, math.sin(-math.pi / 4), math.cos(-math.pi / 4)]
        poses = [_pose(0.0, -step * frame, 0.0, quat) for frame in range(5)]
        out = derive_action(_traj(poses), _stamps(5), None, 5)
        self.assertTrue(np.allclose(out[0]["action"][:3], [step, 0, 0],
                                    atol=1e-6))

    def test_world_delta_is_not_passed_through_verbatim(self):
        step = 0.02
        quat = [0.0, 0.0, math.sin(-math.pi / 4), math.cos(-math.pi / 4)]
        poses = [_pose(0.0, -step * frame, 0.0, quat) for frame in range(3)]
        out = derive_action(_traj(poses), _stamps(3), None, 3)
        self.assertFalse(np.allclose(out[0]["action"][:3], [0, -step, 0],
                                     atol=1e-6))

    def test_pure_rotation_matches_analytic_delta(self):
        angle = 0.1
        poses = [_pose(0, 0, 0, _yaw_quat(angle * i)) for i in range(5)]
        out = derive_action(_traj(poses), _stamps(5), None, 5)
        self.assertTrue(np.allclose(out[0]["action"][3:6], [0, 0, angle],
                                    atol=1e-6))

    def test_gripper_dimension_tracks_open_fraction(self):
        poses = [_pose(0.01 * i, 0, 0) for i in range(4)]
        gripper = [[0.0, 0, 0], [50.0, 0, 0], [100.0, 0, 0], [100.0, 0, 0]]
        out = derive_action(_traj(poses), _stamps(4), gripper, 4)
        self.assertAlmostEqual(out[0]["action"][6], 0.5, places=6)
        self.assertAlmostEqual(out[1]["action"][6], 1.0, places=6)

    def test_gripper_is_clipped_to_unit_range(self):
        poses = [_pose(0.01 * i, 0, 0) for i in range(3)]
        out = derive_action(_traj(poses), _stamps(3), [[500.0, 0, 0]] * 3, 3)
        self.assertLessEqual(out[0]["action"][6], 1.0)
        self.assertGreaterEqual(out[0]["action"][6], 0.0)

    def test_last_frame_reuses_previous_delta(self):
        poses = [_pose(0.01 * i, 0, 0) for i in range(5)]
        out = derive_action(_traj(poses), _stamps(5), None, 5)
        self.assertIn(4, out)
        self.assertTrue(np.allclose(out[4]["action"][:3], out[3]["action"][:3],
                                    atol=1e-9))

    def test_action_dimension_is_seven(self):
        poses = [_pose(0.01 * i, 0, 0) for i in range(5)]
        out = derive_action(_traj(poses), _stamps(5), None, 5)
        self.assertEqual(len(out[0]["action"]), 7)

    def test_all_finite_on_mixed_input(self):
        poses = [_pose(0.01 * i, 0, 0) for i in range(6)]
        traj = _traj(poses)
        traj[3] = []                  # 该格没有样本
        out = derive_action(traj, _stamps(6), None, 6)
        values = np.array([out[i]["action"] for i in sorted(out)])
        self.assertTrue(np.all(np.isfinite(values)))

    def test_empty_and_degenerate_inputs(self):
        self.assertIsNone(derive_action([], [], None, 0))
        self.assertIsNone(derive_action([[]] * 3, _stamps(3), None, 3))
        self.assertIsNone(derive_action(_traj([_pose(0, 0, 0)]), _stamps(1),
                                        None, 1))

    def test_sample_from_neighbour_cell_recovers_empty_frame(self):
        """空帧若能从相邻格子的样本里找到位姿,就不该被当作丢帧。

        这是换用 slam_trajectory 的核心收益:采集端 50Hz、数据帧 30fps,
        两帧之间到达的样本落在其中一格,所以「本帧格子为空」不代表那一刻
        没有位姿。
        """
        # 真实形态:绝大多数帧的格子只装属于自己的那一个样本,个别格子会
        # 多装一个「属于下一帧窗口」的样本。帧 5 自己的格子为空,但帧 4 的
        # 格子里存着它 —— 这正是采集端 50Hz 快于数据 30fps 的结果。
        dt = 1 / 30
        n = 10
        traj = []
        for i in range(n):
            if i == 5:
                traj.append([])                      # 本帧格子空
                continue
            cell = [1000.0 + i * dt] + _pose(0.01 * i, 0, 0)
            if i == 4:
                # 帧 5 的样本溢到了上一个格子里(它比本帧时刻晚一点)
                cell += [1000.0 + 5 * dt] + _pose(0.05, 0, 0)
            traj.append(cell)

        out = derive_action(traj, _stamps(n), None, n)
        self.assertIsNotNone(out)
        self.assertIn(5, out, "空帧应能从相邻格子的样本恢复")
        # 帧 5 用的应是那个真实样本(0.05),而不是帧 4/6 的插值中点
        self.assertAlmostEqual(out[5]["observation.state"][0], 0.05, places=5)

    def test_observation_state_is_relative_to_episode_start(self):
        poses = [_pose(1.0 + 0.01 * i, 2.0, 3.0) for i in range(5)]
        out = derive_action(_traj(poses), _stamps(5), [[50.0, 0, 0]] * 5, 5)
        # 位置分量是相对本集首帧的偏移 → 首帧为 0
        self.assertTrue(np.allclose(out[0]["observation.state"][:3],
                                    [0.0, 0.0, 0.0], atol=1e-9))
        self.assertAlmostEqual(out[3]["observation.state"][0], 0.03, places=6)
        self.assertAlmostEqual(out[3]["observation.state"][3], 0.5, places=6)


class UmiSlamActionModuleTests(unittest.TestCase):
    def _context(self, root: Path) -> JobContext:
        return JobContext(
            node_id="umi_slam_action",
            node_type="umi_slam_action",
            config={},
            job={},
            input_root=root,
            output_root=root / "outputs",
            incoming={},
            progress=lambda _value: None,
            node_states={},
        )

    def _write_episode(self, root: Path, poses, gripper=None,
                       extra_columns: dict | None = None) -> Path:
        """写入一份 canonical parquet。

        位姿写成 ``observation.slam_trajectory`` 的格子格式(每格 8 的整数
        倍)并配 ``timestamp`` 列 —— 模块只从这个缓冲重建位姿。
        """
        (root / "data" / "chunk-000").mkdir(parents=True, exist_ok=True)
        stamps = _stamps(len(poses))
        columns = {
            "frame_index": list(range(len(poses))),
            "timestamp": stamps,
            "observation.slam_trajectory": _traj(poses),
        }
        if gripper is not None:
            columns["observation.gripper_state"] = gripper
        for name, values in (extra_columns or {}).items():
            columns[name] = values
        path = root / "data" / "chunk-000" / "episode_000000.parquet"
        pq.write_table(pa.table(columns), path)
        return path

    def test_derives_varying_action_and_reports_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            # 用非等距轨迹:等距平移的每帧增量恒等,会被导出端的恒定列
            # 过滤剔除,验证不出"动作确实在变化"这一点。
            poses = [_pose(0.01 * i + 0.004 * (i % 3), 0, 0) for i in range(20)]
            self._write_episode(root, poses, [[50.0, 0, 0]] * 20)

            outputs = UmiSlamActionModule().run(self._context(root))

            self.assertIn("action", outputs)
            ref = outputs["action"]
            self.assertEqual(ref.metadata["shape"], [7])
            self.assertEqual(ref.metadata["frames_total"], 20)
            self.assertEqual(len(ref.metadata["names"]), 7)

            import pandas as pd
            frame = pd.read_parquet(root / "outputs" / "umi_slam_action" / "umi_slam_action.parquet")
            values = np.array(frame["action"].tolist())
            self.assertEqual(values.shape[1], 7)
            # 必须是真正变化的动作,否则导出的恒定列过滤会把它删掉
            self.assertGreater(len(np.unique(values[:, 0])), 1)

    def test_output_is_float32(self):
        """LeRobot 的 action 声明是 float32;写 double 会造成声明与 schema
        不一致(官方加载器按声明 cast)。"""
        import pyarrow.parquet as pq
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            poses = [_pose(0.01 * i, 0, 0) for i in range(10)]
            self._write_episode(root, poses, [[50.0, 0, 0]] * 10)
            UmiSlamActionModule().run(self._context(root))

            path = root / "outputs" / "umi_slam_action" / "umi_slam_action.parquet"
            field_type = pq.read_schema(path).field("action").type
            self.assertIn("float", str(field_type))
            self.assertNotIn("double", str(field_type))

    def test_skips_batch_without_slam_pose(self):
        """非 UMI 批次(如 D435 手套)必须跳过,不能写坏数据。"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_episode(root, [_pose(0, 0, 0)], None,
                                extra_columns={"observation.glove": [[1.0, 2.0]]})
            # 覆盖掉 slam 列,模拟没有 SLAM 的设备
            (root / "data" / "chunk-000" / "episode_000000.parquet").unlink()
            pq.write_table(pa.table({
                "frame_index": [0, 1],
                "observation.glove": [[1.0, 2.0], [3.0, 4.0]],
            }), root / "data" / "chunk-000" / "episode_000000.parquet")

            with self.assertRaises(ModuleSkip):
                UmiSlamActionModule().run(self._context(root))

    def test_retries_transient_read_failure(self):
        """上传/处理并发时 parquet 可能瞬时不可读,不能一次失败就放弃。

        实测 ep52/ep71 都因此被误判成「列不可读」跳过,几秒后同一文件完全
        正常。这里注入两次失败,验证第三次成功读到时模块照常产出。
        """
        from unittest.mock import patch

        import pandas as pd

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            poses = [_pose(0.01 * i + 0.004 * (i % 3), 0, 0) for i in range(12)]
            self._write_episode(root, poses, [[50.0, 0, 0]] * 12)

            original = pd.read_parquet
            state = {"calls": 0}

            def flaky(*args, **kwargs):
                state["calls"] += 1
                if state["calls"] <= 2:
                    raise OSError("file is being written")
                return original(*args, **kwargs)

            with patch.object(pd, "read_parquet", flaky), \
                    patch("time.sleep", lambda _s: None):
                outputs = UmiSlamActionModule().run(self._context(root))

            self.assertGreaterEqual(state["calls"], 3, "应当重试到成功")
            self.assertIn("action", outputs)

    def test_skip_message_carries_the_real_exception(self):
        """笼统提示无法区分竞态与设备类型不符,必须带上真实异常。"""
        from unittest.mock import patch

        import pandas as pd

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_episode(root, [_pose(0.01 * i, 0, 0) for i in range(8)],
                                [[50.0, 0, 0]] * 8)

            def always_fail(*args, **kwargs):
                raise OSError("mount is stale")

            with patch.object(pd, "read_parquet", always_fail), \
                    patch("time.sleep", lambda _s: None):
                with self.assertRaises(ModuleSkip) as caught:
                    UmiSlamActionModule().run(self._context(root))

            message = str(caught.exception)
            self.assertIn("OSError", message)
            self.assertIn("stale", message)

    def test_skips_when_all_poses_are_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_episode(root, [[0.0] * 7] * 10, [[0.0, 0, 0]] * 10)
            with self.assertRaises(ModuleSkip):
                UmiSlamActionModule().run(self._context(root))

    def test_skips_when_no_parquet_present(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "data").mkdir(parents=True, exist_ok=True)
            with self.assertRaises(ModuleSkip):
                UmiSlamActionModule().run(self._context(root))

    def test_ignores_non_parquet_upstream_ref(self):
        """上游 ``slam_trajectory`` 端口可能挂着一个非 parquet 产物。

        worker 组装 incoming 时,若边的 sourceHandle 对不上上游任何输出,
        会把该节点的**全部输出平铺**进来 —— 视频 ref 也会落到这个键上。
        实测因此把 mp4 当 parquet 读,报 magic bytes not found。模块必须
        验证产物类型,并回退到扫描批次里的真 parquet。
        """
        from app.processing import ArtifactRef

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            poses = [_pose(0.01 * i + 0.004 * (i % 3), 0, 0) for i in range(12)]
            self._write_episode(root, poses, [[50.0, 0, 0]] * 12)

            # 上游把视频文件挂到了 slam_trajectory 这个端口上
            video = root / "videos" / "observation.images.gripper_rgb" / "chunk-000"
            video.mkdir(parents=True, exist_ok=True)
            clip = video / "episode_000000.mp4"
            clip.write_bytes(b"\x00\x00\x00\x1cftypisom" + b"\x00" * 64)

            ctx = self._context(root)
            ctx.incoming = {"slam_trajectory": ArtifactRef(
                kind="rgb_video", path=str(clip.relative_to(root)),
                source_key="gripper_rgb")}

            outputs = UmiSlamActionModule().run(ctx)

            self.assertIn("action", outputs, "应回退到真正的 parquet")


if __name__ == "__main__":
    unittest.main()
