"""Video streaming + upload API."""

import os
import re
import asyncio
import shutil
import threading
import math
from collections import OrderedDict
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import StreamingResponse, JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import Episode
from app.localstore import get_episode
from app.config import settings
from app.security import verify_api_key
from app.artifact_resolver import (
    find_depth_video,
    has_depth_source,
    find_hand3d_candidates,
    find_hand_keypoint_candidates,
    source_matches,
)

router = APIRouter(prefix="/api/v1/video", tags=["video"])
CHUNK_SIZE = 1024 * 1024

_DEPTH_READER_POOL: dict[str, dict] = {}
_DEPTH_READER_POOL_LOCK = threading.Lock()
_DEPTH_READER_POOL_MAX = 8
_DEPTH_FRAME_CACHE: dict[tuple[str, int], object] = {}
_DEPTH_FRAME_CACHE_MAX = 48
# Canonical SLAM rows are shared by the trajectory and frame-window requests.
# The cache is keyed by file size/mtime so a workflow reprocess cannot return
# a trajectory from an older parquet.
_SLAM_DATA_CACHE: dict[str, tuple[str, list[dict]]] = {}
_SLAM_DATA_CACHE_MAX = 8
# UMI tactile matrices are stored as three flattened int16 planes per frame
# (250*250*3).  Keep only encoded preview images here; never retain a whole
# episode of matrices in the API process.
_TACTILE_PREVIEW_CACHE: OrderedDict[tuple, bytes] = OrderedDict()
_TACTILE_PREVIEW_CACHE_MAX = 48
_TACTILE_FIELD_CACHE: OrderedDict[tuple, tuple[bytes, float, float]] = OrderedDict()
_TACTILE_FIELD_CACHE_MAX = 96
_TACTILE_PREVIEW_LOCK = threading.Lock()
_TACTILE_SHAPE = (250, 250)
_TACTILE_COLUMNS = {
    "left": "observation.gripper_left_force_matrix",
    "right": "observation.gripper_right_force_matrix",
}
_TACTILE_FORCE_COLUMNS = {
    "left": "observation.gripper_left_force",
    "right": "observation.gripper_right_force",
}
# Sequential decode is cheap, but decoding through more than this many
# frames to reach a scrub target is not; jump via a seek reader instead.
_DEPTH_WINDOW_SEEK_GAP = 6


def _read_canonical_depth_codes(path: Path, frame_index: int):
    """Read one stored uint16 code frame without creating a color image.

    Readers are kept sequentially per source so normal 30 FPS playback does
    not restart FFmpeg from frame zero for every HTTP request.  Seeking back
    creates a fresh reader and discards only in-memory decoder state.  The
    display path is deliberately raw: decoded Y samples are already the
    canonical 12-bit codes and must never be quantized as millimetres again.
    """
    if frame_index < 0:
        raise IndexError(frame_index)
    from app.lerobot_v21 import DepthVideoReader
    import numpy as np

    key = str(Path(path).resolve())
    cache_key = (key, int(frame_index))
    with _DEPTH_READER_POOL_LOCK:
        cached = _DEPTH_FRAME_CACHE.get(cache_key)
        if cached is not None:
            return np.ascontiguousarray(cached, dtype="<u2").copy()
        state = _DEPTH_READER_POOL.get(key)
        if state is None:
            state = {"reader": DepthVideoReader(path), "next_frame": 0,
                     "lock": threading.Lock()}
            _DEPTH_READER_POOL[key] = state
            while len(_DEPTH_READER_POOL) > _DEPTH_READER_POOL_MAX:
                old_key, old_state = next(iter(_DEPTH_READER_POOL.items()))
                if old_key == key and len(_DEPTH_READER_POOL) > 1:
                    old_key, old_state = next(iter(
                        list(_DEPTH_READER_POOL.items())[1:]))
                _DEPTH_READER_POOL.pop(old_key, None)
                try:
                    old_state["reader"].close()
                except Exception:
                    pass

    with state["lock"]:
        if frame_index < state["next_frame"]:
            state["reader"].close()
            state["reader"] = DepthVideoReader(path)
            state["next_frame"] = 0
        codes = None
        while state["next_frame"] <= frame_index:
            codes = state["reader"].read_codes()
            if codes is None:
                raise IndexError(frame_index)
            state["next_frame"] += 1
        result = np.ascontiguousarray(codes, dtype="<u2").copy()
        with _DEPTH_READER_POOL_LOCK:
            _DEPTH_FRAME_CACHE[cache_key] = result
            while len(_DEPTH_FRAME_CACHE) > _DEPTH_FRAME_CACHE_MAX:
                _DEPTH_FRAME_CACHE.pop(next(iter(_DEPTH_FRAME_CACHE)))
        return result


def _depth_local_source(depth_video: Path) -> Path:
    """Decode from a local mirror of the NAS depth file when one is ready.

    The mirror is a bit-identical disposable cache (see
    ``browser_preview.ensure_local_mirror``); a missing/cold mirror falls
    back to the authoritative path for this request.
    """
    try:
        from app.browser_preview import ensure_local_mirror

        return ensure_local_mirror(depth_video)
    except Exception:
        return depth_video


