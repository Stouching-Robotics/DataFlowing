#!/usr/bin/env python3
"""Sightac 触觉计算子进程。

父进程只负责相机读取、界面和结果汇总；本模块在独立进程内保留完整的
预处理、Hue、光流、力标定、自动标定、力映射及热力图计算。
"""

import builtins
import importlib
import os
import pickle
import queue
import sys
import time
import traceback

import cv2
import numpy as np


# Sightac 当前为 640x480 uint8 图像。描述符 FIFO 有界；处理线程变慢时
# 槽位耗尽则丢弃新帧，保留队列中较早的帧并限制共享内存占用。
TACTILE_FRAME_QUEUE_SIZE = 8
TACTILE_RESULT_QUEUE_SIZE = 4
# Continuous Sightac frames must not be pickled through a multiprocessing
# Queue. Four 640x480x3 streams otherwise create roughly 100 MB/s of parent
# process serialization/IPC traffic before the actual tactile algorithm runs.
TACTILE_FRAME_SLOT_COUNT = TACTILE_FRAME_QUEUE_SIZE
TACTILE_FRAME_SLOT_BYTES = 640 * 480 * 3
TACTILE_HEATMAP_SHAPE = (240, 320, 3)
TACTILE_FORCE_MATRIX_SHAPE = (250, 250, 3)
TACTILE_RESULT_HEATMAP_BYTES = int(np.prod(TACTILE_HEATMAP_SHAPE))
TACTILE_RESULT_FORCE_BYTES = int(np.prod(
    TACTILE_FORCE_MATRIX_SHAPE)) * np.dtype(np.float32).itemsize
TACTILE_RESULT_FLAG_OFFSET = (
    TACTILE_RESULT_HEATMAP_BYTES + TACTILE_RESULT_FORCE_BYTES)
TACTILE_RESULT_SLOT_BYTES = TACTILE_RESULT_FLAG_OFFSET + 8
TACTILE_RESULT_SLOT_COUNT = TACTILE_RESULT_QUEUE_SIZE + 4
# 输入进程化：采集侧一次标定的采样帧数（与旧主进程基线一致）。
TACTILE_INITIAL_BASELINE_FRAMES = 5
_SIGHTAC_API_PACKAGES = frozenset({
    "api",
    "api_legacy_2_4_5",
    "api_new",
    "api_v3_2_2_ksq",
})


def _quantize_force_channel(m):
    """与 recording.lerobot_v3.quantize_force_matrix 一致的 int16 行差分。"""
    q = np.clip(np.asarray(m, dtype=np.float32),
                -32767.0, 32767.0).astype(np.int16)
    d = np.empty_like(q)
    d[:, 0] = q[:, 0]
    d[:, 1:] = q[:, 1:] - q[:, :-1]
    return d.tobytes()


def create_shared_frame_pool(context, *, slot_count=TACTILE_FRAME_SLOT_COUNT,
                             slot_bytes=TACTILE_FRAME_SLOT_BYTES):
    """Create a bounded shared-memory frame pool for one Sightac side.

    The parent copies one decoded uint8 frame into a free slot and sends only
    a small descriptor through ``frame_queue``. The child returns the slot
    after processing, so a full pool has the same drop-new semantics as the
    old bounded raw-frame FIFO without serializing image payloads.
    """
    slot_count = int(slot_count)
    slot_bytes = int(slot_bytes)
    if slot_count <= 0 or slot_bytes <= 0:
        raise ValueError(
            f"invalid shared frame pool: slots={slot_count} bytes={slot_bytes}")
    storage = context.RawArray("B", slot_count * slot_bytes)
    free_slots = context.Queue(maxsize=slot_count)
    for slot in range(slot_count):
        free_slots.put_nowait(slot)
    return storage, free_slots, slot_bytes


def shared_frame_view(storage, slot, shape, nbytes, slot_bytes):
    """Return a uint8 ndarray view over one shared frame slot."""
    slot = int(slot)
    nbytes = int(nbytes)
    slot_bytes = int(slot_bytes)
    shape = tuple(int(value) for value in shape)
    if slot < 0 or nbytes <= 0 or nbytes > slot_bytes:
        raise ValueError(
            f"invalid shared frame descriptor: slot={slot} "
            f"nbytes={nbytes} slot_bytes={slot_bytes}")
    expected = int(np.prod(shape, dtype=np.int64))
    if expected != nbytes:
        raise ValueError(
            f"shared frame shape/size mismatch: shape={shape} "
            f"expected={expected} nbytes={nbytes}")
    return np.frombuffer(
        storage,
        dtype=np.uint8,
        count=nbytes,
        offset=slot * slot_bytes,
    ).reshape(shape)


def create_shared_result_pool(context, *,
                              slot_count=TACTILE_RESULT_SLOT_COUNT,
                              slot_bytes=TACTILE_RESULT_SLOT_BYTES):
    """Create a bounded shared-memory pool for tactile result payloads."""
    slot_count = int(slot_count)
    slot_bytes = int(slot_bytes)
    if slot_count <= 0 or slot_bytes <= 0:
        raise ValueError(
            f"invalid tactile result pool: slots={slot_count} "
            f"bytes={slot_bytes}")
    storage = context.RawArray("B", slot_count * slot_bytes)
    free_slots = context.Queue(maxsize=slot_count)
    for slot in range(slot_count):
        free_slots.put_nowait(slot)
    return storage, free_slots, slot_bytes


