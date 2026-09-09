import json
import tempfile
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from app.processing import JobContext
from app.processing.modules.gripper_device import GripperDeviceModule
from app.workflow_dispatch import _episode_input_groups
from app.workflow_types import migrate_graph_types


class GripperDeviceModuleTests(unittest.TestCase):
    def _context(self, root: Path) -> JobContext:
        return JobContext(
            node_id="gripper",
            node_type="gripper_device",
            config={"source_key": "gripper_rgb"},
            job={},
            input_root=root,
            output_root=root / "outputs",
            incoming={},
            progress=lambda _value: None,
            node_states={},
        )

    def test_publishes_one_composite_device_with_paired_force_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "meta").mkdir()
            (root / "videos" / "observation.images.gripper_rgb" / "chunk-000").mkdir(parents=True)
            (root / "videos" / "observation.images.gripper_rgb" / "chunk-000" / "episode_000000.mp4").touch()
            (root / "meta" / "info.json").write_text(json.dumps({
                "devices": [{
                    "key": "gripper:test",
                    "kind": "gripper",
                    "name": "UMI Gripper",
                    "slots": ["gripper_rgb", "gripper_force_left", "gripper_force_right"],
                }],
            }), encoding="utf-8")
            (root / "data" / "chunk-000").mkdir(parents=True)
            pq.write_table(pa.table({
                "observation.gripper_state": [[0.5, 1.0, 0.0]],
                "observation.slam_pose": [[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]],
                "observation.gripper_left_force_matrix": [list(range(3))],
                "observation.gripper_right_force_matrix": [list(range(3))],
            }), root / "data" / "chunk-000" / "episode_000000.parquet")

            outputs = GripperDeviceModule().run(self._context(root))

            self.assertEqual(set(outputs), {
                "rgb_video", "gripper_state", "slam_trajectory",
                "tactile_force_matrices",
            })
            self.assertEqual(outputs["slam_trajectory"].metadata["names"],
                             ["x", "y", "z", "qx", "qy", "qz", "qw"])
            self.assertEqual(outputs["tactile_force_matrices"].metadata["columns"], [
                "observation.gripper_left_force_matrix",
                "observation.gripper_right_force_matrix",
            ])
            self.assertEqual(outputs["tactile_force_matrices"].metadata["shape"],
                             [2, 250, 250, 3])

    def test_dispatch_groups_gripper_stream_as_one_device(self):
        episode = {
            "camera_names": ["gripper_rgb", "gripper_stereo_left", "gripper_stereo_right"],
            "device_names": {
                "gripper_rgb": "UMI Gripper",
                "gripper_stereo_left": "UMI Gripper",
                "gripper_stereo_right": "UMI Gripper",
            },
            "devices": [],
            "sensors": [],
        }
        groups = _episode_input_groups(episode)
        self.assertEqual([group["input_type"] for group in groups], ["gripper_device"])
        self.assertEqual(groups[0]["source_keys"], [
            "gripper_rgb", "gripper_stereo_left", "gripper_stereo_right",
        ])

    def test_migrates_old_force_ports_to_one_connection(self):
        graph, changed = migrate_graph_types({
            "nodes": [{
                "id": "gripper", "data": {
                    "nodeType": "gripper_device",
                    "outputs": [
                        {"key": "tactile_left", "label": "Left Force Matrix"},
                        {"key": "tactile_right", "label": "Right Force Matrix"},
                    ],
                },
            }, {"id": "target", "data": {"nodeType": "annotation"}}],
            "edges": [
                {"id": "left", "source": "gripper", "sourceHandle": "tactile_left",
                 "target": "target", "targetHandle": "data"},
                {"id": "right", "source": "gripper", "sourceHandle": "tactile_right",
                 "target": "target", "targetHandle": "data"},
            ],
        })
        self.assertTrue(changed)
        self.assertEqual(graph["nodes"][0]["data"]["outputs"][-1]["key"],
                         "tactile_force_matrices")
        self.assertEqual(len(graph["edges"]), 1)
        self.assertEqual(graph["edges"][0]["sourceHandle"], "tactile_force_matrices")


if __name__ == "__main__":
    unittest.main()