def _downscale_depth_codes(codes, scale: int):
    """Optionally area-downsample raw depth codes for display transport.

    The browser canvas usually renders the depth tile far below the native
    resolution, so a 2x/4x downsample cuts the payload by 4x/16x without
    visible loss.  Codes stay in the canonical uint16 domain so the
    frontend colormap/range pipeline is untouched.
    """
    if not scale or int(scale) <= 1:
        return codes
    import cv2

    s = int(scale)
    h, w = codes.shape
    return cv2.resize(codes, (max(1, w // s), max(1, h // s)),
                      interpolation=cv2.INTER_AREA)


def _read_canonical_depth_code_window(path: Path, start_frame: int,
                                      end_frame: int):
    """Read a contiguous raw-code window from one sequential decoder.

    The browser cannot natively decode ``gray12le`` as a video texture, so it
    still needs the canonical uint16 samples. Returning a short sequential
    window avoids one HTTP request per frame while keeping colorization in the
    browser and never creating a stored JET image.
    """
    if start_frame < 0 or end_frame < start_frame:
        raise IndexError(start_frame)
    from app.lerobot_v21 import DepthVideoReader
    import numpy as np

    key = str(Path(path).resolve())
    with _DEPTH_READER_POOL_LOCK:
        state = _DEPTH_READER_POOL.get(key)
        if state is None:
            state = {"reader": DepthVideoReader(path), "next_frame": 0,
                     "lock": threading.Lock()}
            _DEPTH_READER_POOL[key] = state
            while len(_DEPTH_READER_POOL) > _DEPTH_READER_POOL_MAX:
                old_key, old_state = next(iter(_DEPTH_READER_POOL.items()))
                if old_key == key and len(_DEPTH_READER_POOL) > 1:
                    old_key, old_state = next(iter(
                        list(_DEPTH_READER_POOL.items())[1:]))
                _DEPTH_READER_POOL.pop(old_key, None)
                try:
                    old_state["reader"].close()
                except Exception:
                    pass

    with state["lock"]:
        gap = start_frame - state["next_frame"]
        if gap < 0 or gap > _DEPTH_WINDOW_SEEK_GAP:
            # Backward or far-forward jump: position a seek reader at the
            # exact target frame instead of decoding from frame 0 (or from a
            # stale pool position).  The keyint=2 depth contract keeps the
            # seek cost constant regardless of the jump distance.
            from app.lerobot_v21 import DepthCodeSeekReader

            fps = float(getattr(state["reader"], "fps", 0) or 30.0)
            pix_fmt = str(getattr(state["reader"], "pix_fmt", "") or "gray12le")
            state["reader"].close()
            state["reader"] = DepthCodeSeekReader(
                path, start_frame, fps, pix_fmt)
            state["next_frame"] = start_frame
        frames = []
        while state["next_frame"] <= end_frame:
            codes = state["reader"].read_codes()
            if codes is None:
                if not frames:
                    raise IndexError(start_frame)
                break
            current = state["next_frame"]
            state["next_frame"] += 1
            if current >= start_frame:
                frames.append(np.ascontiguousarray(codes, dtype="<u2").copy())
        if not frames:
            raise IndexError(start_frame)
        return frames


def _sanitize_for_json(obj):
    """Recursively convert numpy types → Python native types for JSON serialization."""
    import numpy as np
    if obj is None:
        return None
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return _sanitize_for_json(obj.tolist())
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(v) for v in obj]
    return obj


# ── Helper: extract hand keypoint data for a frame ─────

def _canonical_episode_data_files(session_dir: Path, episode_id: str) -> list[Path]:
    """Return only the active episode parquet in the canonical dataset."""
    try:
        from app.project_dataset import episode_files, episode_row, is_project_dataset
        root = Path(session_dir)
        if not is_project_dataset(root):
            return []
        row = episode_row(root, str(episode_id))
        if row is None:
            return []
        return list(episode_files(root, int(row.get("episode_index", 0))).get("data", []))
    except (OSError, TypeError, ValueError):
        return []


def _read_gripper_state(value) -> list[float] | None:
    """Read the legacy UMI state vector without changing its raw values.

    The acquisition contract used by the current gripper writer is
    ``[percent, gripped, raw]``.  Keep the complete vector in the response so
    future renderers can use the encoder value without recomputing it.
    """
    if value is None:
        return None
    try:
        values = list(value)
    except TypeError:
        return None
    result: list[float] = []
    for item in values[:3]:
        try:
            number = float(item)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(number):
            return None
        result.append(number)
    return result or None


def _read_slam_rows(path: Path) -> tuple[str, list[dict]]:
    """Read canonical ``observation.slam_pose`` rows once per parquet revision."""
    import numpy as np
    import pandas as pd

    path = Path(path)
    try:
        stat = path.stat()
        revision = f"{int(stat.st_size):x}-{int(stat.st_mtime_ns):x}"
    except OSError:
        return "unknown", []
    cache_key = str(path.resolve())
    cached = _SLAM_DATA_CACHE.get(cache_key)
    if cached is not None and cached[0] == revision:
        return cached
    columns = ["frame_index", "timestamp", "observation.slam_pose"]
    has_gripper_state = False
    try:
        import pyarrow.parquet as pq
        available_columns = set(pq.ParquetFile(path).schema_arrow.names)
        has_gripper_state = "observation.gripper_state" in available_columns
    except Exception:
        pass
    if has_gripper_state:
        columns.append("observation.gripper_state")
    try:
        df = pd.read_parquet(path, columns=columns, engine="pyarrow")
    except Exception:
        return revision, []

    rows: list[dict] = []
    for _, row in df.iterrows():
        try:
            frame = int(row.get("frame_index", len(rows)))
        except (TypeError, ValueError):
            continue
        timestamp = row.get("timestamp")
        try:
            timestamp = float(timestamp) if np.isfinite(float(timestamp)) else None
        except (TypeError, ValueError):
            timestamp = None
        pose = row.get("observation.slam_pose")
        try:
            values = np.asarray(pose, dtype=np.float64).reshape(-1)
        except (TypeError, ValueError):
            values = np.empty(0, dtype=np.float64)

        # A missing native SLAM sample is represented by an all-zero pose in
        # some recordings.  It is not the same thing as a valid pose at the
        # origin: a valid pose always has a unit quaternion.  Keep only valid
        # poses and remember the gap so the renderer can break the polyline.
        valid = values.size >= 7 and np.isfinite(values[:7]).all()
        if valid:
            quaternion_norm = float(np.linalg.norm(values[3:7]))
            valid = 0.5 <= quaternion_norm <= 1.5
        if not valid:
            continue
        rows.append({
            "f": frame,
            "p": [float(value) for value in values[:7]],
            "t": timestamp,
            "g": _read_gripper_state(row.get("observation.gripper_state"))
            if has_gripper_state else None,
        })
    rows.sort(key=lambda item: item["f"])

    # Decimation can legitimately make frame numbers non-consecutive, so the
    # frontend must not infer a gap from every frame jump.  Mark only gaps
    # caused by missing/invalid source rows; sampled long trajectories remain
    # a continuous polyline.
    previous_frame = None
    for item in rows:
        frame = int(item["f"])
        item["gap_before"] = (
            previous_frame is not None and frame != previous_frame + 1
        )
        previous_frame = frame
    result = (revision, rows)
    _SLAM_DATA_CACHE[cache_key] = result
    while len(_SLAM_DATA_CACHE) > _SLAM_DATA_CACHE_MAX:
        _SLAM_DATA_CACHE.pop(next(iter(_SLAM_DATA_CACHE)))
    return result


def _decimate_slam_rows(rows: list[dict], max_points: int) -> list[dict]:
    if max_points <= 0 or len(rows) <= max_points:
        return rows
    if max_points == 1:
        return [rows[0]]
    indexes = {
        round(index * (len(rows) - 1) / (max_points - 1))
        for index in range(max_points)
    }
    return [rows[index] for index in sorted(indexes)]


def _tactile_episode_file(episode_id: str) -> Path | None:
    ep = get_episode(episode_id)
    if ep is None:
        return None
    files = _canonical_episode_data_files(Path(ep["path"]), episode_id)
    return files[0] if files else None


def _tactile_matrix_columns(path: Path) -> set[str]:
    try:
        import pyarrow.parquet as pq
        return set(pq.ParquetFile(path).schema_arrow.names)
    except Exception:
        return set()


def _read_tactile_force_series(path: Path) -> dict | None:
    """Read the small scalar-force series used by the matrix value badges."""
    import numpy as np
    import pyarrow.parquet as pq

    try:
        available = set(pq.ParquetFile(path).schema_arrow.names)
        selected = [
            column for column in _TACTILE_FORCE_COLUMNS.values()
            if column in available
        ]
        if not selected:
            return None
        table = pq.read_table(path, columns=["frame_index", *selected])
        frames = [int(value) for value in table.column("frame_index").to_pylist()]
        result = {"frames": frames, "left": [], "right": []}
        for side, column in _TACTILE_FORCE_COLUMNS.items():
            if column not in selected:
                result[side] = [None] * len(frames)
                continue
            values = table.column(column).to_pylist()
            normalized = []
            for value in values:
                try:
                    vector = np.asarray(value, dtype=np.float64).reshape(-1)
                    if vector.size < 3 or not np.isfinite(vector[:3]).all():
                        normalized.append(None)
                    else:
                        normalized.append([float(item) for item in vector[:3]])
                except (TypeError, ValueError, OverflowError):
                    normalized.append(None)
            result[side] = normalized
        return result
    except Exception:
        return None


def _decode_tactile_matrix(value):
    """Decode one canonical 250x250x3 row-diff force matrix to mN.

    The recorder difference-encodes each sensor row after interleaving the
    three components: ``[fx0, fy0, fz0, fx1, fy1, fz1, ...]``.  Therefore the
    prefix sum must run over the full 750-value row before reshaping back to
    HWC.  Splitting H/W/C first and accumulating each channel independently
    produces a different matrix and was the cause of the incorrect preview.
    The int16 cast is intentional: the recorder's contract is modulo-2^16 and
    the final values are integer mN.
    """
    import numpy as np

    if value is None:
        return None
    try:
        if isinstance(value, (bytes, bytearray, memoryview)):
            raw = np.frombuffer(value, dtype="<i2")
        else:
            raw = np.asarray(value, dtype=np.int16).reshape(-1)
    except (TypeError, ValueError):
        return None
    if raw.size == 0:
        return None
    pixels = _TACTILE_SHAPE[0] * _TACTILE_SHAPE[1]
    if raw.size == pixels * 3:
        # Canonical data: row-diff over H x (W*3), then H x W x [fx,fy,fz].
        flat_rows = raw.reshape(_TACTILE_SHAPE[0], _TACTILE_SHAPE[1] * 3)
        decoded_hwc = np.cumsum(flat_rows, axis=1, dtype=np.int16)
        decoded = decoded_hwc.reshape(*_TACTILE_SHAPE, 3).transpose(2, 0, 1)
    elif raw.size == pixels:
        # Legacy single-channel field: row-diff over H x W.
        rows = raw.reshape(*_TACTILE_SHAPE)
        decoded = np.cumsum(rows, axis=1, dtype=np.int16)[None, ...]
    else:
        return None
    decoded = decoded.astype(np.float32)
    if decoded.shape[0] == 1:
        decoded = decoded.repeat(3, axis=0)
    return decoded


def _read_tactile_frame(path: Path, frame_index: int, side: str):
    """Read only the requested parquet row and side.

    PyArrow applies the frame_index predicate before materialising the list
    column.  This keeps normal scrubbing bounded to one matrix row instead of
    loading both force streams into the browser.
    """
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    column = _TACTILE_COLUMNS.get(side)
    if column is None:
        return None
    try:
        table = pq.read_table(
            path,
            columns=["frame_index", column],
            filters=[("frame_index", "=", int(frame_index))],
        )
        if table.num_rows == 0:
            return None
        frames = table.column("frame_index").to_pylist()
        column_data = table.column(column)
        if isinstance(column_data, pa.ChunkedArray):
            column_data = column_data.combine_chunks()
        offsets = np.asarray(column_data.offsets, dtype=np.int64)
        values = np.asarray(column_data.values, dtype=np.int16)
        for index, frame in enumerate(frames):
            if int(frame) == int(frame_index):
                decoded = _decode_tactile_matrix(
                    values[offsets[index]:offsets[index + 1]])
                # Some writers emit an empty vector for a valid first frame
                # before the tactile device publishes its first sample. Keep
                # that frame drawable as an all-zero matrix rather than
                # making the UI show a broken image.
                if decoded is None:
                    return np.zeros((3, *_TACTILE_SHAPE), dtype=np.float32)
                return decoded
    except Exception:
        return None
    return None


def _tactile_force_field(
    path: Path, frame_index: int, side: str,
) -> tuple[bytes, float, float] | None:
    """Return one smoothed fz pressure field as float32 bytes.

    The payload is numeric data, not a rendered image.  The frontend applies
    the color map in WebGL2.  ``lo``/``hi`` preserve the acquisition-side
    per-frame contrast normalization without sending a rendered image.
    """
    import cv2
    import numpy as np

    try:
        stat = path.stat()
        revision = (int(stat.st_size), int(stat.st_mtime_ns))
    except OSError:
        revision = (0, 0)
    if side not in _TACTILE_COLUMNS:
        return None
    cache_key = (str(path.resolve()), revision, int(frame_index), side)
    with _TACTILE_PREVIEW_LOCK:
        cached = _TACTILE_FIELD_CACHE.get(cache_key)
        if cached is not None:
            _TACTILE_FIELD_CACHE.move_to_end(cache_key)
            return cached

    matrix = _read_tactile_frame(path, frame_index, side)
    if matrix is None:
        return None
    # Match the acquisition-side heatmap exactly: the color field is the
    # calibrated normal-pressure channel fz.  fx/fy remain available in the
    # numeric force badge and are intentionally not mixed into this primary
    # pressure visualization.
    pressure = np.maximum(matrix[2], 0.0)
    pressure = cv2.GaussianBlur(pressure, (3, 3), 0.8)
    valid = pressure[pressure > 0.01]
    lo = 0.0
    hi = 1.0
    if valid.size > 50:
        lo = float(np.percentile(valid, 10))
        hi = max(float(np.percentile(valid, 98)) * 1.3, lo + 0.5)
    field = np.ascontiguousarray(pressure, dtype="<f4")
    result = (field.tobytes(), lo, hi)
    with _TACTILE_PREVIEW_LOCK:
        _TACTILE_FIELD_CACHE[cache_key] = result
        _TACTILE_FIELD_CACHE.move_to_end(cache_key)
        while len(_TACTILE_FIELD_CACHE) > _TACTILE_FIELD_CACHE_MAX:
            _TACTILE_FIELD_CACHE.popitem(last=False)
    return result


_TACTILE_RANGE_CACHE: OrderedDict[tuple, bytes] = OrderedDict()
_TACTILE_RANGE_CACHE_MAX = 24


def _tactile_range_payload(path: Path, first: int, last: int,
                           side: str) -> bytes | None:
    """Encode a run of consecutive fz fields as R8 plus per-frame min/max.

    Body layout: ``frames * 250 * 250`` bytes of R8 (one byte per cell, row
    major) followed by ``frames * 2`` little-endian float32 values holding
    each frame's ``lo``/``hi`` normalisation pair.  One request per window
    replaces one 250 KB request per frame, which is what saturated the
    browser's socket buffers (net::ERR_NO_BUFFER_SPACE) during playback.
    """
    import cv2
    import numpy as np
    import pyarrow.parquet as pq

    column = _TACTILE_COLUMNS.get(side)
    if column is None or last <= first:
        return None
    try:
        stat = path.stat()
        revision = f"{int(stat.st_size):x}-{int(stat.st_mtime_ns):x}"
    except OSError:
        revision = "0-0"
    cache_key = (str(path.resolve()), revision, int(first), int(last), side)
    with _TACTILE_PREVIEW_LOCK:
        cached = _TACTILE_RANGE_CACHE.get(cache_key)
        if cached is not None:
            _TACTILE_RANGE_CACHE.move_to_end(cache_key)
            return cached
    try:
        table = pq.read_table(
            path,
            columns=["frame_index", column],
            filters=[("frame_index", ">=", int(first)),
                     ("frame_index", "<", int(last))],
        )
    except Exception:
        return None
    frames = [int(value) for value in table.column("frame_index").to_pylist()]
    # Read the list column straight out of the arrow buffers: as_py() would
    # materialise 187500 Python ints per frame (~100 ms each).
    import pyarrow as pa
    column_data = table.column(column)
    if isinstance(column_data, pa.ChunkedArray):
        column_data = column_data.combine_chunks()
    offsets = np.asarray(column_data.offsets, dtype=np.int64)
    values = np.asarray(column_data.values, dtype=np.int16)
    rows = {frame: values[offsets[index]:offsets[index + 1]]
            for index, frame in enumerate(frames)}
    pixels = _TACTILE_SHAPE[0] * _TACTILE_SHAPE[1]
    blocks: list[bytes] = []
    pairs: list[float] = []
    for frame in range(int(first), int(last)):
        matrix = _decode_tactile_matrix(rows.get(frame))
        if matrix is None:
            blocks.append(bytes(pixels))
            pairs.extend((0.0, 1.0))
            continue
        pressure = np.maximum(matrix[2], 0.0)
        pressure = cv2.GaussianBlur(pressure, (3, 3), 0.8)
        valid = pressure[pressure > 0.01]
        lo = 0.0
        hi = 1.0
        if valid.size > 50:
            lo = float(np.percentile(valid, 10))
            hi = max(float(np.percentile(valid, 98)) * 1.3, lo + 0.5)
        scaled = np.clip((pressure - lo) / (hi - lo + 1e-6), 0.0, 1.0)
        blocks.append(np.rint(scaled * 255.0).astype(np.uint8).tobytes())
        pairs.extend((lo, hi))
    payload = b"".join(blocks) + np.asarray(pairs, dtype="<f4").tobytes()
    with _TACTILE_PREVIEW_LOCK:
        _TACTILE_RANGE_CACHE[cache_key] = payload
        _TACTILE_RANGE_CACHE.move_to_end(cache_key)
        while len(_TACTILE_RANGE_CACHE) > _TACTILE_RANGE_CACHE_MAX:
            _TACTILE_RANGE_CACHE.popitem(last=False)
    return payload


def _tactile_preview_png(path: Path, frame_index: int, side: str) -> bytes | None:
    """Render the numeric force field as a legacy WebP preview."""
    import cv2
    import numpy as np

    try:
        stat = path.stat()
        revision = (int(stat.st_size), int(stat.st_mtime_ns))
    except OSError:
        revision = (0, 0)
    cache_key = (str(path.resolve()), revision, int(frame_index), side)
    with _TACTILE_PREVIEW_LOCK:
        cached = _TACTILE_PREVIEW_CACHE.get(cache_key)
        if cached is not None:
            _TACTILE_PREVIEW_CACHE.move_to_end(cache_key)
            return cached
    field_data = _tactile_force_field(path, frame_index, side)
    if field_data is None:
        return None
    raw, lo, hi = field_data
    force_magnitude = np.frombuffer(raw, dtype="<f4").reshape(_TACTILE_SHAPE)
    force_magnitude = np.clip(
        (force_magnitude - lo) / (hi - lo + 1e-6), 0.0, 1.0
    )
    gray = np.uint8(np.round(force_magnitude * 255.0))
    # Keep the complete 250x250 field visible as a real heatmap.  JET's
    # lowest value is the cold blue end of the scale, so zero-pressure cells
    # remain visible instead of turning the preview into a mostly-black image
    # with a thin coloured line.  This changes only the visualization; the
    # decoded force matrix and its zero cells are not modified.
    colored = cv2.applyColorMap(gray, cv2.COLORMAP_JET)
    _, encoded = cv2.imencode(".webp", colored,
                              [cv2.IMWRITE_WEBP_QUALITY, 90])
    result = encoded.tobytes()
    with _TACTILE_PREVIEW_LOCK:
        _TACTILE_PREVIEW_CACHE[cache_key] = result
        _TACTILE_PREVIEW_CACHE.move_to_end(cache_key)
        while len(_TACTILE_PREVIEW_CACHE) > _TACTILE_PREVIEW_CACHE_MAX:
            _TACTILE_PREVIEW_CACHE.popitem(last=False)
    return result


@router.get("/{episode_id}/tactile-matrix-data/{frame_index}")
def get_tactile_matrix_data(
    episode_id: str,
    frame_index: int,
    side: str = Query("left", pattern="^(left|right)$"),
):
    """Return one numeric all-component force field for WebGL2 rendering."""
    if frame_index < 0:
        raise HTTPException(status_code=400, detail="frame_index must be non-negative")
    path = _tactile_episode_file(episode_id)
    if path is None or not _tactile_matrix_columns(path).intersection(_TACTILE_COLUMNS.values()):
        raise HTTPException(status_code=404, detail="force matrices are unavailable")
    field = _tactile_force_field(path, frame_index, side)
    if field is None:
        raise HTTPException(status_code=404, detail=f"no force matrix at frame {frame_index}")
    raw, lo, hi = field
    return Response(
        content=raw,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Force-Width": str(_TACTILE_SHAPE[1]),
            "X-Force-Height": str(_TACTILE_SHAPE[0]),
            "X-Force-Dtype": "float32-le",
            "X-Force-Min": repr(lo),
            "X-Force-Max": repr(hi),
        },
    )