def shared_result_view(storage, slot, offset, length):
    """Return one contiguous uint8 result region in shared memory."""
    slot = int(slot)
    offset = int(offset)
    length = int(length)
    if slot < 0 or offset < 0 or length <= 0:
        raise ValueError(
            f"invalid tactile result view: slot={slot} offset={offset} "
            f"length={length}")
    return np.frombuffer(
        storage,
        dtype=np.uint8,
        count=length,
        offset=slot * TACTILE_RESULT_SLOT_BYTES + offset,
    )


def write_shared_result(storage, free_slots, slot_bytes, result):
    """Copy heatmap/force payload into shared memory and tag the result."""
    heatmap = result.get("heatmap")
    force_matrix = result.get("force_matrix")
    if heatmap is None:
        return None
    heatmap = np.ascontiguousarray(heatmap, dtype=np.uint8)
    if heatmap.shape != TACTILE_HEATMAP_SHAPE:
        raise ValueError(
            "unsupported tactile heatmap shape: "
            f"{heatmap.shape} expected={TACTILE_HEATMAP_SHAPE}")
    if force_matrix is not None:
        force_matrix = np.ascontiguousarray(force_matrix, dtype=np.float32)
        if force_matrix.shape != TACTILE_FORCE_MATRIX_SHAPE:
            raise ValueError(
                "unsupported tactile force matrix shape: "
                f"{force_matrix.shape} "
                f"expected={TACTILE_FORCE_MATRIX_SHAPE}")
    try:
        slot = int(free_slots.get_nowait())
    except queue.Empty:
        return None
    heat_dest = shared_result_view(
        storage, slot, 0, TACTILE_RESULT_HEATMAP_BYTES).reshape(
            TACTILE_HEATMAP_SHAPE)
    np.copyto(heat_dest, heatmap)
    if force_matrix is None:
        force_dest = shared_result_view(
            storage, slot, TACTILE_RESULT_HEATMAP_BYTES,
            TACTILE_RESULT_FORCE_BYTES).view(np.float32).reshape(
                TACTILE_FORCE_MATRIX_SHAPE)
        force_dest.fill(0)
        result["force_matrix"] = None
    else:
        force_dest = shared_result_view(
            storage, slot, TACTILE_RESULT_HEATMAP_BYTES,
            TACTILE_RESULT_FORCE_BYTES).view(np.float32).reshape(
                TACTILE_FORCE_MATRIX_SHAPE)
        np.copyto(force_dest, force_matrix)
        result["force_matrix"] = None
    flags = shared_result_view(
        storage, slot, TACTILE_RESULT_FLAG_OFFSET, 8)
    flags[0:8] = 0
    flags[0] = 1
    flags[1] = 0 if force_matrix is None else 1
    # Both payloads already live in the shared slot. Keeping heatmap here
    # accidentally pickles another 230,400 bytes through the result pipe.
    result["heatmap"] = None
    result["result_slot"] = slot
    return slot


def snapshot_sensor_state(sensor):
    """提取算法状态，排除只能留在父进程的相机/XU硬件句柄。"""
    state = {}
    for key, value in vars(sensor).items():
        if key in {"cap", "xu_camera"} or key.startswith("_ksq_"):
            continue
        state[key] = value
    state["__ksq_api_package__"] = getattr(
        sensor, "_ksq_api_package", "api")
    # 在启动子进程前失败，避免硬件运行后才发现某个状态不能跨进程传递。
    pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL)
    return state


def create_frame_queue(context, maxsize=TACTILE_FRAME_QUEUE_SIZE):
    """创建有界的单生产者/单消费者帧描述符 FIFO。"""
    maxsize = int(maxsize)
    if maxsize <= 0:
        raise ValueError(f"Sightac frame queue size must be positive, got {maxsize}")
    return context.Queue(maxsize=maxsize)


def clear_frame_queue(frame_queue, free_slots=None):
    """清空阶段切换前尚未处理的帧。标定请求以此作为明确的阶段边界。"""
    while True:
        try:
            item = frame_queue.get_nowait()
            if (
                free_slots is not None
                and isinstance(item, tuple)
                and len(item) >= 5
                and isinstance(item[1], (int, np.integer))
            ):
                try:
                    free_slots.put_nowait(int(item[1]))
                except (queue.Full, BrokenPipeError, EOFError, OSError):
                    pass
        except queue.Empty:
            return
        except (EOFError, OSError):
            return


def pressure_to_heatmap(pressure_matrix, device_type="bevel", scale=None):
    """0902 定标 JET 显示；不再按每传感器/每帧各自归一化。"""
    if pressure_matrix is None:
        return None
    if scale is None:
        scale = 200.0 if device_type == "curved" else 50.0
    fz = np.asarray(pressure_matrix, dtype=np.float32)
    scaled = np.clip(fz * float(scale), 0, 255).astype(np.uint8)
    heatmap_bgr = cv2.applyColorMap(scaled, cv2.COLORMAP_JET)
    heatmap_rgb = cv2.cvtColor(heatmap_bgr, cv2.COLOR_BGR2RGB)
    return cv2.resize(heatmap_rgb, (320, 240))


def _finite_nonnegative(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, value) if np.isfinite(value) else None


