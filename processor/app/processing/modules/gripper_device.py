"""UMI Gripper device input.

This is a composite workflow *source* node.  The acquisition client uploads
one gripper episode, while the workflow receives its RGB video, ESP32 grip
state, SLAM trajectory and the two calibrated tactile force matrices through
typed output ports.  The node does not control hardware or recapture data.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.processing import ArtifactRef, JobContext, ProcessingModule, field
from app.processing.registry import register
from app.processing.theme import INPUT_COLOR


_VIDEO_OUTPUT = "rgb_video"
_PARQUET_COLUMNS = {
    "gripper_state": "observation.gripper_state",
    "slam_trajectory": "observation.slam_pose",
}
_TACTILE_COLUMNS = {
    "left": "observation.gripper_left_force_matrix",
    "right": "observation.gripper_right_force_matrix",
}
_ALL_PARQUET_COLUMNS = set(_PARQUET_COLUMNS.values()) | set(_TACTILE_COLUMNS.values())


def _read_info(root: Path) -> dict:
    path = root / "meta" / "info.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _values(value: object) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _selected_gripper(info: dict, config: dict) -> dict:
    """Return the selected physical gripper declaration, if any."""
    wanted = {item.casefold() for item in (
        _values(config.get("source_keys"))
        + _values(config.get("source_key"))
        + _values(config.get("device_id"))
        + _values(config.get("device_name"))
    )}
    candidates = []
    for item in info.get("devices") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").casefold()
        name = str(item.get("name") or "")
        key = str(item.get("key") or "")
        slots = _values(item.get("slots"))
        haystack = {value.casefold() for value in (name, key, *slots) if value}
        if "gripper" in kind or "gripper" in name.casefold() or "umi" in name.casefold():
            candidates.append((bool(wanted & haystack), item))
    if candidates:
        candidates.sort(key=lambda pair: (not pair[0], str(pair[1].get("key") or pair[1].get("name") or "")))
        return candidates[0][1]
    return {}


def _video_entries(root: Path) -> list[tuple[str, Path]]:
    from app.lerobot_v21 import iter_video_streams
    videos_root = root / "videos"
    if not videos_root.is_dir():
        return []
    return [(str(source), path) for source, path in iter_video_streams(videos_root)]


def _find_rgb_video(root: Path, info: dict, config: dict) -> tuple[str, Path] | None:
    entries = _video_entries(root)
    if not entries:
        return None
    device = _selected_gripper(info, config)
    slots = {value.casefold() for value in _values(device.get("slots"))}
    configured = {value.casefold() for value in (
        _values(config.get("source_keys")) + _values(config.get("source_key"))
    )}

    def is_primary(source: str) -> bool:
        low = source.casefold()
        return (low in slots or low in configured
                or "gripper_rgb" in low or ("gripper" in low and "rgb" in low)) and not (
                    "stereo_left" in low or "stereo_right" in low
                    or low.endswith("_left") or low.endswith("_right"))

    for source, path in entries:
        if is_primary(source):
            return source, path
    # A valid device declaration may use a non-standard RGB slot name.  If
    # the selected gripper has exactly one non-stereo video, use that slot.
    non_stereo = [item for item in entries if not any(
        token in item[0].casefold() for token in ("stereo_left", "stereo_right"))]
    if len(non_stereo) == 1 and (device or configured):
        return non_stereo[0]
    return None


def _parquet_columns(root: Path) -> tuple[Path, set[str]] | None:
    try:
        import pyarrow.parquet as pq
    except ImportError:
        return None
    candidates: list[tuple[Path, set[str]]] = []
    for path in sorted(root.rglob("*.parquet")):
        if "/meta/" in path.as_posix().casefold():
            continue
        try:
            names = set(str(name) for name in pq.read_schema(path).names)
        except (OSError, ValueError):
            continue
        if names & _ALL_PARQUET_COLUMNS:
            candidates.append((path, names))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-len(item[1] & _ALL_PARQUET_COLUMNS), item[0].as_posix()))
    return candidates[0]


@register
class GripperDeviceModule(ProcessingModule):
    slug = "gripper_device"
    version = "1.1"
    category = "input"
    label = "UMI Gripper"
    icon = "ant-design:robot-outlined"
    color = INPUT_COLOR
    outputs = (
        {"key": "rgb_video", "label": "RGB Video"},
        {"key": "gripper_state", "label": "ESP Gripper State"},
        {"key": "slam_trajectory", "label": "SLAM Trajectory"},
        {"key": "tactile_force_matrices", "label": "Left and Right Force Matrices"},
    )
    default_config = {"source_key": "", "fps": 30}
    config_schema = (
        field("source_key", "string", "Gripper device / source key", ""),
    )
    execution_target = "server"
    capabilities = ("composite_device", "video_input", "gripper", "slam", "tactile")

    def run(self, ctx: JobContext) -> dict[str, ArtifactRef] | None:
        info = _read_info(ctx.input_root)
        outputs: dict[str, ArtifactRef] = {}
        rgb = _find_rgb_video(ctx.input_root, info, ctx.config)
        if rgb is not None:
            source, path = rgb
            outputs[_VIDEO_OUTPUT] = ctx.ref(
                "gripper_rgb_video", path, source_key=source,
                metadata={"device_type": "umi_gripper"},
            )

        parquet = _parquet_columns(ctx.input_root)
        if parquet is not None:
            path, columns = parquet
            for output, column in _PARQUET_COLUMNS.items():
                if column not in columns:
                    continue
                metadata = {"column": column, "device_type": "umi_gripper"}
                if output == "slam_trajectory":
                    metadata.update({"shape": [7], "names": ["x", "y", "z", "qx", "qy", "qz", "qw"]})
                outputs[output] = ctx.ref(
                    output, path, source_key=column, metadata=metadata,
                )
            tactile_columns = [column for column in _TACTILE_COLUMNS.values() if column in columns]
            if len(tactile_columns) == len(_TACTILE_COLUMNS):
                outputs["tactile_force_matrices"] = ctx.ref(
                    "tactile_force_matrices", path,
                    source_key="observation.gripper_force_matrices",
                    metadata={
                        "columns": tactile_columns,
                        "sides": ["left", "right"],
                        "shape": [2, 250, 250, 3],
                        "components": ["fx", "fy", "fz"],
                        "device_type": "umi_gripper",
                    },
                )

        if not outputs:
            ctx.skip("UMI gripper RGB/video and data channels were not found")
        return outputs