@router.get("/{episode_id}/tactile-matrix-meta")
def get_tactile_matrix_meta(episode_id: str):
    path = _tactile_episode_file(episode_id)
    columns = _tactile_matrix_columns(path) if path else set()
    sides = [side for side, column in _TACTILE_COLUMNS.items() if column in columns]
    ep = get_episode(episode_id)
    try:
        stat = path.stat() if path else None
        revision = f"{int(stat.st_size):x}-{int(stat.st_mtime_ns):x}" if stat else None
    except OSError:
        revision = None
    force_series = _read_tactile_force_series(path) if path else None
    return {
        "available": bool(sides),
        "episode_id": episode_id,
        "sides": sides,
        "shape": [250, 250, 3],
        "units": ["mN", "mN", "mN"],
        "channel_order": ["fx", "fy", "fz"],
        "layout": "HWC",
        "encoding": "row_diff_quantized",
        "force_order": ["fx", "fy", "fz"],
        "force_units": "mN",
        "force_series": force_series,
        "artifact_revision": revision,
        "frame_count": int(ep.get("frame_count") or 0) if ep else 0,
        "fps": float(ep.get("fps") or 30) if ep else 30.0,
    }


@router.get("/{episode_id}/tactile-matrix-range")
def get_tactile_matrix_range(
    episode_id: str,
    side: str = Query("left", pattern="^(left|right)$"),
    start: int = Query(0, ge=0),
    end: int = Query(0, ge=0),
    limit: int = Query(96, ge=1, le=256),
):
    """Return a run of consecutive fz fields for one side (R8 + min/max)."""
    path = _tactile_episode_file(episode_id)
    if path is None or not _tactile_matrix_columns(path).intersection(
            _TACTILE_COLUMNS.values()):
        raise HTTPException(status_code=404, detail="force matrices are unavailable")
    ep = get_episode(episode_id)
    total = int(ep.get("frame_count") or 0) if ep else 0
    first = max(0, int(start))
    last = int(end) if int(end) > first else first + int(limit)
    last = min(last, first + int(limit))
    if total:
        first = min(first, max(0, total - 1))
        last = min(last, total)
    if last <= first:
        raise HTTPException(status_code=404, detail="empty range")
    payload = _tactile_range_payload(path, first, last, side)
    if payload is None:
        raise HTTPException(status_code=404, detail="no force matrices in range")
    return Response(
        content=payload,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Force-First": str(first),
            "X-Force-Frames": str(last - first),
            "X-Force-Width": str(_TACTILE_SHAPE[1]),
            "X-Force-Height": str(_TACTILE_SHAPE[0]),
            "X-Force-Encoding": "r8+minmax",
        },
    )