def process_tactile_frame(sensor, raw_frame, captured_at):
    """执行一帧完整触觉算法，返回可跨进程发布的结果。"""
    frame_started = time.perf_counter()
    queue_wait_ms = max(
        0.0, (time.monotonic() - float(captured_at)) * 1000.0)
    processed = sensor.process_raw_frame(raw_frame, mode=0)
    if not processed:
        return {
            "kind": (
                "recalibration_required"
                if getattr(sensor, "recalibration_required", False)
                else "skip"
            )
        }

    sensor_timings = getattr(sensor, "last_process_timings_ms", {}) or {}
    diagnostics = {
        "preprocess_ms": _finite_nonnegative(sensor_timings.get("preprocess")),
        "hue_ms": _finite_nonnegative(sensor_timings.get("hue")),
        "optical_flow_ms": _finite_nonnegative(
            sensor_timings.get("optical_flow")),
        "force_calibration_ms": _finite_nonnegative(
            sensor_timings.get("force_calibration")),
        "auto_calibration_ms": _finite_nonnegative(
            sensor_timings.get("auto_calibration")),
        "force_mapping_ms": _finite_nonnegative(
            sensor_timings.get("force_mapping")),
        "queue_wait_ms": queue_wait_ms,
    }
    force = (
        sensor.fx_total,
        sensor.fy_total,
        sensor.fz_total,
    )
    # Keep the three calibrated per-cell components from one source frame
    # together for native-rate recording.  Partial matrices are rejected.
    force_matrix = None
    fx_matrix = getattr(sensor, "fx_matrix", None)
    fy_matrix = getattr(sensor, "fy_matrix", None)
    fz_matrix = getattr(sensor, "fz_matrix", None)
    # 0902 model 1（响应曲线标定）无正向接触时返回 None；补一张零平面，
    # 不因此丢掉一帧本可用的录制样本。
    if (fz_matrix is None and getattr(sensor, "bevel_calibrate_model", None) == 1
            and getattr(sensor, "fz_total", None) == 0.0):
        hue = getattr(sensor, "hue_matrix_filtered", None)
        if hue is not None:
            fz_matrix = np.zeros_like(hue, dtype=np.float32)
    if (
        fx_matrix is not None
        and fy_matrix is not None
        and fz_matrix is not None
        and fx_matrix.shape == fy_matrix.shape == fz_matrix.shape
    ):
        force_matrix = np.stack(
            (fx_matrix, fy_matrix, fz_matrix), axis=-1
        ).astype(np.float32, copy=False)
    heatmap_started = time.perf_counter()
    heatmap = pressure_to_heatmap(
        fz_matrix, getattr(sensor, "device_type", "bevel"))
    heatmap_ms = (time.perf_counter() - heatmap_started) * 1000.0
    if heatmap is None:
        return {"kind": "skip"}

    child_total_ms = (time.perf_counter() - frame_started) * 1000.0
    classified_ms = heatmap_ms
    for key in (
            "preprocess_ms", "hue_ms", "optical_flow_ms",
            "force_calibration_ms", "auto_calibration_ms",
            "force_mapping_ms"):
        value = diagnostics.get(key)
        if value is not None:
            classified_ms += value
    diagnostics.update({
        "heatmap_ms": heatmap_ms,
        # IPC传输和父进程结果交接耗时在父进程收到结果后补入。
        "publish_ms": 0.0,
        "other_ms": max(0.0, child_total_ms - classified_ms),
    })
    return {
        "kind": "frame",
        "heatmap": heatmap,
        "force": force,
        "force_matrix": force_matrix,
        "diagnostics": diagnostics,
        "result_ready_at": time.monotonic(),
        "captured_at": float(captured_at),
    }


def _sightac_sdk_candidates(preferred=None):
    """Sightac SDK（api_new 等包）的候选导入根目录。"""
    candidates = []
    if preferred:
        candidates.append(str(preferred))
    explicit = os.environ.get("KSQ_SIGHTAC_ROOT")
    if explicit:
        candidates.append(str(explicit))
    try:
        from core.gripper import paths
        candidates.append(paths.SIGHTAC_SDK_ROOT)
    except Exception:
        pass
    module_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.append(
        os.path.join(module_dir, "sightac_sdk"))
    return candidates


def _ensure_sightac_sdk_path(preferred=None):
    """把可用的 Sightac SDK 根目录加入 sys.path，返回该目录。"""
    for candidate in _sightac_sdk_candidates(preferred):
        normalized = os.path.abspath(os.path.normpath(candidate))
        if os.path.isdir(normalized):
            if normalized not in sys.path:
                sys.path.insert(0, normalized)
            return normalized
    return None


def _pin_entire_process(cpu):
    """把当前进程已有及后续线程限制到一个逻辑 CPU。"""
    expected = {int(cpu)}
    tids = [
        int(name) for name in os.listdir("/proc/self/task") if name.isdigit()
    ]
    for tid in tids:
        os.sched_setaffinity(tid, expected)
    for tid in tids:
        actual = set(os.sched_getaffinity(tid))
        if actual != expected:
            raise RuntimeError(
                f"tactile process affinity failed: tid={tid} "
                f"expected={sorted(expected)} actual={sorted(actual)}"
            )
    return tuple(sorted(os.sched_getaffinity(0)))


