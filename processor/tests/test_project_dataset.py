from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from app.project_dataset import (
    _merge_info,
    allocate_project_episode_id,
    write_project_episode_index,
)


def _write_frame_data(root: Path) -> None:
    path = root / "data" / "chunk-000" / "episode_000000.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({
        "episode_index": pa.array([0]),
        "frame_index": pa.array([0]),
        "observation.slam_pose": pa.array([[0.0] * 7]),
        "observation.gripper_left_force": pa.array([[1.0, 2.0, 3.0]]),
    }), path)


def test_new_episode_id_does_not_reuse_malformed_existing_id(tmp_path: Path):
    write_project_episode_index(tmp_path, [{
        "episode_index": 0,
        "episode_id": "Uncategorized_000001",
        "length": 10,
    }])

    assert allocate_project_episode_id(
        tmp_path, "Test9_4", "Uncategorized",
    ) == "Uncategorized_000002"


def test_empty_project_starts_at_episode_zero(tmp_path: Path):
    assert allocate_project_episode_id(
        tmp_path, "new_upload", "UMI夹爪项目",
    ) == "UMI夹爪项目_000000"


def test_info_is_filtered_to_real_files_and_columns(tmp_path: Path):
    _write_frame_data(tmp_path)
    video = (tmp_path / "videos" / "observation.images.gripper_rgb"
             / "chunk-000" / "episode_000000.mp4")
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"placeholder")

    stale_info = {
        "task_name": "Test9/4",
        "features": {
            "observation.slam_pose": {"dtype": "float32", "shape": [7]},
            "observation.gripper_left_force": {
                "dtype": "float32", "shape": [3],
            },
            "observation.images.gripper_rgb": {"dtype": "video"},
            "observation.images.D435_rgb": {"dtype": "video"},
            "observation.imu": {"dtype": "float32", "shape": [6]},
        },
        "devices": [
            {"key": "d435", "name": "D435", "slots": ["D435_rgb"]},
            {"key": "gripper", "name": "UMI", "slots": [
                "gripper_rgb", "gripper_pose", "gripper_force_left",
            ]},
        ],
        "device_names": {
            "D435_rgb": "D435",
            "gripper_rgb": "UMI",
            "gripper_pose": "UMI",
            "gripper_force_left": "UMI",
        },
        "cameras": {
            "D435_rgb": {"width": 1280},
            "gripper_rgb": {"width": 1280},
        },
        "sensors": ["right_glove"],
    }
    merged = _merge_info(tmp_path, stale_info, [{
        "episode_index": 0,
        "length": 1,
    }], [{"task_index": 0, "task": "Test9/4"}])

    assert set(merged["features"]) == {
        "observation.slam_pose",
        "observation.gripper_left_force",
        "observation.images.gripper_rgb",
    }
    assert merged["cameras"] == {"gripper_rgb": {"width": 1280}}
    assert [d["key"] for d in merged["devices"]] == ["gripper"]
    assert merged["features"]["observation.slam_pose"]["quaternion_order"] == "xyzw"
    assert merged["features"]["observation.gripper_left_force"]["names"] == [
        "fx", "fy", "fz",
    ]