@router.get("/{episode_id}/tactile-matrix/{frame_index}")
def get_tactile_matrix_preview(
    episode_id: str,
    frame_index: int,
    side: str = Query("left", pattern="^(left|right)$"),
):
    if frame_index < 0:
        raise HTTPException(status_code=400, detail="frame_index must be non-negative")
    path = _tactile_episode_file(episode_id)
    if path is None or not _tactile_matrix_columns(path).intersection(_TACTILE_COLUMNS.values()):
        raise HTTPException(status_code=404, detail="force matrices are unavailable")
    image = _tactile_preview_png(path, frame_index, side)
    if image is None:
        raise HTTPException(status_code=404, detail=f"no force matrix at frame {frame_index}")
    return Response(
        content=image,
        media_type="image/webp",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.get("/{episode_id}/slam-trajectory")
def get_slam_trajectory(
    episode_id: str,
    start_frame: int | None = Query(None, ge=0),
    end_frame: int | None = Query(None, ge=0),
    max_points: int = Query(2400, ge=1, le=10000),
):
    """Return synchronized SLAM poses and a display-ready 3D trajectory.

    Without a range, long episodes return a decimated trajectory.  With a
    range, every valid pose in that frame window is returned so the browser
    can place the current marker exactly without one request per frame.
    Pose order is ``x,y,z,qx,qy,qz,qw`` and coordinates are metres when the
    collector follows the UMI SLAM contract.
    """
    ep = get_episode(episode_id)
    if ep is None:
        return {"available": False, "episode_id": episode_id, "frames": [], "trajectory": []}
    sdir = Path(ep["path"])
    files = _canonical_episode_data_files(sdir, episode_id)
    if not files:
        return {"available": False, "episode_id": episode_id, "frames": [], "trajectory": []}
    revision, rows = _read_slam_rows(files[0])
    frame_count = int(ep.get("frame_count") or 0)
    if rows:
        frame_count = max(frame_count, int(rows[-1]["f"]) + 1)

    ranged = start_frame is not None or end_frame is not None
    if ranged:
        start = max(0, int(start_frame or 0))
        end = max(start, int(end_frame if end_frame is not None else start))
        selected = [row for row in rows if start <= row["f"] <= end]
        selected = _decimate_slam_rows(selected, max_points)
        trajectory = selected
    else:
        start = 0
        end = max(0, frame_count - 1)
        trajectory = _decimate_slam_rows(rows, max_points)
        # A short episode is returned in full, while long episodes use the
        # same bounded sample for the initial current-frame lookup.
        selected = trajectory
    return {
        "available": bool(rows),
        "episode_id": episode_id,
        "frame_count": frame_count,
        "fps": float(ep.get("fps") or 30),
        "unit": "m",
        "pose_order": ["x", "y", "z", "qx", "qy", "qz", "qw"],
        "gripper_state_order": ["percent", "gripped", "raw"] if any(
            row.get("g") is not None for row in rows) else None,
        "coordinate_frame": "slam_world",
        "artifact_revision": revision,
        "valid_frames": len(rows),
        "start": start,
        "end": end,
        "frames": selected,
        "trajectory": trajectory,
    }

async def _get_hand_keypoints_data(
    episode_id: str,
    frame_index: int,
) -> dict | None:
    """Read hand keypoint + gesture data from parquet for a given frame.

    Looks for auto_labels columns (hand_0_keypoints, hand_0_gesture, etc.)
    or observation.hand_* naming variants.

    Returns a dict with hand_0, hand_1, two_hand_distance, contact, source
    or None if no hand data is found at all.
    """
    import numpy as np
    import pandas as pd

    ep = get_episode(episode_id)
    if ep is None:
        return None

    session_dir = Path(ep["path"])
    if not session_dir or not session_dir.exists():
        return None

    canonical_files = _canonical_episode_data_files(session_dir, episode_id)
    for _data_dir in (session_dir / "data",):
        for parq_file in canonical_files:
            parq_str = str(parq_file)
            cache_key = f"{episode_id}:{parq_str}"

            try:
                if cache_key in _parquet_df_cache:
                    df, _ = _parquet_df_cache[cache_key]
                else:
                    df = pd.read_parquet(parq_file)
                    available = []
                    for col in df.columns:
                        if col.startswith("observation."):
                            sname = col.split(".", 1)[1]
                            if sname not in available:
                                available.append(sname)
                    if len(_parquet_df_cache) >= _MAX_PARQUET_CACHE:
                        _parquet_df_cache.pop(next(iter(_parquet_df_cache)))
                    _parquet_df_cache[cache_key] = (df, available)

                row_df = df[df["frame_index"] == frame_index]
                if len(row_df) == 0:
                    continue
                row = row_df.iloc[0]

                # Detect which naming convention is used
                result = {"frame_index": frame_index, "source": "none"}

                # Try auto_labels naming: hand_0_keypoints, hand_0_gesture, ...
                has_auto_labels = "hand_0_gesture" in df.columns or "hand_0_keypoints" in df.columns
                # Try observation.* naming: observation.hand_keypoints_left, ...
                has_obs_labels = any(c.startswith("observation.hand_") for c in df.columns)

                if has_auto_labels:
                    result["source"] = "detected"
                    for hand_prefix in ("hand_0", "hand_1"):
                        hand_data = {}
                        kp_col = f"{hand_prefix}_keypoints"
                        gesture_col = f"{hand_prefix}_gesture"

                        if kp_col in df.columns:
                            val = row[kp_col]
                            if val is not None and not (isinstance(val, float) and pd.isna(val)):
                                hand_data["keypoints"] = val if isinstance(val, list) else val.tolist() if hasattr(val, 'tolist') else None

                        if gesture_col in df.columns:
                            val = row[gesture_col]
                            if val is not None and not (isinstance(val, float) and pd.isna(val)):
                                hand_data["gesture"] = str(val)

                        for extra_col in ("extended", "extended_count", "fist", "pinch",
                                          "center_x", "center_y", "motion"):
                            col_name = f"{hand_prefix}_{extra_col}"
                            if col_name in df.columns:
                                val = row[col_name]
                                if val is not None and not (isinstance(val, float) and pd.isna(val)):
                                    if isinstance(val, (list, np.ndarray)):
                                        hand_data[extra_col] = val if isinstance(val, list) else val.tolist()
                                    elif isinstance(val, (np.integer,)):
                                        hand_data[extra_col] = int(val)
                                    elif isinstance(val, (np.floating,)):
                                        hand_data[extra_col] = float(val)
                                    elif isinstance(val, (np.bool_,)):
                                        hand_data[extra_col] = bool(val)
                                    else:
                                        hand_data[extra_col] = val

                        result[hand_prefix] = hand_data if hand_data else None

                    for global_col in ("two_hand_distance", "contact"):
                        if global_col in df.columns:
                            val = row[global_col]
                            if val is not None and not (isinstance(val, float) and pd.isna(val)):
                                if isinstance(val, (np.floating,)):
                                    result[global_col] = float(val)
                                elif isinstance(val, (np.integer,)):
                                    result[global_col] = int(val)
                                else:
                                    result[global_col] = val

                elif has_obs_labels:
                    result["source"] = "detected"
                    for hand_prefix, label in (("hand_0", "left"), ("hand_1", "right")):
                        hand_data = {}
                        kp_col = f"observation.hand_keypoints_{label}"
                        gesture_col = f"observation.hand_gesture"

                        if kp_col in df.columns:
                            val = row[kp_col]
                            if val is not None and not (isinstance(val, float) and pd.isna(val)):
                                hand_data["keypoints"] = val if isinstance(val, list) else val.tolist() if hasattr(val, 'tolist') else None

                        if label == "left" and gesture_col in df.columns:
                            val = row[gesture_col]
                            if val is not None and not (isinstance(val, float) and pd.isna(val)):
                                hand_data["gesture"] = str(val)

                        result[hand_prefix] = hand_data if hand_data else None

                if result.get("source") != "none":
                    return _sanitize_for_json(result)

            except Exception:
                _parquet_df_cache.pop(cache_key, None)
                continue

    return None


# ── Helper: extract sensor data for a frame ───────────

# Parquet DataFrame cache — avoid re-reading the same file on every frame request.
# Key=(episode_id, parq_path), holds the full df + available sensor list.
_parquet_df_cache: dict[str, tuple["pd.DataFrame", list[str]]] = {}
_MAX_PARQUET_CACHE = 5


async def _get_sensor_frame_data(
    episode_id: str,
    frame_index: int,
    sensor: str | None,
) -> tuple["np.ndarray", list[str]]:
    """Read a single sensor observation from parquet for a given frame.

    Returns (frame_data, available_sensors).
    ``sensor`` selects the target column (e.g. ``"sensors_right"``);
    when None, the first ``observation.*`` column is used (backward compat).

    DataFrame is cached — first frame reads parquet; subsequent frames query
    the cached df in memory. Batch directory comes from localstore (no DB).
    """
    import numpy as np
    import pandas as pd

    from app.localstore import get_episode
    ep = get_episode(episode_id)
    available: list[str] = []

    if not ep:
        return None, available

    session_dir = Path(ep["path"])
    if not session_dir.is_dir():
        return None, available

    canonical_files = _canonical_episode_data_files(session_dir, episode_id)
    for _data_dir in (session_dir / "data",):
        for parq_file in canonical_files:
            parq_str = str(parq_file)
            cache_key = f"{episode_id}:{parq_str}"
            try:
                # Cache hit — query in memory
                if cache_key in _parquet_df_cache:
                    df, available = _parquet_df_cache[cache_key]
                else:
                    df = pd.read_parquet(parq_file)
                    # Collect available sensor names
                    for col in df.columns:
                        if col.startswith("observation."):
                            sname = col.split(".", 1)[1]
                            if sname not in available:
                                available.append(sname)
                    # Cache with LRU eviction
                    if len(_parquet_df_cache) >= _MAX_PARQUET_CACHE:
                        _parquet_df_cache.pop(next(iter(_parquet_df_cache)))
                    _parquet_df_cache[cache_key] = (df, available)

                row_df = df[df["frame_index"] == frame_index]
                if len(row_df) == 0:
                    continue

                row = row_df.iloc[0]

                if sensor:
                    target_col = f"observation.{sensor}"
                    if target_col not in df.columns:
                        # 新格式兼容:data/<glove_name>/ 独立 parquet,
                        # 列名可能就是手套名(无 observation. 前缀)。
                        target_col = sensor
                    if target_col in df.columns:
                        val = row[target_col]
                        if isinstance(val, (list, np.ndarray)):
                            return np.array(val, dtype=np.float32), available
                    # This parquet lacks the requested sensor (e.g. left_glove
                    # file has no observation.right_glove) → keep scanning.
                    continue

                # Backward compat: no sensor specified → first observation.* column
                for col in df.columns:
                    if col.startswith("observation."):
                        val = row[col]
                        if isinstance(val, (list, np.ndarray)):
                            return np.array(val, dtype=np.float32), available
                        break
                continue
            except Exception:
                # On error, evict stale cache entry and continue
                _parquet_df_cache.pop(cache_key, None)
                continue

    return None, available


# ── Heatmap generation ───────────────────────────────

@router.get("/{episode_id}/heatmap/{frame_index}")
async def get_heatmap_frame(
    episode_id: str,
    frame_index: int,
    sensor: str | None = None,
):
    """Generate a single heatmap frame image using OpenCV (Viridis colormap).

    Returns a PNG image of the 16x16 sensor heatmap with blur smoothing.
    Optional ``?sensor=`` selects a specific observation column (e.g. sensors_right).
    """
    import numpy as np
    import cv2

    frame_data, available = await _get_sensor_frame_data(episode_id, frame_index, sensor)

    if frame_data is None:
        detail = f"No data for frame {frame_index}"
        if available:
            detail += f". Available sensors: {available}"
        raise HTTPException(status_code=404, detail=detail)

    # Reshape to 16x16 if flat
    if len(frame_data.shape) == 1:
        if len(frame_data) == 256:
            frame_data = frame_data.reshape(16, 16)
        else:
            side = int(np.sqrt(len(frame_data)))
            frame_data = frame_data.reshape(side, side)

    # Normalize
    data = np.maximum(0, frame_data)
    vmax = max(float(data.max()), 1.0)
    norm = (data / vmax * 255).astype(np.uint8)

    # Resize with interpolation for smooth look (like the OpenCV reference)
    target_size = 512
    resized = cv2.resize(norm, (target_size, target_size), interpolation=cv2.INTER_CUBIC)

    # Gaussian blur for smoothness
    resized = cv2.GaussianBlur(resized, (7, 7), 0)

    # Apply Viridis colormap
    colored = cv2.applyColorMap(resized, cv2.COLORMAP_VIRIDIS)

    # Encode as PNG
    _, buf = cv2.imencode(".png", colored)
    return StreamingResponse(
        iter([buf.tobytes()]),
        media_type="image/png",
        headers={"Content-Type": "image/png", "Cache-Control": "public, max-age=3600"},
    )


# ── Bionic Hand rendering (仿生手掌) ──────────────────

# 传感器区域映射（axis_order 对齐 hand_ble_config.json，col_row = rows→x/cols→y）
HAND_REGIONS = {
    "thumb":  {"name": "拇指",  "rows": [0, 1, 2],        "cols": [14, 12, 13, 15], "axis": "col_row"},
    "index":  {"name": "食指",  "rows": [3, 4, 5],        "cols": [14, 12, 13, 15], "axis": "col_row"},
    "middle": {"name": "中指",  "rows": [6, 7, 8],        "cols": [14, 12, 13, 15], "axis": "col_row"},
    "ring":   {"name": "无名指","rows": [9, 10, 11],     "cols": [14, 12, 13, 15], "axis": "col_row"},
    "pinky":  {"name": "小拇指","rows": [12, 13, 14],    "cols": [14, 12, 13, 15], "axis": "col_row"},
    "palm":   {"name": "掌心",  "rows": list(range(15)),  "cols": [10, 9, 8, 6, 4], "axis": "col_row"},
}

# 布局: 手指在上排, 掌心在第2排居中
FINGER_CELL = 16
PALM_CELL = 16
FINGER_W = 3 * FINGER_CELL   # 3 rows → 3 cells wide  (col_row: rows→x)
FINGER_H = 4 * FINGER_CELL   # 4 cols → 4 cells tall  (col_row: cols→y)
# 掌心 axis=col_row: 15行→15列宽, 5列→5行高 → 横着的
PALM_W = 15 * PALM_CELL      # 15 columns wide
PALM_H = 5 * PALM_CELL       # 5 rows tall

# 5 fingers equally spaced at top
FINGER_Y = 20
FINGER_GAP = 15
FINGER_START_X = (800 - (5 * FINGER_W + 4 * FINGER_GAP)) // 2

LAYOUT = {
    "thumb":  (FINGER_START_X,                                                                    FINGER_Y),
    "index":  (FINGER_START_X + 1 * (FINGER_W + FINGER_GAP),                                     FINGER_Y),
    "middle": (FINGER_START_X + 2 * (FINGER_W + FINGER_GAP),                                     FINGER_Y),
    "ring":   (FINGER_START_X + 3 * (FINGER_W + FINGER_GAP),                                     FINGER_Y),
    "pinky":  (FINGER_START_X + 4 * (FINGER_W + FINGER_GAP),                                     FINGER_Y),
    "palm":   ((800 - PALM_W) // 2, FINGER_Y + FINGER_H + 25),   # 居中
}

# Canvas
CANVAS_W, CANVAS_H = 800, 220


@router.get("/{episode_id}/hand")
async def get_hand_frame(
    episode_id: str,
    frame_index: int = 0,
    mirror: bool = False,
    sensor: str | None = None,
    compact: bool = False,
):
    """仿生手掌传感器图 — 5 手指 + 掌心。

    - ``mirror=True`` 水平翻转适配左手视角。
    - ``?sensor=sensors_right`` 选择特定传感器列；省略则取第一个 observation.* 列。
    """
    import numpy as np
    import cv2

    frame_data, available = await _get_sensor_frame_data(episode_id, frame_index, sensor)

    if frame_data is not None:
        if len(frame_data.shape) == 1:
            frame_data = frame_data.reshape(16, 16)
        data = np.maximum(0, frame_data)
        vmax = max(float(data.max()), 1.0)
    else:
        # No sensor data → render empty hand outline
        data = np.zeros((16, 16), dtype=np.float32)
        vmax = 1.0

    # Viridis LUT
    viridis_lut = cv2.applyColorMap(
        np.arange(256, dtype=np.uint8).reshape(1, 256), cv2.COLORMAP_VIRIDIS
    )[0]  # (256, 3)

    # Canvas
    W, H = CANVAS_W, CANVAS_H
    canvas = np.full((H, W, 3), 18, dtype=np.uint8)

    for region_key in ["thumb", "index", "middle", "ring", "pinky", "palm"]:
        cfg = HAND_REGIONS[region_key]
        sx, sy = LAYOUT[region_key]
        rows, cols = cfg["rows"], cfg["cols"]
        cs = PALM_CELL if region_key == "palm" else FINGER_CELL

        # col_row: rows→x(宽), cols→y(高) — 对齐 zhenghe4.py 参考实现
        block_w, block_h = len(rows) * cs, len(cols) * cs
        for i, r in enumerate(rows):
            for j, c in enumerate(cols):
                val = data[r, c]
                x1, y1 = sx + i * cs, sy + j * cs
                x2, y2 = x1 + cs, y1 + cs

                idx = int(min(255, (val / vmax) * 255))
                b, g, r_col = int(viridis_lut[idx, 0]), int(viridis_lut[idx, 1]), int(viridis_lut[idx, 2])
                cv2.rectangle(canvas, (x1, y1), (x2, y2), (b, g, r_col), -1)

                lum = 0.299 * r_col + 0.587 * g + 0.114 * b
                tc = (0, 0, 0) if lum > 140 else (255, 255, 255)
                txt = str(int(val))
                (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_PLAIN, 0.45, 1)
                cv2.putText(canvas, txt, (x1 + (cs - tw) // 2, y1 + (cs + th) // 2),
                           cv2.FONT_HERSHEY_PLAIN, 0.45, tc, 1, cv2.LINE_AA)
                cv2.rectangle(canvas, (x1, y1), (x2, y2), (70, 70, 70), 1)

    # 水平翻转：左手数据需要镜像才能呈现"掌心朝前"的正面视角
    if mirror:
        canvas = cv2.flip(canvas, 1)

    # Compact preview only: remove the unused top/bottom canvas margin while
    # keeping the full-resolution sensor values and export data untouched.
    # The generated hand content occupies roughly y=20..202 on the 220px
    # canvas; keep a small safety margin for labels and antialiasing.
    if compact:
        canvas = canvas[8:208, :, :]

    _, buf = cv2.imencode(".png", canvas)
    return StreamingResponse(
        iter([buf.tobytes()]),
        media_type="image/png",
        headers={"Content-Type": "image/png", "Cache-Control": "public, max-age=3600"},
    )


# ── Hand Keypoints API ────────────────────────────────

@router.get("/{episode_id}/hand-keypoints")
async def get_hand_keypoints(
    episode_id: str,
    frame_index: int = 0,
):
    """Return hand keypoint coordinates + gesture labels for a single frame.

    Used by the frontend Canvas skeleton overlay to draw hand pose
    on top of the video in the Review page.

    Returns ``null`` body fields when no hand data is found (graceful degradation).
    """
    data = await _get_hand_keypoints_data(episode_id, frame_index)
    if data is None:
        return {
            "frame_index": frame_index,
            "source": "none",
            "hand_0": None,
            "hand_1": None,
            "two_hand_distance": None,
            "contact": None,
        }
    return data


@router.get("/{episode_id}/hand-keypoints/all")
async def get_hand_keypoints_all(
    episode_id: str,
):
    """Return ALL frames' hand keypoints in one request.

    Used by the frontend to preload skeleton data so that per-frame
    Canvas rendering is instant (zero HTTP during playback).

    Returns a compact list::

        [{"fi":0, "h0":{"kp":[[x,y],...21], "g":"open"}, "h1":{...}}, ...]
    """
    import numpy as np
    import pandas as pd

    ep = get_episode(episode_id)
    if ep is None:
        return {"frames": [], "source": "none"}

    sdir = Path(ep["path"])
    if not sdir or not sdir.exists():
        return {"frames": [], "source": "none"}

    parq_files = _canonical_episode_data_files(sdir, episode_id)
    if not parq_files:
        return {"frames": [], "source": "none"}

    try:
        df = pd.read_parquet(parq_files[0])
    except Exception:
        return {"frames": [], "source": "none"}

    # Detect which columns exist
    has_hand_cols = "hand_0_gesture" in df.columns or "hand_0_keypoints" in df.columns

    if not has_hand_cols:
        return {"frames": [], "source": "none"}

    frames = []
    for i in range(len(df)):
        row = df.iloc[i]
        fd = {"fi": int(row["frame_index"])}
        for hk in ("hand_0", "hand_1"):
            hand = {}
            kp = row.get(f"{hk}_keypoints")
            if kp is not None:
                kp_list = kp.tolist() if hasattr(kp, 'tolist') else kp
                if isinstance(kp_list, list) and len(kp_list) >= 21:
                    # Normalize to 0-1 (MediaPipe standard)
                    hand["kp"] = [
                        {"x": float(p[0]) / 640.0, "y": float(p[1]) / 480.0}
                        for p in kp_list[:21]
                    ]
            ges = row.get(f"{hk}_gesture")
            if ges is not None and not (isinstance(ges, float) and pd.isna(ges)):
                gs = str(ges)
                if gs != 'none':
                    hand["g"] = gs
            if hand:
                fd["h0" if hk == "hand_0" else "h1"] = hand
        frames.append(fd)

    return {
        "frames": frames,
        "total": len(frames),
        "source": "detected",
        "width": 640,    # reference: mediapipe normalized coords = pixel / this
        "height": 480,
    }


@router.get("/{episode_id}/hand-3d")
def get_hand_3d(
    episode_id: str,
    frame: int | None = None,
    source_key: str | None = Query(None),
    start_frame: int | None = Query(None, ge=0),
    end_frame: int | None = Query(None, ge=0),
):
    """3D 手部骨骼数据。

    - ``?frame=N``:只返回第 N 帧的手部 3D 点。
    - ``?start_frame=&end_frame=``:窗口拉取(播放期前端缓存 ±窗口,
      逐帧 HTTP 是卡顿主因;一次 parquet 读取 + 窗口内逐帧返回)。
    - 都不带:只返回元信息(render_video/标定等),用于按钮显隐。

    Returns::

        {"frame": 0, "h0": {"lm": [[x,y,z]x21], "label": "Right"},
         "h1": {...}, "render_video": "...", "baseline_m": ..., ...}
        # 或 meta-only(无 frame):{"frames": [], "source": ..., "render_video": ...}

    or ``{"frames": [], "source": "none"}`` when no hand_3d artifact exists.
    """
    import json as _json
    import numpy as _np
    import pandas as _pd

    ep = get_episode(episode_id)
    if ep is None:
        return {"frames": [], "source": "none"}
    sdir = Path(ep["path"])
    if not sdir or not sdir.exists():
        return {"frames": [], "source": "none"}

    # meta-only(不带 frame/窗口)是切批次必发的一次全目录递归
    # (rglob hand_3d)+ parquet 元信息读取,结果在两次 run 之间不变:
    # 缓存响应,上传/reprocess/run 完成时显式失效(media_cache)。
    meta_only = frame is None and start_frame is None and end_frame is None
    cacheable_meta = meta_only and not source_key
    if cacheable_meta:
        from app.media_cache import get_hand3d_meta
        cached = get_hand3d_meta(episode_id)
        if (cached is not None
                and cached.get("meta_schema_version") == 2
                and cached.get("artifact_revision")):
            return cached

    # 手部产物只从 canonical episode parquet 读取；渲染视频不再持久化。
    candidates = find_hand3d_candidates(
        sdir, str(source_key or ""), str(ep.get("id") or episode_id))
    if not candidates:
        none_resp = {"frames": [], "source": "none", "meta_schema_version": 2}
        if cacheable_meta:
            from app.media_cache import set_hand3d_meta
            set_hand3d_meta(episode_id, none_resp)
        return none_resp
    # Processing outputs are merged into one episode parquet.  ``source_key``
    # remains an API selector for old callers, but no sidecar file is used to
    # split or resurrect a second artifact.
    path = _pick_hand3d_parquet(candidates)
    if path is None:
        none_resp = {"frames": [], "source": "none", "meta_schema_version": 2}
        if cacheable_meta:
            from app.media_cache import set_hand3d_meta
            set_hand3d_meta(episode_id, none_resp)
        return none_resp

    def _row_value(row, names):
        for name in names:
            value = row.get(name)
            if value is None:
                continue
            if isinstance(value, (_np.floating, float)) and not _np.isfinite(value):
                continue
            return value
        return None

    def _hand_out(row) -> dict:
        fd: dict = {}
        for hk, side in (("hand_0", "left"), ("hand_1", "right")):
            # New processors use hand_N_*; older merged v2.1 exports use
            # observation.state.hand_left/right_3d_*.
            present = _row_value(row, [
                f"{hk}_present",
                f"observation.state.hand_{side}_3d_valid",
                f"observation.state.devices.head_right_rgb_4.hand_{side}_3d_valid",
            ])
            lm = _row_value(row, [
                f"{hk}_landmarks_3d",
                f"observation.state.hand_{side}_3d",
                f"observation.state.devices.head_right_rgb_4.hand_{side}_3d",
            ])
            if present is not None and not bool(present):
                continue
            if lm is None:
                continue
            try:
                arr = _np.asarray(lm, dtype=_np.float64).reshape(21, 3)
            except (TypeError, ValueError):
                continue
            # NaN = 无效点。标准 JSON 不允许 NaN 字面量(FastAPI 序列化
            # 直接 500),换成 null 透传,前端对非有限值跳过绘制。
            hand = {"lm": [
                [None if not _np.isfinite(v) else float(v) for v in pt]
                for pt in arr
            ]}
            label = _row_value(row, [
                f"{hk}_label",
                f"observation.hand_{hk.split('_')[-1]}_handedness",
            ]) or side.title()
            if label is not None:
                hand["label"] = str(label)
            err = _row_value(row, [
                f"{hk}_reprojection_error",
                f"observation.state.hand_{side}_3d_reprojection_error",
                f"observation.state.devices.head_right_rgb_4.hand_{side}_3d_reprojection_error",
            ])
            if err is not None:
                try:
                    err_f = float(err)
                except (TypeError, ValueError):
                    err_f = float("nan")
                if _np.isfinite(err_f):
                    hand["err"] = err_f
            fd["h0" if hk == "hand_0" else "h1"] = hand
        return fd

    # A stereo run stores one namespaced hand-3D result per RGB view in the
    # canonical parquet.  Select the view with the most valid landmarks;
    # ties are stable and prefer the left camera.  The selected view still
    # contains both physical hand slots (hand_0 and hand_1).
    columns = _hand3d_parquet_columns(path)
    source_keys = _hand3d_source_keys(columns)
    source_meta = {
        key: _hand3d_meta(path, key) for key in source_keys
    }
    selected_source = max(
        source_keys,
        key=lambda key: (
            int(source_meta[key].get("valid_landmark_points") or 0),
            int(source_meta[key].get("valid_hand_frames") or 0),
            1 if key.lower() in {"stereo_left", "left"} else 0,
            key,
        ),
        default=str(source_key or "canonical"),
    )
    active_source = (str(source_key) if source_key in source_meta
                     else selected_source)
    meta = _hand3d_meta(path, active_source)
    meta["selected_source_key"] = selected_source
    meta["available_sources"] = [
        {"source_key": key, **value}
        for key, value in sorted(source_meta.items())
    ]
    # “手部世界坐标”不是 hand_3d 文件存在就可以显示。必须同时满足：
    # 1) manifest 是深度抬升/相机米制产物；2) 当前批次确实还能找到
    # 原始或处理后的深度帧目录。MediaPipe 相对 3D 即使有 parquet，也
    # 只能用于骨骼处理结果，不能冒充世界坐标窗口。
    depth_available = has_depth_source(sdir, str(ep.get("id") or ""))
    meta["depth_available"] = depth_available
    meta["world_preview"] = bool(meta.get("source") == "depth_camera_meters"
                                  and meta.get("unit") == "camera_meters"
                                  and depth_available)
    meta["rgb_estimated_preview"] = bool(
        meta.get("mode") in {"rgb_estimated_3d", "black_glove_rgb_estimated_3d"}
        and meta.get("unit") == "rgb_estimated_meters")
    meta["space_preview"] = bool(meta["world_preview"]
                                  or meta["rgb_estimated_preview"])
    meta["preview_mode"] = (
        "depth_world" if meta["world_preview"] else
        "rgb_estimated" if meta["rgb_estimated_preview"] else "relative")
    for option in meta.get("available_sources") or []:
        option["depth_available"] = depth_available
        option["world_preview"] = bool(
            option.get("source") == "depth_camera_meters"
            and option.get("unit") == "camera_meters"
            and depth_available)
        option["rgb_estimated_preview"] = bool(
            option.get("mode") in {"rgb_estimated_3d", "black_glove_rgb_estimated_3d"}
            and option.get("unit") == "rgb_estimated_meters")
        option["space_preview"] = bool(option["world_preview"]
                                        or option["rgb_estimated_preview"])
        option["preview_mode"] = (
            "depth_world" if option["world_preview"] else
            "rgb_estimated" if option["rgb_estimated_preview"] else "relative")

    def _read_hand3d_frames():
        """Read whichever canonical or legacy 3D columns this episode has."""
        available = _hand3d_parquet_columns(path)
        field_map = _hand3d_column_map(available, active_source)
        wanted = ["frame_index"]
        for hk, side in (("hand_0", "left"), ("hand_1", "right")):
            wanted.extend([
                f"{hk}_present", f"{hk}_landmarks_3d", f"{hk}_label",
                f"{hk}_reprojection_error",
                f"observation.state.hand_{side}_3d_valid",
                f"observation.state.hand_{side}_3d",
                f"observation.state.hand_{side}_3d_reprojection_error",
                f"observation.state.devices.head_right_rgb_4.hand_{side}_3d_valid",
                f"observation.state.devices.head_right_rgb_4.hand_{side}_3d",
                f"observation.state.devices.head_right_rgb_4.hand_{side}_3d_reprojection_error",
                f"observation.hand_{hk.split('_')[-1]}_handedness",
            ])
        cols = [field_map.get(name, name) for name in wanted
                if field_map.get(name, name) in available]
        frame_df = _pd.read_parquet(path, columns=list(dict.fromkeys(cols)),
                                    engine="pyarrow")
        # Normalize the selected source namespace to the public field names
        # consumed by _hand_out().
        rename = {actual: public for public, actual in field_map.items()
                  if actual in frame_df.columns and actual != public}
        return frame_df.rename(columns=rename)

    if start_frame is not None:
        # 窗口拉取:一次读 parquet,窗口内逐帧返回(前端缓存整窗,
        # 播放期零网络 —— 替代逐帧 ?frame= 请求)
        try:
            df = _read_hand3d_frames()
        except Exception:
            return {"frames": [], "source": "none", "meta_schema_version": 2}
        end = int(end_frame) if end_frame is not None else int(start_frame)
        end = max(int(start_frame), end)
        window = df[(df["frame_index"] >= int(start_frame))
                    & (df["frame_index"] <= end)]
        frames = []
        for _, row in window.iterrows():
            fd = _hand_out(row)
            entry = {"f": int(row["frame_index"])}
            for k in ("h0", "h1"):
                if fd.get(k) is not None:
                    entry[k] = fd[k]
            frames.append(entry)
        return {**meta, "frames": frames, "start": int(start_frame),
                "end": end, "count": int(len(df))}
    if frame is not None:
        try:
            df = _read_hand3d_frames()
        except Exception:
            return {"frames": [], "source": "none", "meta_schema_version": 2}
        row = df[df["frame_index"] == int(frame)]
        fd = _hand_out(row.iloc[0]) if len(row) else {}
        return {**meta, "frame": int(frame), **fd}

    # meta-only:不带 frame 时不再全量返回(226MB 级响应会拖死前端)
    try:
        df = _pd.read_parquet(path, columns=["frame_index"])
        count = int(len(df))
    except Exception:
        count = 0
    response = {**meta, "frames": [], "count": count,
                "source": meta.get("source", "hand_3d")}
    if cacheable_meta:
        from app.media_cache import set_hand3d_meta
        set_hand3d_meta(episode_id, response)
    return response


_HAND_KP_DF_CACHE: dict[str, tuple] = {}
_HAND_KP_DF_MAX = 6


@router.get("/{episode_id}/{camera}/hand-keypoints")
def get_hand_keypoints_2d(
    episode_id: str,
    camera: str,
    start_frame: int = Query(0, ge=0),
    end_frame: int | None = Query(None, ge=0),
):
    """2D 手部关键点分段拉取 —— SVG 骨骼叠加层数据源(兼容两种手部模块)。

    通过 artifact_resolver 按 camera/source_key/manifest 查找：兼容
    hand_keypoints/<camera>.parquet、旧 node_*/hand_keypoints.parquet，
    以及含 2D 列的 hand_3d/hand_3d_right 产物。
    无产物/无 2D 列 → 404(前端按钮显示 No tracking data,而非报错)。

    返回:{"frames": [{"f": int, "h0": {"k": [[x,y]x21], "hand": "Left",
    "conf": 0.97}, "h1": {...} | null}, ...]},k = 归一化图像坐标
    (0..1),前端按播放器 letterbox 换算实际像素。数据量大,前端按
    帧窗口分段拉取(如 500 帧/次)。
    """
    import json as _json
    import numpy as _np
    import pandas as _pd

    safe_camera = camera.replace("/", "_").replace("\\", "_")
    ep = get_episode(episode_id)
    if ep is None:
        raise HTTPException(status_code=404, detail="Episode not found")
    sdir = Path(ep["path"])
    if not sdir or not sdir.is_dir():
        raise HTTPException(status_code=404, detail="Episode data missing")

    def _pick_best_keypoints(cands: list) -> Path:
        """mediapipe_hand 多节点产物(左右目各一个 node)按关键点非空
        数量择优 —— 路径排序会选中全空的那个节点。"""
        best, best_score = cands[0], -1
        for p in cands:
            try:
                sub = _pd.read_parquet(p)
                field_map = _hand3d_column_map(sub.columns, safe_camera)
                cols = [field_map.get(c) for c in
                        ("hand_0_keypoints", "hand_1_keypoints")
                        if field_map.get(c) in sub.columns]
                if not cols:
                    continue
                score = sum(int(sub[c].notna().sum()) for c in cols)
            except Exception:
                continue
            if score > best_score:
                best, best_score = p, score
        return best

    # Resolve the canonical merged episode parquet.  A 3D parquet is only used as a 2D overlay fallback
    # when it actually contains hand_*_keypoints columns below.
    candidates = find_hand_keypoint_candidates(sdir, safe_camera, str(ep.get("id") or ""))
    mode = "mediapipe_2d"
    if not candidates:
        candidates = find_hand3d_candidates(sdir, safe_camera, str(ep.get("id") or ""))
        mode = "stereo"
    if not candidates:
        raise HTTPException(status_code=404, detail="No tracking data yet")
    path = (_pick_best_keypoints(candidates)
            if mode == "mediapipe_2d" else _pick_hand3d_parquet(candidates))
    cache_key = f"{episode_id}:{path}"
    if cache_key in _HAND_KP_DF_CACHE:
        df = _HAND_KP_DF_CACHE[cache_key]
    else:
        try:
            df = _pd.read_parquet(path)
        except Exception:
            raise HTTPException(status_code=404, detail="Cannot read tracking data")
        if len(_HAND_KP_DF_CACHE) >= _HAND_KP_DF_MAX:
            _HAND_KP_DF_CACHE.pop(next(iter(_HAND_KP_DF_CACHE)))
        _HAND_KP_DF_CACHE[cache_key] = df
    # New stereo hand-3D results are source-qualified in the canonical table.
    # Normalize the requested camera's fields so the legacy overlay code can
    # keep consuming hand_N_keypoints without duplicating the parser.
    if "hand_0_keypoints" not in df.columns:
        field_map = _hand3d_column_map(df.columns, safe_camera)
        for public, actual in field_map.items():
            if public.startswith("hand_") and actual in df.columns \
                    and public not in df.columns:
                df[public] = df[actual]
    if "hand_0_keypoints" not in df.columns:
        # Canonical metric-3D data without 2D columns cannot be drawn over RGB.
        raise HTTPException(status_code=404, detail="Reprocess required for 2D keypoints")

    end = end_frame if end_frame is not None else start_frame
    end = max(start_frame, end)
    window = df[(df["frame_index"] >= int(start_frame)) & (df["frame_index"] <= int(end))]

    frames_out: list[dict] = []
    for _, row in window.iterrows():
        entry: dict = {"f": int(row["frame_index"])}
        for hk, short in (("hand_0", "h0"), ("hand_1", "h1")):
            hand: dict | None = None
            kp = row.get(f"{hk}_keypoints")
            # stereo 产物有 present 列;mediapipe_2d 无该列,以
            # keypoints 非空判存在。handedness 列名两种产物不同
            # (stereo: {hk}_label / mediapipe: {hk}_handedness)。
            # 2D review visibility is independent from metric-3D validity.
            # A depth gate may reject 3D while the RGB detector still has a
            # valid hand; newer artifacts declare that as *_2d_present.
            present = bool(row.get(
                f"{hk}_2d_present",
                row.get(f"{hk}_present", True),
            ))
            if present and kp is not None:
                # 三种产物形态都兼容:
                #  1) (21,3) 数组(stereo 嵌套列表在部分 pyarrow 版本读回)
                #  2) 扁平 63 位数组(手部骨骼产物实际读回形态;
                #     此前 7a50922 去掉 reshape 后走到逐点转换,scalar
                #     不可迭代被静默丢弃 → 双目骨骼叠加层消失)
                #  3) 对象嵌套数组(mediapipe_hand 产物的 pyarrow 列表
                #     序列化形态)→ 逐点容错转 float
                try:
                    arr = _np.asarray(kp, dtype=_np.float64)
                except (TypeError, ValueError):
                    arr = None
                if arr is not None and arr.dtype != object \
                        and arr.ndim == 1 and arr.size == 63:
                    arr = arr.reshape(21, 3)  # 扁平 63 位 → 21×3
                if arr is None or (arr.dtype == object) or arr.ndim != 2:
                    try:
                        arr = _np.array(
                            [[float(v) if v is not None else float("nan")
                              for v in pt] for pt in kp],
                            dtype=_np.float64)
                    except (TypeError, ValueError):
                        arr = None
                if arr is None or arr.ndim != 2 \
                        or arr.shape[0] < 21 or arr.shape[1] < 2:
                    continue  # 格式异常 → 跳过该手
                pts = [
                    [round(float(row_pt[0]), 4) if _np.isfinite(row_pt[0]) else None,
                     round(float(row_pt[1]), 4) if _np.isfinite(row_pt[1]) else None]
                    for row_pt in arr[:21]
                ]
                conf = float(row.get(f"{hk}_confidence") or 0.0)
                hand = {
                    "k": pts,
                    "hand": str(row.get(f"{hk}_label")
                               or row.get(f"{hk}_handedness") or ""),
                    # 非有限(预测补全帧无检测)→ null,防 JSON NaN 500
                    "conf": conf if _np.isfinite(conf) else None,
                }
            entry[short] = hand
        if entry.get("h0") or entry.get("h1"):
            frames_out.append(entry)
    return {"frames": frames_out, "start": int(start_frame), "end": int(end),
            "count": int(len(df))}


def _pick_hand3d_parquet(candidates: list[Path]) -> Path | None:
    """Select the canonical merged hand-3D parquet."""
    if not candidates:
        return None
    return candidates[0]
def _hand3d_source_keys(columns) -> list[str]:
    """Return source namespaces embedded in the canonical hand-3D table."""
    prefix = "processing.hand_3d."
    result = set()
    for name in columns:
        text = str(name)
        if not text.startswith(prefix):
            continue
        tail = text[len(prefix):]
        source = tail.split(".", 1)[0].strip()
        if source:
            result.add(source)
    if result:
        return sorted(result)
    # Legacy single-source tables used unqualified hand_N_* columns.
    if any(str(name).startswith("hand_0_") or str(name).startswith("hand_1_")
           for name in columns):
        return ["canonical"]
    return []


def _hand3d_column_map(columns, source_key: str | None) -> dict[str, str]:
    """Map public hand-3D field names to canonical source-qualified columns."""
    names = {str(name) for name in columns}
    source = str(source_key or "").strip()
    if source and source != "canonical":
        prefix = f"processing.hand_3d.{source}."
        mapped = {
            name[len(prefix):]: name for name in names
            if name.startswith(prefix)
        }
        if mapped:
            return mapped
    return {name: name for name in names
            if not name.startswith("processing.hand_3d.")}


def _hand3d_parquet_columns(path: Path) -> set[str]:
    """Read parquet column names without materializing any row data."""
    try:
        import pyarrow.parquet as _pq
        # ``schema.names`` exposes physical leaf names for nested list columns
        # (often just ``element``), which hides hand_N_landmarks_3d. Use the
        # logical Arrow schema so the frame reader sees the real top-level
        # parquet fields.
        return set(_pq.ParquetFile(path).schema_arrow.names)
    except Exception:
        import pandas as _pd
        return set(_pd.read_parquet(path, columns=[]).columns)


def _hand3d_meta(path: Path, source_key: str | None = None) -> dict:
    """Describe canonical 3D data using the coordinate contract it stores.

    RGB-only hand processing also writes 3D-looking arrays, but those points
    are an image-based estimate and must not be advertised as depth-world
    coordinates.  Infer the mode from the merged parquet instead of assuming
    every ``*_3d`` column came from a depth camera.
    """
    import pandas as _pd
    import numpy as _np

    mode = "depth_3d"
    unit = "camera_meters"
    source = "depth_camera_meters"
    coordinate_frame = "camera"
    metric_3d_available = True
    valid_hand_frames = 0
    valid_landmark_points = 0
    counted = False  # 计数是否真实执行过(前端据此区分"全空"与"统计失败")
    try:
        # The browser must be able to distinguish two workflow outputs that
        # happen to have the same episode id/frame count.  Size + mtime is a
        # cheap revision for the canonical parquet and is stable for the
        # lifetime of one published artifact.
        artifact_stat = path.stat()
        artifact_revision = (
            f"{int(artifact_stat.st_size):x}-"
            f"{int(artifact_stat.st_mtime_ns):x}"
        )
    except OSError:
        artifact_revision = "unknown"
    try:
        columns = _hand3d_parquet_columns(path)
        field_map = _hand3d_column_map(columns, source_key)
        source_columns = [name for name in field_map.values()
                          if name.endswith("_depth_source")]
        if source_columns:
            # New metric depth artifacts always carry *_depth_source. Avoid
            # decoding that entire object column during the click-time meta
            # request; the source namespace is enough to classify the preview.
            is_rgb_estimated = False
        else:
            is_rgb_estimated = any(
                name.endswith("hand_left_3d") or
                name.endswith("hand_right_3d")
                for name in field_map)
        if is_rgb_estimated:
            mode = "rgb_estimated_3d"
            unit = "rgb_estimated_meters"
            source = "rgb_camera_estimated"
            coordinate_frame = "camera_relative"
            metric_3d_available = False

        # Count real finite landmarks for source selection.  This is only
        # metadata for the review UI; it never changes the stored data.
        # 采样统计:均匀散布读 1/3 的 row group(NAS 上全量读这些列
        # 实测 ~20s),计数按采样率放大保持量级;采样失败回退全量读。
        present_raw = [field_map.get(name) for name in (
            "hand_0_present", "hand_1_present",
        ) if field_map.get(name)]
        landmark_raw = [field_map.get(name) for name in (
            "hand_0_landmarks_3d", "hand_1_landmarks_3d",
        ) if field_map.get(name)]
        # Presence booleans are enough for source selection and are much
        # cheaper than decoding nested 21x3 landmark arrays over SSHFS.
        count_raw = present_raw or landmark_raw

        def _count_rows(rows_iter) -> None:
            nonlocal valid_hand_frames, valid_landmark_points
            for _, count_row in rows_iter:
                row_has_hand = False
                for hk in ("hand_0", "hand_1"):
                    present_name = field_map.get(f"{hk}_present")
                    points_name = field_map.get(f"{hk}_landmarks_3d")
                    present = count_row.get(present_name) if present_name else True
                    points = count_row.get(points_name) if points_name else None
                    if present is False or points is None:
                        if (points is None and present is not None
                                and bool(present)):
                            valid_landmark_points += 21
                            row_has_hand = True
                        continue
                    try:
                        arr = _np.asarray(points, dtype=_np.float64)
                        if arr.size == 63:
                            arr = arr.reshape(21, 3)
                        finite = (arr.reshape(-1, 3).shape[1] == 3
                                  and _np.isfinite(arr.reshape(-1, 3)).all(axis=1))
                    except (TypeError, ValueError):
                        continue
                    valid_landmark_points += int(finite.sum())
                    row_has_hand = row_has_hand or bool(finite.any())
                if row_has_hand:
                    valid_hand_frames += 1

        if count_raw:
            count_columns = list(dict.fromkeys(count_raw))
            counted = False
            try:
                import pyarrow.parquet as _pq

                parquet = _pq.ParquetFile(path)
                total_rows = int(parquet.metadata.num_rows)
                groups = list(range(0, parquet.num_row_groups, 3)) or [0]
                sampled_rows = sum(
                    int(parquet.metadata.row_group(i).num_rows) for i in groups)
                sample_factor = (total_rows / max(1, sampled_rows))
                before_frames = valid_hand_frames
                before_points = valid_landmark_points
                for group_index in groups:
                    count_df = parquet.read_row_group(
                        group_index, columns=count_columns).to_pandas()
                    _count_rows(count_df.iterrows())
                valid_hand_frames = before_frames + int(
                    (valid_hand_frames - before_frames) * sample_factor)
                valid_landmark_points = before_points + int(
                    (valid_landmark_points - before_points) * sample_factor)
                counted = True
            except Exception:
                count_df = _pd.read_parquet(path, columns=count_columns,
                                            engine="pyarrow")
                _count_rows(count_df.iterrows())
                counted = True
    except Exception:
        # Metadata failure must not prevent the frame endpoint from working.
        pass

    return {
        "source_key": source_key or "canonical",
        "device_key": None,
        "device_name": None,
        "baseline_m": None,
        "calib_source": None,
        "method": "canonical_episode_data",
        "mode": mode,
        "unit": unit,
        "source": source,
        "coordinate_frame": coordinate_frame,
        "metric_3d_available": metric_3d_available,
        "processing_warnings": [],
        "width": 1280,
        "height": 800,
        "render_video": "",
        "valid_hand_frames": int(valid_hand_frames),
        "valid_landmark_points": int(valid_landmark_points),
        "counted": bool(counted),
        "artifact_revision": artifact_revision,
        "meta_schema_version": 2,
    }
def _parse_range(range_header: str, file_size: int) -> tuple[int, int]:
    m = re.match(r"bytes=(\d+)-(\d*)", range_header)
    if not m:
        raise HTTPException(status_code=416, detail="Invalid range header")
    start = int(m.group(1))
    end = int(end_str) if (end_str := m.group(2)) else file_size - 1
    if start >= file_size:
        raise HTTPException(status_code=416, detail="Range not satisfiable")
    return start, min(end, file_size - 1)


# ── Skeleton PNG rendering (Google MediaPipe drawing_utils) ──
# Zero ML inference — only uses drawing_utils to render pre-computed
# keypoint coordinates from parquet onto a transparent canvas.

@router.get("/{episode_id}/depth/{name}/{frame_index}")
async def get_depth_frame(
    episode_id: str,
    name: str,
    frame_index: int,
):
    """Deprecated PNG endpoint; active projects use the HEVC depth stream."""
    raise HTTPException(status_code=404,
                        detail="PNG depth frames are not part of the active storage format")


@router.get("/{episode_id}/depth-codes/{name}/{frame_index}")
async def get_depth_codes(
    episode_id: str,
    name: str,
    frame_index: int,
):
    """Return one canonical little-endian uint16 depth-code frame.

    This endpoint deliberately never calls a colormap and never writes a
    derived preview.  The browser owns the display-only JET conversion.
    """
    ep = get_episode(episode_id)
    if ep is None:
        raise HTTPException(status_code=404, detail="Episode not found")
    session_dir = Path(ep["path"])
    safe_name = str(name).replace("/", "_").replace("\\", "_").replace("..", "_")
    depth_video = find_depth_video(
        session_dir, safe_name, str(ep.get("id") or ""))
    if depth_video is None:
        raise HTTPException(status_code=404, detail=f"Depth source not found: {name}")
    try:
        codes = await asyncio.to_thread(
            _read_canonical_depth_codes, _depth_local_source(depth_video),
            frame_index)
    except IndexError:
        raise HTTPException(
            status_code=404,
            detail=f"Depth frame not found: {name}/{frame_index}",
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Unable to decode depth codes: {exc}",
        ) from exc
    return Response(
        content=codes.tobytes(order="C"),
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-Depth-Width": str(codes.shape[1]),
            "X-Depth-Height": str(codes.shape[0]),
            "X-Depth-Dtype": "uint16-le",
            "X-Depth-Encoding": "depth_mm_log_to_gray12le",
            "X-Depth-Min-Mm": "100",
            "X-Depth-Max-Mm": "5000",
            "X-Depth-Qmax": "4095",
            "X-Depth-Frame": str(frame_index),
        },
    )


@router.get("/depth-jet-lut")
async def get_depth_jet_lut():
    """Return the 256-entry OpenCV JET LUT for frontend-only rendering.

    The server returns palette constants only; it never renders or stores a
    colorized depth frame.  Canvas applies this LUT to the received codes.
    """
    import cv2
    import numpy as np

    values = np.arange(256, dtype=np.uint8).reshape(-1, 1)
    lut = cv2.applyColorMap(values, cv2.COLORMAP_JET).reshape(-1, 3)
    return JSONResponse({"name": "opencv_colormap_jet", "order": "bgr",
                         "values": lut.tolist()})


@router.get("/depth-color-lut")
async def get_depth_color_lut():
    """Return the 256-entry Google Turbo LUT (bgr order, Rerun-style depth
    colormap) for frontend-only depth colorization."""
    from app.lerobot_v21 import turbo_lut_256

    values = [[b, g, r] for (r, g, b) in turbo_lut_256()]
    return JSONResponse({"name": "google_turbo", "order": "bgr",
                         "values": values})


_DEPTH_STATS_CACHE: dict[str, dict] = {}
_DEPTH_STATS_CACHE_LOCK = threading.Lock()


def _depth_stats_for(source: Path) -> dict | None:
    """q01/q99 bounds for a depth source: memory cache → disk persistence.

    The disk copy is written once by preview generation (fingerprint-keyed)
    and survives restarts, so the stats endpoint and the preview stream can
    answer instantly without a second full-stream decode.
    """
    try:
        stats_key = f"{source.resolve()}@{source.stat().st_mtime_ns}"
    except OSError:
        return None
    with _DEPTH_STATS_CACHE_LOCK:
        cached = _DEPTH_STATS_CACHE.get(stats_key)
    if cached is not None:
        return cached
    try:
        from app.browser_preview import read_depth_stats

        disk = read_depth_stats(source)
        if disk is not None:
            with _DEPTH_STATS_CACHE_LOCK:
                _DEPTH_STATS_CACHE[stats_key] = disk
            return disk
    except Exception:
        pass
    return None


def _compute_depth_stats(source: Path) -> dict | None:
    """Sample-decode the stored depth video and return q01/q99 code bounds.

    One ffmpeg pass keeps ~32 spread frames; the sampled domain is the
    canonical uint16 gray12le codes, so the percentiles map directly onto the
    frontend color range (Rerun-style adaptive depth scaling).
    """
    import subprocess

    import numpy as np
    from app.lerobot_v21 import DEPTH_QMAX

    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg:
        return None
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-count_packets", "-show_entries", "stream=nb_read_packets",
         "-of", "csv=p=0", str(source)],
        check=False, capture_output=True, text=True, timeout=30)
    total = 0
    try:
        total = int(float(str(probe.stdout).strip().split(",")[0]))
    except (ValueError, IndexError):
        total = 0
    step = max(1, (total or 900) // 32)
    proc = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(source), "-map", "0:v:0",
         "-vf", f"select=not(mod(n\\,{step}))",
         "-f", "rawvideo", "-pix_fmt", "gray12le", "-"],
        check=False, capture_output=True, timeout=600)
    data = np.frombuffer(proc.stdout, dtype="<u2")
    if data.size < 2:
        return None
    # Keep the same 12-bit code domain as the raw frames served to the
    # browser.  Sampling with gray16le made ffmpeg bit-shift every value
    # (~x16) to fill the wider format, so q01/q99 came out ~16x too large
    # and the frontend Turbo ramp collapsed into its darkest segment.
    data = np.clip(data, 0, DEPTH_QMAX)
    return {
        "q01": float(np.percentile(data, 1)),
        "q99": float(np.percentile(data, 99)),
        "total_frames": total,
    }