def _restore_touch_sensor(sensor_state):
    # 子进程不打开或持有任何 UVC/XU 设备，只恢复 TouchSensor 的算法状态。
    sensor_state = dict(sensor_state)
    api_package = sensor_state.pop("__ksq_api_package__", "api")
    if api_package not in _SIGHTAC_API_PACKAGES:
        raise RuntimeError(
            f"unsupported Sightac worker API package: {api_package!r}")
    # 输入进程化后父进程可能不再构造 TouchSensor，sdk 路径由 input 子
    # 进程随 sensor_state 回传（__ksq_sightac_root__）；缺失时按本模块
    # 位置/KSQ_SIGHTAC_ROOT 回退，保证 spawn 计算子进程可 import api_*。
    _ensure_sightac_sdk_path(
        sensor_state.pop("__ksq_sightac_root__", None))
    TouchSensor = importlib.import_module(api_package).TouchSensor

    module = sys.modules.get(TouchSensor.__module__)
    if module is not None:
        def concise_sdk_print(*args, **kwargs):
            message = " ".join(str(arg) for arg in args).strip()
            lowered = message.lower()
            if any(token in lowered for token in (
                    "error", "failed", "exception", "cannot", "unable",
                    "timeout", "错误", "失败")):
                builtins.print(
                    f"[TactileSDK:{os.getpid()}] {message}", **kwargs)
        module.print = concise_sdk_print

    sensor = TouchSensor.__new__(TouchSensor)
    sensor.__dict__.update(sensor_state)
    sensor.cap = None
    sensor.xu_camera = None
    sensor._ksq_api_package = api_package
    return sensor


def _put_status(status_queue, message):
    try:
        status_queue.put_nowait(message)
    except (queue.Full, BrokenPipeError, EOFError, OSError):
        pass


def _run_calibration(sensor, frames):
    if getattr(sensor, "map_mask_pending", False):
        sensor.get_map_mask()
        sensor.map_mask_pending = False
    preprocessed_frames = []
    for raw_frame, _captured_at in frames:
        frame = sensor.preprocess_raw_frame(raw_frame)
        if frame is None:
            raise RuntimeError("invalid calibration frame")
        preprocessed_frames.append(frame.copy())
    sensor.calibrate_baseline(preprocessed_frames)
    return len(preprocessed_frames)


def run_tactile_process(
        side, cpu, sensor_state, frame_queue, command_queue, result_queue,
        status_queue, stop_event, shared_storage=None, free_slots=None,
        slot_bytes=TACTILE_FRAME_SLOT_BYTES,
        result_storage=None, result_free_slots=None,
        result_slot_bytes=TACTILE_RESULT_SLOT_BYTES,
        record_queue=None, record_gate=None):
    """独立 Left/Right 触觉计算子进程入口。"""
    # 父进程停止消费后，子进程退出时不能等待 Queue feeder 把结果刷入
    # 已无人读取的 pipe。
    try:
        result_queue.cancel_join_thread()
    except (AttributeError, OSError, ValueError):
        pass
    try:
        actual = _pin_entire_process(cpu)
        cv2.setNumThreads(1)
        cv2.setUseOptimized(True)
        sensor = _restore_touch_sensor(sensor_state)
        _put_status(status_queue, ("ready", os.getpid(), actual))
    except Exception as exc:
        _put_status(status_queue, (
            "fatal", str(exc), traceback.format_exc(limit=12)))
        return

    last_sequence = 0
    dropped_results = 0
    awaiting_calibration = False
    while not stop_event.is_set():
        command = None
        try:
            command = command_queue.get_nowait()
        except queue.Empty:
            pass
        except (EOFError, OSError):
            break

        if command is not None:
            kind = command[0]
            if kind == "stop":
                break
            if kind == "calibrate":
                _, job_id, reason, frames = command
                try:
                    samples = _run_calibration(sensor, frames)
                    _put_status(status_queue, (
                        "calibration_result", int(job_id), True, None,
                        str(reason), int(samples)))
                except Exception as exc:
                    _put_status(status_queue, (
                        "calibration_result", int(job_id), False, str(exc),
                        str(reason), 0))
                finally:
                    awaiting_calibration = False
                    frames = None
                continue

        if awaiting_calibration:
            stop_event.wait(0.01)
            continue

        frame_slot = None
        try:
            item = frame_queue.get(timeout=0.05)
            if shared_storage is not None and free_slots is not None:
                sequence, frame_slot, shape, nbytes, captured_at = item
                raw_frame = shared_frame_view(
                    shared_storage, frame_slot, shape, nbytes, slot_bytes)
            else:
                # Compatibility path for direct callers that still provide
                # raw ndarray frames instead of a shared pool.
                sequence, raw_frame, captured_at = item
        except queue.Empty:
            continue
        except (EOFError, OSError):
            break

        if int(sequence) <= int(last_sequence):
            _put_status(status_queue, (
                "error",
                f"non-increasing Sightac frame sequence={sequence} "
                f"last={last_sequence}",
                ""))
            if frame_slot is not None and free_slots is not None:
                try:
                    free_slots.put_nowait(int(frame_slot))
                except (queue.Full, BrokenPipeError, EOFError, OSError):
                    pass
            continue
        last_sequence = sequence
        try:
            result = process_tactile_frame(sensor, raw_frame, captured_at)
            kind = result.get("kind")
            if kind == "recalibration_required":
                awaiting_calibration = True
                _put_status(status_queue, (
                    "recalibration_request", "frame-shape-change"))
                continue
            if kind != "frame":
                continue
            result["sequence"] = int(sequence)
            record_payload = None
            if (
                record_queue is not None
                and record_gate is not None
                and record_gate.is_set()
            ):
                try:
                    force_matrix = result.get("force_matrix")
                    arr = np.asarray(force_matrix, dtype=np.float32)
                    if arr.ndim == 3 and arr.shape[2] == 3:
                        packed = tuple(
                            _quantize_force_channel(arr[:, :, i])
                            for i in range(3)
                        )
                        captured_at = float(
                            result.get("captured_at", 0.0) or 0.0)
                        mono_ns = (
                            int(captured_at * 1e9)
                            if captured_at > 0
                            else time.monotonic_ns()
                        )
                        record_payload = (
                            "tactile", str(side),
                            tuple(float(v) for v in result.get(
                                "force", (0.0, 0.0, 0.0))),
                            int(mono_ns), packed,
                        )
                except Exception:
                    record_payload = None
            result_slot = None
            if result_storage is not None and result_free_slots is not None:
                try:
                    result_slot = write_shared_result(
                        result_storage,
                        result_free_slots,
                        result_slot_bytes,
                        result,
                    )
                except Exception as exc:
                    _put_status(status_queue, (
                        "error", f"result shared-memory write failed: {exc}",
                        traceback.format_exc(limit=8)))
                    raise
                if result_slot is None:
                    dropped_results += 1
                    if (
                            dropped_results == 1 or
                            dropped_results % 100 == 0):
                        _put_status(status_queue, (
                            "result_dropped", int(dropped_results)))
                    continue
            try:
                result_queue.put_nowait(result)
            except queue.Full:
                # 结果队列已满时不等待；等待会把子进程处理结果变成越来越
                # 旧的预览数据。当前 result 已完成计算，按 drop-new 策略丢掉。
                dropped_results += 1
                if dropped_results == 1 or dropped_results % 100 == 0:
                    _put_status(status_queue, (
                        "result_dropped", int(dropped_results)))
            except (BrokenPipeError, EOFError, OSError):
                stop_event.set()
            if record_payload is not None and (
                record_gate is None or record_gate.is_set()
            ):
                try:
                    record_queue.put_nowait(record_payload)
                except (queue.Full, BrokenPipeError, EOFError, OSError):
                    pass
        except Exception as exc:
            _put_status(status_queue, (
                "error", str(exc), traceback.format_exc(limit=12)))
            stop_event.wait(0.05)
        finally:
            if frame_slot is not None and free_slots is not None:
                try:
                    free_slots.put_nowait(int(frame_slot))
                except (queue.Full, BrokenPipeError, EOFError, OSError):
                    pass


# ---------------------------------------------------------------------------
# Sightac 输入进程化（阶段1）：input 线程从主进程迁入 spawn 子进程。
#
# 新进程（run_tactile_input_process）负责：
#   * TouchSensor 构造 / claim（spawn 不能继承 socket / VideoCapture），
#   * 独占读取 Sightac 帧并写入共享帧池（计算子进程继续消费），
#   * 原 TactileProcessBridge 的 calibration job 状态与 submit_capture 逻辑。
# 主进程不再持有 VideoCapture/TouchSensor，也不再创建 input 线程；计算
# 子进程保持现有 run_tactile_process 不变。
# ---------------------------------------------------------------------------


class _InputCalibrationJob:
    """输入进程内一次标定采集任务（迁移自 TactileProcessBridge 的 job）。"""

    __slots__ = (
        "job_id", "reason", "warmup_remaining", "frame_count",
        "frames", "ready", "taken",
    )

    def __init__(self, job_id, reason, warmup_frames, frame_count):
        self.job_id = int(job_id)
        self.reason = str(reason)
        self.warmup_remaining = max(0, int(warmup_frames))
        self.frame_count = max(1, int(frame_count))
        self.frames = []
        self.ready = False
        self.taken = False


class TactileCaptureSubmitter:
    """采集侧标定状态机：原 TactileProcessBridge.submit_capture 迁入输入进程。

    只允许单生产者使用（输入进程主循环）。无标定任务时把解码后的 uint8
    帧拷入共享帧池并向计算子进程发布描述符；有标定任务时按 warmup /
    采样数收集原始帧，收集满后把 ("calibrate", ...) 直接发往计算子进程的
    command_queue。``finish_calibration`` 只在本进程收到父进程转发的
    calibration_done 后调用，语义与旧父进程内 job.done 一致。
    """

    def __init__(
        self,
        frame_queue,
        free_frame_slots,
        shared_frame_storage,
        frame_slot_bytes,
        command_queue,
    ):
        self.frame_queue = frame_queue
        self.free_frame_slots = free_frame_slots
        self.shared_frame_storage = shared_frame_storage
        self.frame_slot_bytes = int(frame_slot_bytes)
        self.command_queue = command_queue
        self.calibration_job = None
        self.next_sequence = 0
        self.published_count = 0
        self.dropped_new_frames = 0
        self.stopping = False

    def request_calibration(
        self,
        job_id: int,
        reason: str,
        warmup_frames: int = 0,
        frame_count: int = TACTILE_INITIAL_BASELINE_FRAMES,
    ):
        """创建一个指定 job_id 的采集任务；已有任务时返回现有任务。"""
        if self.stopping:
            return None
        current = self.calibration_job
        if current is not None:
            return current
        job = _InputCalibrationJob(
            job_id, reason, warmup_frames, frame_count)
        self.calibration_job = job
        clear_frame_queue(self.frame_queue, self.free_frame_slots)
        return job

    def finish_calibration(self, job_id: int) -> bool:
        """计算子进程完成一次标定后清除任务，恢复普通帧采集。"""
        if self.calibration_job is None:
            return False
        if int(job_id) != int(self.calibration_job.job_id):
            return False
        self.calibration_job = None
        clear_frame_queue(self.frame_queue, self.free_frame_slots)
        return True

    def submit_capture(self, frame, captured_at=None):
        """与 TactileProcessBridge.submit_capture 语义一致的单生产者版本。"""
        captured_at = (
            time.monotonic()
            if captured_at is None
            else float(captured_at)
        )
        job = self.calibration_job
        if job is not None:
            if job.ready or job.taken:
                return "calibration-wait"
            if job.warmup_remaining > 0:
                job.warmup_remaining -= 1
                return "calibration-warmup"
            job.frames.append((frame, captured_at))
            if len(job.frames) >= job.frame_count:
                job.ready = True
                job.taken = True
                frames = job.frames
                job.frames = []
                calibration_payload = (
                    "calibrate",
                    int(job.job_id),
                    job.reason,
                    frames,
                )
                try:
                    self.command_queue.put(calibration_payload)
                except (BrokenPipeError, EOFError, OSError) as exc:
                    # 计算子进程不可达：任务无法完成，恢复正常帧采集。
                    self.calibration_job = None
                    clear_frame_queue(
                        self.frame_queue, self.free_frame_slots)
                    raise RuntimeError(
                        f"calibration IPC failed: {exc}")
            return "calibration-frame"

        self.next_sequence += 1
        sequence = self.next_sequence
        slot = None
        try:
            slot = self.free_frame_slots.get_nowait()
        except queue.Empty:
            self.dropped_new_frames += 1
            return "dropped-new"
        try:
            array = np.asarray(frame)
            if (
                array.dtype != np.uint8
                or array.nbytes > self.frame_slot_bytes
            ):
                raise ValueError(
                    "unsupported Sightac frame for shared transport: "
                    f"dtype={array.dtype} shape={array.shape} "
                    f"nbytes={array.nbytes} max={self.frame_slot_bytes}"
                )
            destination = shared_frame_view(
                self.shared_frame_storage,
                slot,
                (array.nbytes,),
                array.nbytes,
                self.frame_slot_bytes,
            )
            destination[:] = np.ascontiguousarray(array).reshape(-1)
            self.frame_queue.put_nowait(
                (
                    sequence,
                    int(slot),
                    tuple(int(value) for value in array.shape),
                    int(array.nbytes),
                    captured_at,
                )
            )
        except queue.Full:
            try:
                self.free_frame_slots.put_nowait(slot)
            except (queue.Full, BrokenPipeError, EOFError, OSError):
                pass
            self.dropped_new_frames += 1
            return "dropped-new"
        except Exception:
            try:
                self.free_frame_slots.put_nowait(slot)
            except (queue.Full, BrokenPipeError, EOFError, OSError):
                pass
            raise
        self.published_count += 1
        return "frame"