@router.get("/depth-stats/{episode_id}/{name}")
async def get_depth_stats(episode_id: str, name: str):
    """q01/q99 depth-code bounds for adaptive frontend colorization."""
    ep = get_episode(episode_id)
    if ep is None:
        raise HTTPException(status_code=404, detail="Episode not found")
    session_dir = Path(ep["path"])
    safe_name = str(name).replace("/", "_").replace("\\", "_").replace("..", "_")
    source = find_depth_video(session_dir, safe_name, str(ep.get("id") or ""))
    if source is None:
        raise HTTPException(status_code=404, detail=f"Depth source not found: {name}")
    cached = _depth_stats_for(source)
    if cached is not None:
        return JSONResponse(
            cached,
            headers={"Cache-Control": "public, max-age=3600"})
    # No whole-video decode on a cache miss: preview generation computes and
    # persists these bounds during its own decode, so the frontend receives
    # the neutral full-range fallback now and re-fetches after the load.
    from app.lerobot_v21 import DEPTH_QMAX

    return JSONResponse(
        {"q01": 0.0, "q99": float(DEPTH_QMAX), "total_frames": 0,
         "pending": True},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{episode_id}/depth-preview/{name}/{frame_index}")
async def get_depth_preview(
    episode_id: str,
    name: str,
    frame_index: int,
):
    """Deprecated alias returning raw codes for the frontend renderer."""
    return await get_depth_codes(episode_id, name, frame_index)


@router.get("/{episode_id}/depth-codes-window/{name}")
async def get_depth_codes_window(
    episode_id: str,
    name: str,
    start_frame: int = Query(0, ge=0),
    end_frame: int | None = Query(None, ge=0),
    scale: int = Query(1, ge=1, le=8),
):
    """Return a short contiguous window of canonical uint16 depth codes.

    This is a transport optimization for the browser-only colorized renderer.
    The response is still the exact stored code domain (little-endian
    uint16), never a heatmap or a persisted colorized frame.  ``scale``
    downsamples each frame by an integer factor (INTER_AREA) to cut the
    payload; the frontend colormap pipeline is unaffected.
    """
    ep = get_episode(episode_id)
    if ep is None:
        raise HTTPException(status_code=404, detail="Episode not found")
    session_dir = Path(ep["path"])
    safe_name = str(name).replace("/", "_").replace("\\", "_").replace("..", "_")
    depth_video = find_depth_video(
        session_dir, safe_name, str(ep.get("id") or ""))
    if depth_video is None:
        raise HTTPException(status_code=404, detail=f"Depth source not found: {name}")
    requested_end = (start_frame + 11 if end_frame is None else int(end_frame))
    if requested_end < start_frame:
        raise HTTPException(status_code=400, detail="Invalid depth frame window")
    # Keep responses bounded while amortizing decoder/HTTP overhead. The
    # browser requests the same 120-frame (~4 second) window used by its
    # playback buffer, rather than repeatedly seeking every 60 frames.
    requested_end = min(requested_end, start_frame + 119)
    try:
        frames = await asyncio.to_thread(
            _read_canonical_depth_code_window,
            _depth_local_source(depth_video), start_frame, requested_end,
        )
    except IndexError:
        raise HTTPException(
            status_code=404,
            detail=f"Depth frame window not found: {name}/{start_frame}",
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Unable to decode depth codes: {exc}",
        ) from exc
    first = frames[0]
    if int(scale) > 1:
        frames = [_downscale_depth_codes(frame, scale) for frame in frames]
        first = frames[0]
    payload = b"".join(frame.tobytes(order="C") for frame in frames)
    actual_end = start_frame + len(frames) - 1
    return Response(
        content=payload,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-Depth-Width": str(first.shape[1]),
            "X-Depth-Height": str(first.shape[0]),
            "X-Depth-Dtype": "uint16-le",
            "X-Depth-Encoding": "depth_mm_log_to_gray12le",
            "X-Depth-Start": str(start_frame),
            "X-Depth-End": str(actual_end),
            "X-Depth-Frames": str(len(frames)),
            "X-Depth-Frame-Bytes": str(first.nbytes),
            "X-Depth-Scale": str(scale),
            "X-Depth-Qmax": "4095",
        },
    )