def _import_ipc_camera():
    """从 checkout 内 camera_service/python 导入 libuvc IPC 客户端。"""
    try:
        from usb_camera_ipc import Camera as IpcCamera
        return IpcCamera
    except ImportError:
        pass
    try:
        from core.gripper.fays_runtime import PROJECT_ROOT as _runtime_root
    except Exception:
        _runtime_root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
    _ipc_root = os.path.join(_runtime_root, "camera_service", "python")
    if _ipc_root not in sys.path:
        sys.path.insert(0, _ipc_root)
    from usb_camera_ipc import Camera as IpcCamera
    return IpcCamera


class _ChildIpcReservedCamera:
    """输入子进程内重建的 libuvc IPC 保留相机（spawn 不可继承父句柄）。

    只保留 TactileDiscovery.init_sensor 需要的 transport / flash_params /
    claim()；claim 复刻 IpcReservedCamera.claim 的首帧重试语义。
    """

    transport = "libuvc-ipc"

    def __init__(self, spec):
        spec = dict(spec or {})
        self.socket_path = str(spec.get("socket_path") or "")
        self.flash_params = spec.get("flash_params")
        raw_format = tuple(spec.get("capture_format") or ())
        if len(raw_format) != 4:
            raw_format = (640, 480, 30.0, "MJPG")
        self.actual_format = (
            int(raw_format[0]), int(raw_format[1]),
            float(raw_format[2]), str(raw_format[3]),
        )

    def claim(self):
        if not self.socket_path:
            raise RuntimeError("Sightac IPC reservation has no socket path")
        camera = _import_ipc_camera()(self.socket_path, timeout=0.5)
        try:
            camera.open()
            first_frame = None
            failed_reads = 0
            for attempt in range(20):
                ok, frame = camera.read()
                if ok and frame is not None and getattr(
                    frame, "size", 0
                ) > 0:
                    first_frame = frame
                    break
                failed_reads += 1
                if attempt + 1 < 20:
                    time.sleep(0.05)
            if first_frame is None:
                raise RuntimeError(
                    f"Sightac libuvc IPC has no first frame: "
                    f"{self.socket_path} failed_reads={failed_reads}"
                )
            return camera, first_frame, self.actual_format
        except Exception:
            try:
                camera.release()
            except Exception:
                pass
            raise


def _reconstruct_discovery(payload):
    """在输入子进程内重建带完整 Flash 缓存的 TactileDiscovery。"""
    payload = dict(payload or {})
    from core.gripper.devices.tactile_discovery import TactileDiscovery

    kwargs = {}
    sysfs_root = str(payload.get("sysfs_root") or "/sys/class/video4linux")
    dev_root = str(payload.get("dev_root") or "/dev")
    kwargs["sysfs_root"] = sysfs_root
    kwargs["dev_root"] = dev_root
    if payload.get("platform") is not None:
        kwargs["platform"] = str(payload["platform"])
    if payload.get("sightac_root") is not None:
        kwargs["sightac_root"] = str(payload["sightac_root"])
    if payload.get("old_sensor_template_file") is not None:
        kwargs["old_sensor_template_file"] = str(
            payload["old_sensor_template_file"])
    if payload.get("full_logs") is not None:
        kwargs["full_logs"] = bool(payload["full_logs"])
    discovery = TactileDiscovery(**kwargs)
    if payload.get("api_package"):
        discovery.api_package = str(payload["api_package"])
    cache = payload.get("flash_full_params_cache") or {}
    discovery.flash_full_params_cache = {
        int(index): dict(params) for index, params in cache.items()
    }
    labels = payload.get("flash_param_cache") or {}
    discovery.flash_param_cache = {
        int(index): value for index, value in labels.items()
    }
    return discovery


def _build_input_sensor(config):
    """在输入子进程内构造 TouchSensor。

    默认重建带完整 Flash 缓存的 TactileDiscovery 并 init_sensor；
    测试/隔离调试可注入 "sensor_builder"（"module:attr" 工厂）。
    """
    builder = str(config.get("sensor_builder") or "").strip()
    if builder:
        module_name, _, attr = builder.partition(":")
        if not module_name or not attr:
            raise RuntimeError(
                f"invalid sensor_builder: {builder!r}")
        module = importlib.import_module(module_name)
        factory = getattr(module, attr)
        return factory(config)
    discovery = _reconstruct_discovery(config.get("discovery") or {})
    reserved_spec = config.get("reserved")
    reserved_camera = (
        None
        if not reserved_spec
        else _ChildIpcReservedCamera(reserved_spec)
    )
    video_index = int(config["video_index"])
    return discovery.init_sensor(
        video_index, reserved_camera=reserved_camera)