def _open_depth_code_stream(path: Path):
    """Open one dedicated sequential decoder and read its first code frame.

    The full-buffer endpoint intentionally does not use the small random-access
    reader pool.  One decoder owns one request, so the browser can receive the
    complete canonical code stream in decode order without competing with a
    frame-by-frame seek reader.
    """
    import numpy as np
    from app.lerobot_v21 import DepthVideoReader

    reader = DepthVideoReader(path)
    first = reader.read_codes()
    if first is None:
        reader.close()
        raise IndexError("empty depth video")
    return reader, np.ascontiguousarray(first, dtype="<u2").copy()


@router.get("/{episode_id}/depth-codes-full/{name}")
async def get_depth_codes_full(episode_id: str, name: str,
                               scale: int = Query(1, ge=1, le=8)):
    """Stream the complete stored depth-code sequence in frame order.

    This is a transport-only optimization for the frontend renderer.  The
    payload is raw little-endian uint16 gray12le code data; it is never JET,
    RGB, millimetres, or a persisted preview image.  ``scale`` downsamples
    each frame by an integer factor (INTER_AREA) before streaming.
    """
    ep = get_episode(episode_id)
    if ep is None:
        raise HTTPException(status_code=404, detail="Episode not found")
    session_dir = Path(ep["path"])
    safe_name = str(name).replace("/", "_").replace("\\", "_").replace("..", "_")
    depth_video = find_depth_video(
        session_dir, safe_name, str(ep.get("id") or ""))
    if depth_video is None:
        raise HTTPException(status_code=404, detail=f"Depth source not found: {name}")
    try:
        import numpy as np
        reader, first = await asyncio.to_thread(
            _open_depth_code_stream, _depth_local_source(depth_video))
    except IndexError:
        raise HTTPException(status_code=404, detail=f"Empty depth source: {name}")
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Unable to decode depth codes: {exc}",
        ) from exc

    if int(scale) > 1:
        first = _downscale_depth_codes(first, scale)
    width, height = int(first.shape[1]), int(first.shape[0])
    frame_bytes = int(first.nbytes)
    expected_frames = int(reader.frame_count or 0)
    # Give the browser a reusable, range-free cache entry for the sequential
    # raw-code stream. Without Content-Length many browsers keep a streamed
    # response only in a transient cache, so every re-open decodes gray12le
    # from FFmpeg again. The ETag changes when the source file is replaced;
    # this remains raw uint16 code data, never a colorized image.
    try:
        stat = depth_video.stat()
        etag = f'W/"{stat.st_size:x}-{stat.st_mtime_ns:x}"'
    except OSError:
        etag = None
    full_headers = {
        "Cache-Control": "public, max-age=3600",
        "X-Depth-Width": str(width),
        "X-Depth-Height": str(height),
        "X-Depth-Dtype": "uint16-le",
        "X-Depth-Encoding": "depth_mm_log_to_gray12le",
        "X-Depth-Frames": str(expected_frames),
        "X-Depth-Frame-Bytes": str(frame_bytes),
        "X-Depth-Start": "0",
        "X-Depth-End": str(max(0, expected_frames - 1)),
        "X-Depth-Scale": str(scale),
        "X-Depth-Qmax": "4095",
        "X-Depth-Transport": "full-sequential-raw-codes",
    }
    if expected_frames > 0:
        full_headers["Content-Length"] = str(expected_frames * frame_bytes)
    if etag:
        full_headers["ETag"] = etag

    def iterator():
        count = 0
        current = first
        histogram = np.zeros(4096, dtype=np.int64)
        stats_cache_key = None
        try:
            while current is not None:
                histogram += np.bincount(
                    current.reshape(-1).astype(np.int64),
                    minlength=4096)[:4096]
                yield current.tobytes(order="C")
                count += 1
                current = reader.read_codes()
                if current is not None:
                    current = np.ascontiguousarray(
                        current, dtype="<u2")
                    if int(scale) > 1:
                        current = _downscale_depth_codes(current, scale)
        finally:
            reader.close()
            # Piggyback the q01/q99 display stats on the full-stream pass:
            # the depth-stats endpoint then needs no second whole-video
            # decode (which competed with this one and doubled open time).
            try:
                stats_cache_key = (
                    f"{depth_video.resolve()}"
                    f"@{depth_video.stat().st_mtime_ns}")
                cumulative = np.cumsum(histogram)
                total_pixels = int(cumulative[-1])
                if total_pixels > 0:
                    q01 = float(np.searchsorted(
                        cumulative, total_pixels * 0.01, side="right"))
                    q99 = float(np.searchsorted(
                        cumulative, total_pixels * 0.99, side="right"))
                    with _DEPTH_STATS_CACHE_LOCK:
                        _DEPTH_STATS_CACHE[stats_cache_key] = {
                            "q01": q01,
                            "q99": q99,
                            "total_frames": int(reader.frame_count or count),
                        }
            except Exception:
                pass

    return StreamingResponse(
        iterator(),
        media_type="application/octet-stream",
        headers=full_headers,
    )