def run_tactile_input_process(
    side,
    cpu,
    config,
    frame_queue,
    free_frame_slots,
    shared_frame_storage,
    frame_slot_bytes,
    command_queue,
    input_command_queue,
    input_status_queue,
    stop_event,
    begin_event,
):
    """独立 Sightac 输入子进程入口（spawn）。

    config 字段：
      video_index   : int
      mode          : "open" | "connected"（仅日志）
      initial_job   : {"job_id": int, "warmup_frames": int} 或 None
      discovery     : 父进程 TactileDiscovery 可序列化状态
      reserved      : IPC 保留相机 spec 或 None（None 走 V4L2）

    上报消息（input_status_queue）：
      ("ready", pid, cpus, sensor_state)    传感器构造完成
      ("calibration_started", job_id, reason)
      ("calibration_busy", job_id, reason)
      ("frame_activity", monotonic_ns)
      ("input_stats", published, dropped_new, empty_reads)
      ("error"/"fatal", message, detail)
    """
    logger = config.get("_logger") if isinstance(config, dict) else None

    def log(text):
        if logger is not None:
            try:
                logger(str(text))
            except Exception:
                pass

    sensor = None
    skip_pin = bool(config.get("skip_pin")) \
        if isinstance(config, dict) else False
    try:
        actual = () if skip_pin else _pin_entire_process(int(cpu))
    except Exception as exc:
        _put_status(input_status_queue, (
            "fatal", str(exc), traceback.format_exc(limit=12)))
        return

    sensor_name = "sensor2" if side == "left" else "sensor3"
    try:
        sensor = _build_input_sensor(config)
        if sensor is None:
            raise RuntimeError("TouchSensor construction failed")
        video_index = int(config["video_index"])
        sensor_state = snapshot_sensor_state(sensor)
        sightac_root = _ensure_sightac_sdk_path(
            (config.get("discovery") or {}).get("sightac_root"))
        if sightac_root is not None:
            sensor_state["__ksq_sightac_root__"] = sightac_root
        log(
            f"[TactileInput:{sensor_name}] sensor ready "
            f"video{video_index} pid={os.getpid()} "
            f"sdk={sightac_root or '<missing>'}"
        )
    except Exception as exc:
        _put_status(input_status_queue, (
            "fatal", str(exc), traceback.format_exc(limit=12)))
        try:
            if sensor is not None:
                cap = getattr(sensor, "cap", None)
                if cap is not None and hasattr(cap, "release"):
                    cap.release()
        except Exception:
            pass
        return

    _put_status(input_status_queue, (
        "ready", os.getpid(), actual, sensor_state))

    submitter = TactileCaptureSubmitter(
        frame_queue,
        free_frame_slots,
        shared_frame_storage,
        int(frame_slot_bytes),
        command_queue,
    )
    initial_job = config.get("initial_job") or {}
    if initial_job.get("job_id") is not None:
        submitter.request_calibration(
            job_id=int(initial_job["job_id"]),
            reason=str(initial_job.get("reason") or "initial"),
            warmup_frames=int(initial_job.get("warmup_frames") or 0),
            frame_count=int(
                initial_job.get("frame_count")
                or TACTILE_INITIAL_BASELINE_FRAMES),
        )
        _put_status(input_status_queue, (
            "calibration_started",
            int(initial_job["job_id"]),
            str(initial_job.get("reason") or "initial"),
        ))
        log(
            f"[TactileInput:{sensor_name}] initial calibration queued: "
            f"job={int(initial_job['job_id'])} "
            f"warmup={int(initial_job.get('warmup_frames') or 0)}"
        )

    # 等父进程完成 Fays open 与计算子进程启动后再开始读帧。
    while not begin_event.wait(timeout=0.05):
        if stop_event.is_set():
            break

    empty_reads = 0
    last_stats_at = time.monotonic()
    try:
        while not stop_event.is_set():
            command = None
            try:
                command = input_command_queue.get_nowait()
            except queue.Empty:
                pass
            except (EOFError, OSError):
                break
            if command is not None:
                kind = command[0]
                if kind == "stop":
                    break
                if kind == "request_calibration":
                    (
                        _kind, job_id, reason, warmup_frames,
                        frame_count,
                    ) = command
                    existing = submitter.request_calibration(
                        job_id=int(job_id),
                        reason=str(reason),
                        warmup_frames=int(warmup_frames or 0),
                        frame_count=int(
                            frame_count or TACTILE_INITIAL_BASELINE_FRAMES),
                    )
                    if existing is None:
                        continue
                    if int(existing.job_id) == int(job_id):
                        _put_status(input_status_queue, (
                            "calibration_started",
                            int(existing.job_id),
                            str(existing.reason),
                        ))
                    else:
                        _put_status(input_status_queue, (
                            "calibration_busy",
                            int(existing.job_id),
                            str(existing.reason),
                        ))
                elif kind == "calibration_done":
                    (
                        _kind, job_id, success, error,
                    ) = command
                    if submitter.finish_calibration(int(job_id)):
                        log(
                            f"[TactileInput:{sensor_name}] calibration "
                            f"done: job={int(job_id)} "
                            f"success={bool(success)}")
                continue

            try:
                raw_frame = getattr(
                    sensor, "_ksq_prefetched_frame", None)
                if raw_frame is not None:
                    sensor._ksq_prefetched_frame = None
                else:
                    raw_frame = sensor.read_raw_frame()
                captured_at = time.monotonic()
            except Exception as exc:
                log(
                    f"[TactileInput:{sensor_name}] ERROR: {exc}")
                _put_status(input_status_queue, (
                    "error", str(exc),
                    traceback.format_exc(limit=8)))
                time.sleep(0.05)
                continue

            if raw_frame is None:
                empty_reads += 1
                capture = getattr(sensor, "cap", None)
                transport = getattr(capture, "transport", "")
                if transport == "libuvc-ipc":
                    try:
                        capture.reconnect()
                    except Exception as exc:
                        log(
                            f"[TactileInput:{sensor_name}] IPC "
                            f"reconnect failed: {exc}")
                if empty_reads in (1, 10, 30, 60):
                    log(
                        f"[TactileInput:{sensor_name}] no frame "
                        f"count={empty_reads} transport={transport!r} "
                        f"reconnects="
                        f"{getattr(capture, 'reconnect_count', 0)} "
                        f"error="
                        f"{getattr(capture, 'last_transport_error', '')!r}")
                if empty_reads >= 60:
                    error_text = (
                        f"{sensor_name} Sightac IPC has no frame for "
                        f"60 reads (reconnects="
                        f"{getattr(capture, 'reconnect_count', 0)}, "
                        f"transport_error="
                        f"{getattr(capture, 'last_transport_error', '') or 'unknown'})"
                    )
                    _put_status(input_status_queue, (
                        "error", error_text, "",
                    ))
                    break
                time.sleep(0.01)
                continue

            empty_reads = 0
            if stop_event.is_set():
                break
            _put_status(input_status_queue, (
                "frame_activity", int(time.monotonic_ns())))
            try:
                submitter.submit_capture(raw_frame, captured_at)
            except Exception as exc:
                log(
                    f"[TactileInput:{sensor_name}] submit ERROR: {exc}")
                _put_status(input_status_queue, (
                    "error", str(exc),
                    traceback.format_exc(limit=8)))
                time.sleep(0.05)

            now = time.monotonic()
            if now - last_stats_at >= 0.5:
                last_stats_at = now
                _put_status(input_status_queue, (
                    "input_stats",
                    int(submitter.published_count),
                    int(submitter.dropped_new_frames),
                    int(empty_reads),
                ))
    finally:
        try:
            if sensor is not None:
                cap = getattr(sensor, "cap", None)
                if cap is not None and hasattr(cap, "release"):
                    cap.release()
        except Exception:
            pass
        _put_status(input_status_queue, (
            "input_stats",
            int(submitter.published_count),
            int(submitter.dropped_new_frames),
            int(empty_reads),
        ))
        log(f"[TactileInput:{sensor_name}] input process exit")