@router.get("/{episode_id}/skeleton/{frame_index}")
async def get_skeleton_frame(
    episode_id: str,
    frame_index: int,
):
    """Return a transparent PNG with hand skeleton drawn via MediaPipe.

    Uses ``mediapipe.tasks.python.vision.drawing_utils`` to render the
    21 hand landmarks with official MediaPipe styling (colours, connections,
    joint radii).  No model is loaded — only the drawing primitives run.
    """
    import numpy as np
    import cv2

    data = await _get_hand_keypoints_data(episode_id, frame_index)

    # Transparent RGBA canvas (640×480)
    canvas = np.zeros((480, 640, 4), dtype=np.uint8)

    if data is None or data.get("source") == "none":
        _, buf = cv2.imencode(".png", canvas)
        return StreamingResponse(
            iter([buf.tobytes()]), media_type="image/png",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    # Draw each hand using MediaPipe's official rendering utilities
    try:
        from mediapipe.tasks.python.vision import drawing_utils, drawing_styles
        from mediapipe.tasks.python.vision.hand_landmarker import HandLandmarksConnections
        from mediapipe.tasks.python.components.containers import landmark as lm

        for hand_key in ("hand_0", "hand_1"):
            hand = data.get(hand_key)
            if not hand or not hand.get("keypoints"):
                continue
            kp = hand["keypoints"]
            if len(kp) < 21:
                continue

            # Build NormalizedLandmark list (MediaPipe expects 0–1 coords)
            landmarks = []
            for pt in kp[:21]:
                l = lm.NormalizedLandmark()
                l.x = float(pt[0]) / 640.0
                l.y = float(pt[1]) / 480.0
                l.z = 0.0
                landmarks.append(l)

            # Draw on BGR temp (MediaPipe drawing_utils works on BGR)
            tmp = canvas[:, :, :3].copy()  # BGR channels (drop alpha)
            drawing_utils.draw_landmarks(
                tmp,
                landmarks,
                HandLandmarksConnections.HAND_CONNECTIONS,
                drawing_styles.get_default_hand_landmarks_style(),
                drawing_styles.get_default_hand_connections_style(),
            )
            # Merge drawn pixels back into RGBA canvas
            drawn_mask = (tmp > 10).any(axis=2)
            canvas[drawn_mask, :3] = tmp[drawn_mask]
            canvas[drawn_mask, 3] = 220  # alpha

    except ImportError:
        # Fallback: mediapipe not installed → use OpenCV rendering
        _draw_skeleton_fallback(canvas, data)

    _, buf = cv2.imencode(".png", canvas)
    return StreamingResponse(
        iter([buf.tobytes()]),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


def _draw_skeleton_fallback(canvas, data):
    """Minimal OpenCV hand skeleton drawing — used when mediapipe is unavailable."""
    import cv2
    connections = [
        (0,1),(1,2),(2,3),(3,4),(0,5),(5,6),(6,7),(7,8),
        (0,9),(9,10),(10,11),(11,12),(0,13),(13,14),(14,15),(15,16),
        (0,17),(17,18),(18,19),(19,20),(5,9),(9,13),(13,17),
    ]
    colors = [(48,255,48),(48,208,255),(255,48,255),(255,255,48),(48,144,255)]
    for hand_key in ("hand_0", "hand_1"):
        hand = data.get(hand_key)
        if not hand or not hand.get("keypoints") or len(hand["keypoints"]) < 21:
            continue
        pts = [(int(p[0]), int(p[1])) for p in hand["keypoints"][:21]]
        for f in range(5):
            c = colors[f]; rgba = (c[2],c[1],c[0],220)
            for i,j in connections[f*4:f*4+4]:
                cv2.line(canvas, pts[i], pts[j], rgba, 4, cv2.LINE_AA)
        for i,j in [(5,9),(9,13),(13,17)]:
            cv2.line(canvas, pts[i], pts[j], (255,255,255,128), 3, cv2.LINE_AA)
        for i,(x,y) in enumerate(pts):
            r = [7,4,4,3,2,4,4,3,2,4,4,3,2,4,4,3,2,4,4,3,2][i]
            oc = (255,255,255,220) if i==0 else (colors[(i-1)//4][2],colors[(i-1)//4][1],colors[(i-1)//4][0],220)
            cv2.circle(canvas, (x,y), r+1, oc, -1, cv2.LINE_AA)
            cv2.circle(canvas, (x,y), max(1,r-1), (255,255,255,200), -1, cv2.LINE_AA)


# ── Video path cache (avoids O(n) filesystem walk on every Range request) ──
_video_path_cache: dict[str, Path] = {}


def invalidate_episode_caches(episode_id, *, data_paths=(),
                              video_paths=(), episode_index=None) -> None:
    """Evict in-memory frame/parquet/reader state for a permanently deleted episode."""
    episode_id = str(episode_id)
    prefix = f"{episode_id}:"
    for key in list(_parquet_df_cache):
        if key.startswith(prefix):
            _parquet_df_cache.pop(key, None)
    for key in list(_video_path_cache):
        if key.startswith(f"{episode_id}_"):
            _video_path_cache.pop(key, None)
    # Depth readers/frames are keyed by resolved path.  Evict the exact
    # deleted paths when supplied; else fall back to an index-stem match.
    resolved = {str(Path(p).resolve()) for p in video_paths if p}
    if not resolved and episode_index is not None:
        stem = f"/episode_{int(episode_index):06d}."
        with _DEPTH_READER_POOL_LOCK:
            resolved = {key for key in _DEPTH_READER_POOL if stem in key}
            resolved.update(key[0] for key in _DEPTH_FRAME_CACHE
                            if stem in key[0])
    with _DEPTH_READER_POOL_LOCK:
        for key in list(_DEPTH_FRAME_CACHE):
            if key[0] in resolved:
                _DEPTH_FRAME_CACHE.pop(key, None)
        for key in resolved:
            state = _DEPTH_READER_POOL.pop(key, None)
            if state:
                try:
                    state["reader"].close()
                except Exception:
                    pass

def _find_video_file(episode_id: UUID, camera: str,
                     ep_meta: dict | None = None) -> Path:
    """Find one canonical source video for an episode.

    Only ``sessions/<project>/{data,meta,videos}`` is active.  Resolving by
    the episode index prevents a same-named stream from another batch from
    being returned after an upload or reprocess.
    """
    cache_key = f"{episode_id}_{camera}_raw"

    # Cache hit — verify file still exists (could have been deleted externally)
    if cache_key in _video_path_cache:
        p = _video_path_cache[cache_key]
        if p.exists():
            return p
        del _video_path_cache[cache_key]  # stale entry

    result: Path | None = None
    if ep_meta:
        from app.lerobot_v21 import iter_video_streams
        from app.project_dataset import episode_row, is_project_dataset

        project_root = Path(ep_meta.get("path") or "")
        if is_project_dataset(project_root):
            row = episode_row(project_root, str(ep_meta.get("id") or episode_id))
            if row is not None:
                episode_index = int(row.get("episode_index", 0))
                # Prefer an exact source key.  ``source_matches`` intentionally
                # supports legacy aliases (for example D435 and
                # D435_depth_rgb), but its normalized variants also share the
                # device prefix for D435_rgb/D435_depth.  Using that fuzzy
                # match in the first pass can therefore select the depth file
                # for an RGB request, depending on filesystem ordering.
                episode_stem = f"episode_{episode_index:06d}"
                streams = [
                    (source, path)
                    for source, path in iter_video_streams(project_root / "videos")
                    if path.stem == episode_stem
                ]
                for source, path in streams:
                    if source == camera:
                        result = path
                        break
                if result is None:
                    for source, path in streams:
                        if source_matches(source, str(camera)):
                            result = path
                            break

    if result is None:
        raise HTTPException(status_code=404, detail=f"Video not found: {episode_id}/{camera}")

    _video_path_cache[cache_key] = result
    return result


async def _h264_preview_path(source: Path) -> Path:
    from app.browser_preview import ensure_h264_preview

    try:
        return await asyncio.to_thread(ensure_h264_preview, source)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _depth_h264_preview_path(source: Path, q01: float, q99: float) -> Path:
    from app.browser_preview import ensure_depth_h264_preview

    try:
        return ensure_depth_h264_preview(source, q01=q01, q99=q99)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _video_response(file_path: Path, range_header: str | None):
    """Serve an MP4 with byte ranges required by HTML5/Plyr.

    Cacheable by the browser (ETag = size+mtime): repeat opens of RGB and
    depth preview streams are served from the browser cache, so the video
    element paints its first frame without waiting on a re-download.
    """
    file_path = Path(file_path)
    file_size = file_path.stat().st_size
    try:
        stat = file_path.stat()
        etag = f'W/"{stat.st_size:x}-{stat.st_mtime_ns:x}"'
    except OSError:
        etag = None
    common_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=3600",
    }
    if etag:
        common_headers["ETag"] = etag

    def _stream_full():
        with open(file_path, "rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                yield chunk

    if not range_header:
        return StreamingResponse(
            _stream_full(), media_type="video/mp4",
            headers={**common_headers, "Content-Length": str(file_size)},
        )

    start, end = _parse_range(range_header, file_size)
    content_length = end - start + 1

    def _stream_range():
        with open(file_path, "rb") as f:
            f.seek(start)
            remaining = content_length
            while remaining > 0:
                data = f.read(min(CHUNK_SIZE, remaining))
                if not data:
                    break
                yield data
                remaining -= len(data)

    return StreamingResponse(
        _stream_range(), status_code=206, media_type="video/mp4",
        headers={**common_headers,
                 "Content-Range": f"bytes {start}-{end}/{file_size}",
                 "Content-Length": str(content_length)},
    )


@router.post("/upload")
async def upload_video(
    session: AsyncSession = Depends(get_session),
    _: str = Depends(verify_api_key),
    video: UploadFile = File(...),
    task_description: str = Form(""),
    fps: int = Form(30),
    camera: str = Form("cam_main"),
):
    """Quick upload: single MP4 → auto-create Episode."""
    ep = Episode(id=uuid4(), task_description=task_description, fps=fps,
                 camera_names=[camera], status="completed")
    session.add(ep)

    if video and video.filename:
        ext = video.filename.rsplit(".", 1)[-1].lower() if "." in video.filename else "mp4"
        if ext not in settings.ALLOWED_VIDEO_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"Format not allowed: .{ext}")
        dest_dir = settings.storage_root / "videos"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{ep.id}_{camera}.{ext}"
        with open(dest, "wb") as f:
            while chunk := await video.read(1024 * 1024):
                f.write(chunk)
        from app.storage import sync_file_to_remote_async
        await sync_file_to_remote_async(
            dest,
            dest.relative_to(settings.storage_root),
        )
        ep.meta = {
            "video_path": str(dest.relative_to(settings.storage_root)).replace("\\", "/"),
            "storage_backend": settings.STORAGE_BACKEND,
        }
        try:
            import cv2
            cap = cv2.VideoCapture(str(dest))
            ep.frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
            if (detected := cap.get(cv2.CAP_PROP_FPS)) > 0:
                ep.fps = int(detected)
            cap.release()
        except Exception:
            ep.frame_count = 1

    await session.commit()
    return JSONResponse(status_code=201, content={
        "episode_id": str(ep.id), "status": "completed", "camera": camera,
        "frame_count": ep.frame_count, "fps": ep.fps,
        "stream_url": f"/api/v1/video/{ep.id}/{camera}/preview-stream",
    })


@router.get("/{episode_id}/{camera}/stream")
async def stream_video(episode_id: str, camera: str,
                       range_header: str | None = Header(None, alias="Range")):
    ep = get_episode(episode_id)
    if ep is None:
        raise HTTPException(status_code=404, detail="Episode not found")

    file_path = _find_video_file(episode_id, camera, ep)
    file_size = file_path.stat().st_size

    def _stream_full():
        with open(file_path, "rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                yield chunk

    if not range_header:
        return StreamingResponse(_stream_full(), media_type="video/mp4",
                                 headers={"Content-Length": str(file_size), "Accept-Ranges": "bytes"})

    start, end = _parse_range(range_header, file_size)
    content_length = end - start + 1

    def _stream_range():
        with open(file_path, "rb") as f:
            f.seek(start)
            remaining = content_length
            while remaining > 0:
                data = f.read(min(CHUNK_SIZE, remaining))
                if not data: break
                yield data
                remaining -= len(data)

    return StreamingResponse(_stream_range(), status_code=206, media_type="video/mp4",
                             headers={"Content-Range": f"bytes {start}-{end}/{file_size}",
                                      "Content-Length": str(content_length), "Accept-Ranges": "bytes"})


@router.get("/{episode_id}/{camera}/preview-stream")
async def stream_browser_preview(
    episode_id: str,
    camera: str,
    range_header: str | None = Header(None, alias="Range"),
):
    """Serve a browser-compatible H.264 preview without changing raw data."""
    ep = get_episode(episode_id)
    if ep is None:
        raise HTTPException(status_code=404, detail="Episode not found")
    source = _find_video_file(episode_id, camera, ep)
    preview = await _h264_preview_path(source)
    return _video_response(preview, range_header)


@router.get("/{episode_id}/depth-stream/{camera}")
async def stream_depth_video(
    episode_id: str,
    camera: str,
    range_header: str | None = Header(None, alias="Range"),
):
    """Stream the canonical HEVC gray12le depth video for one source."""
    ep = get_episode(episode_id)
    if ep is None:
        raise HTTPException(status_code=404, detail="Episode not found")
    file_path = find_depth_video(Path(ep["path"]), camera, str(ep.get("id") or ""))
    if file_path is None:
        raise HTTPException(status_code=404, detail="Depth video not found")
    file_size = file_path.stat().st_size

    def _stream_full():
        with open(file_path, "rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                yield chunk

    if not range_header:
        return StreamingResponse(_stream_full(), media_type="video/mp4",
                                 headers={"Content-Length": str(file_size), "Accept-Ranges": "bytes"})

    start, end = _parse_range(range_header, file_size)
    content_length = end - start + 1

    def _stream_range():
        with open(file_path, "rb") as f:
            f.seek(start)
            remaining = content_length
            while remaining > 0:
                data = f.read(min(CHUNK_SIZE, remaining))
                if not data:
                    break
                yield data
                remaining -= len(data)

    return StreamingResponse(_stream_range(), status_code=206, media_type="video/mp4",
                             headers={"Content-Range": f"bytes {start}-{end}/{file_size}",
                                      "Content-Length": str(content_length), "Accept-Ranges": "bytes"})


@router.get("/{episode_id}/depth-preview-stream/{camera}")
async def stream_browser_depth_preview(
    episode_id: str,
    camera: str,
    range_header: str | None = Header(None, alias="Range"),
):
    """Serve an 8-bit Turbo H.264 preview of the depth stream.

    Generated once per source into the local disposable preview cache
    (identical clamp/scale/colormap math as the browser renderer used to
    apply), so the review page plays it through a native <video> element.
    The canonical 12-bit stream remains the authoritative dataset asset.
    """
    ep = get_episode(episode_id)
    if ep is None:
        raise HTTPException(status_code=404, detail="Episode not found")
    source = find_depth_video(Path(ep["path"]), camera, str(ep.get("id") or ""))
    if source is None:
        raise HTTPException(status_code=404, detail="Depth video not found")
    # Prefer the q01/q99 bounds from the in-memory cache or the disk copy
    # persisted by preview generation; a neutral full-range mapping is used
    # when the stats have not been computed yet.
    stats = _depth_stats_for(source) or {}
    preview = await asyncio.to_thread(
        _depth_h264_preview_path, source,
        float(stats.get("q01") or 0.0), float(stats.get("q99") or 4095.0),
    )
    return _video_response(preview, range_header)


@router.get("/{episode_id}/{camera}/download")
async def download_video(episode_id: str, camera: str):
    ep = get_episode(episode_id)
    if ep is None:
        raise HTTPException(status_code=404, detail="Episode not found")
    file_path = _find_video_file(episode_id, camera, ep)
    file_size = file_path.stat().st_size
    def _stream():
        with open(file_path, "rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                yield chunk
    return StreamingResponse(_stream(), media_type="video/mp4",
                             headers={"Content-Disposition": f"attachment; filename={episode_id}_{camera}.mp4",
                                      "Content-Length": str(file_size)})
