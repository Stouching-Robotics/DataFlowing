"""Sightac 句柄、输入线程和独立计算子进程的唯一生命周期所有者。

锁顺序固定为 ``TactileState.lock`` → 单路 ``TactileProcessBridge.condition``；
输入线程只在 ``capture_lock`` 内调用 ``read_raw_frame``，不会持有 state lock。
停止时先使 generation 失效并唤醒 IPC，再等待输入/协调/初始化线程，最后释放
VideoCapture，避免 release 与正常 read 并发。
"""

from __future__ import annotations

from dataclasses import dataclass
import multiprocessing
import os
import queue
import sys
import threading
import tempfile
import time
import traceback
from types import MappingProxyType
from typing import Callable, Mapping, Optional

import numpy as np

from core.gripper.runtime.interruptible_queue import InterruptibleQueueReader

from core.gripper.tactile_process_worker import (
    clear_frame_queue,
    create_frame_queue,
    create_shared_frame_pool,
    create_shared_result_pool,
    run_tactile_input_process,
    run_tactile_process,
    shared_frame_view,
    shared_result_view,
    snapshot_sensor_state,
    TACTILE_FORCE_MATRIX_SHAPE,
    TACTILE_HEATMAP_SHAPE,
    TACTILE_INITIAL_BASELINE_FRAMES,
    TACTILE_RESULT_FLAG_OFFSET,
    TACTILE_RESULT_FORCE_BYTES,
    TACTILE_RESULT_HEATMAP_BYTES,
    TACTILE_RESULT_SLOT_BYTES,
)


def _env_flag_default_on(key: str) -> bool:
    """读取“默认开启”的环境开关：未设置或非 0/false/no/off 视为开启。"""
    value = os.environ.get(key, "").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _env_flag_default_off(key: str) -> bool:
    """读取“默认关闭”的环境开关：仅 1/true/yes/on 视为开启。"""
    return os.environ.get(key, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


# 输入进程化开关：默认 1（input 采帧线程迁入独立 spawn 子进程，
# run_tactile_input_process），主进程不再持有 VideoCapture/TouchSensor。
# 需要旧父进程 input 线程路径时显式置 0。
KSQ_TACTILE_INPUT_PROCESS = _env_flag_default_on(
    "KSQ_TACTILE_INPUT_PROCESS")

# force_matrix 主进程回传开关：默认 0（主进程不再逐帧把完整矩阵从共享
# 内存拷回，只回 heatmap+fx/fy/fz 标量，减 ~90MB/s 拷贝；
# force_matrix parquet 仍由计算子进程直写 writer）。需要旧行为时显式置 1。
KSQ_TACTILE_FORCE_MATRIX_MATERIALIZE = _env_flag_default_off(
    "KSQ_TACTILE_FORCE_MATRIX_MATERIALIZE")


TACTILE_INIT_TIMEOUT = 10.0
TACTILE_PROCESS_READY_TIMEOUT = 10.0
TACTILE_CALIBRATION_TIMEOUT = 10.0
TACTILE_STOP_DEADLINE = 3.0
TACTILE_INPUT_UNBLOCK_TIMEOUT = 1.0
TACTILE_PROCESS_JOIN_TIMEOUT = 1.5
TACTILE_PROCESS_TERMINATE_TIMEOUT = 1.0
TACTILE_RESULT_QUEUE_SIZE = 4
INITIAL_BASELINE_FRAMES = TACTILE_INITIAL_BASELINE_FRAMES
OPEN_WARMUP_FRAMES = 5
CONNECTED_WARMUP_FRAMES = 30
HARDWARE_CALIBRATION_SOURCES = frozenset({
    "hardware", "hardware-live-flash"
})


class TactileCalibrationJob:
    """由 CPU4 输入线程采帧、独立子进程计算的一次标定任务。"""

    def __init__(
        self, job_id: int, reason: str,
        warmup_frames: int, frame_count: int,
    ):
        self.job_id = int(job_id)
        self.reason = str(reason)
        self.warmup_remaining = max(0, int(warmup_frames))
        self.frame_count = max(1, int(frame_count))
        self.frames = []
        self.ready = False
        self.taken = False
        self.done = threading.Event()
        self.success = False
        self.error = None


class _InputCalibrationWaiter:
    """输入进程化模式下一次手动标定的父进程完成记录。

    计算子进程上报 calibration_result 后由 coordinator 解析并 set。
    """

    def __init__(self, reason: str):
        self.reason = str(reason)
        self.done = threading.Event()
        self.success = False
        self.error = None


class TactileProcessBridge:
    """父进程与单路计算子进程之间的有界共享帧 FIFO。

    队列满时不让相机读取线程等待；当前正在处理的帧继续完成，新到的帧
    直接丢弃。图像本体位于共享槽位，FIFO 只传递描述符；这样处理速度
    低于采集速度时不会把实时预览拖成越来越旧，也不会反复序列化图像。
    """

    RESULT_QUEUE_SIZE = TACTILE_RESULT_QUEUE_SIZE

    def __init__(
        self,
        context,
        *,
        materialize_force_matrix: Optional[bool] = None,
    ):
        self.materialize_force_matrix = (
            KSQ_TACTILE_FORCE_MATRIX_MATERIALIZE
            if materialize_force_matrix is None
            else bool(materialize_force_matrix)
        )
        self.condition = threading.Condition()
        self.capture_lock = threading.Lock()
        self.frame_queue = create_frame_queue(context)
        (
            self.shared_frame_storage,
            self.free_frame_slots,
            self.frame_slot_bytes,
        ) = create_shared_frame_pool(context)
        (
            self.result_shared_storage,
            self.free_result_slots,
            self.result_slot_bytes,
        ) = create_shared_result_pool(context)
        self.command_queue = context.Queue()
        self.result_queue = context.Queue(
            maxsize=self.RESULT_QUEUE_SIZE)
        self._result_reader = None
        self.status_queue = context.Queue()
        # 输入进程化：input 子进程专用命令/状态通道与开始采集门。
        self.input_command_queue = context.Queue()
        self.input_status_queue = context.Queue()
        self.begin_event = context.Event()
        self.stop_event = context.Event()
        self.stopping = False
        self._next_calibration_id = 0
        self.calibration_job = None
        # 输入进程化：手动标定 waiter（未绑定 job 与已绑定 job_id）。
        self._input_waiters = []
        self._input_bound = {}
        self.input_calibrating = False
        self.next_sequence = 0
        self.published_count = 0
        self.dropped_new_frames = 0
        self.dropped_result_count = 0
        self.taken_count = 0
        self.last_processed_age_ms = None
        self._closed = False

    def get_result(self, timeout=0.05):
        # Created in the consuming parent thread, never passed to spawn children.
        if self._result_reader is None:
            self._result_reader = InterruptibleQueueReader(self.result_queue)
        return self._result_reader.get(
            timeout, cancelled=lambda: self.stopping)

    def submit_capture(self, frame, captured_at=None):
        captured_at = (
            time.monotonic()
            if captured_at is None
            else float(captured_at)
        )
        calibration_payload = None
        calibration_capture = False
        with self.condition:
            if self.stopping:
                return "stopped"
            job = self.calibration_job
            if job is not None:
                calibration_capture = True
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
                        job.job_id,
                        job.reason,
                        frames,
                    )
                outcome = "calibration-frame"
            else:
                outcome = "frame"

        if calibration_payload is not None:
            try:
                self.command_queue.put(calibration_payload)
            except (BrokenPipeError, EOFError, OSError) as exc:
                self.finish_calibration(
                    calibration_payload[1],
                    False,
                    f"calibration IPC failed: {exc}",
                )
            return outcome
        if calibration_capture:
            return outcome

        with self.condition:
            if self.stopping:
                return "stopped"
            self.next_sequence += 1
            sequence = self.next_sequence
        # 不等待、不覆盖旧帧：FIFO 满时丢弃刚到的新帧，避免采集线程被
        # 处理端反压，进而让预览和后续数据越来越滞后。
        slot = None
        try:
            slot = self.free_frame_slots.get_nowait()
        except queue.Empty:
            with self.condition:
                self.dropped_new_frames += 1
            return "dropped-new"

        try:
            array = np.asarray(frame)
            if array.dtype != np.uint8 or array.nbytes > self.frame_slot_bytes:
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
            with self.condition:
                self.dropped_new_frames += 1
            return "dropped-new"
        except Exception:
            try:
                self.free_frame_slots.put_nowait(slot)
            except (queue.Full, BrokenPipeError, EOFError, OSError):
                pass
            raise
        with self.condition:
            self.published_count += 1
        return outcome

    def request_calibration(
        self,
        reason: str,
        warmup_frames: int = 0,
        frame_count: int = INITIAL_BASELINE_FRAMES,
    ):
        with self.condition:
            if self.stopping:
                return None
            current = self.calibration_job
            if current is not None and not current.done.is_set():
                return current
            self._next_calibration_id += 1
            job = TactileCalibrationJob(
                self._next_calibration_id,
                reason,
                warmup_frames,
                frame_count,
            )
            self.calibration_job = job
        clear_frame_queue(self.frame_queue, self.free_frame_slots)
        return job

    def finish_calibration(
        self, job_id: int, success: bool, error=None,
    ) -> bool:
        with self.condition:
            job = self.calibration_job
            if job is None or job.job_id != int(job_id):
                return False
            self.calibration_job = None
            job.success = bool(success)
            job.error = None if error is None else str(error)
            job.done.set()
            self.condition.notify_all()
        clear_frame_queue(self.frame_queue, self.free_frame_slots)
        return True

    def _send_input_command(self, command) -> None:
        try:
            self.input_command_queue.put(command)
        except (queue.Full, BrokenPipeError, EOFError, OSError):
            pass

    def next_job_id(self) -> int:
        """分配一个跨父进程/input 子进程一致的标定 job id。"""
        with self.condition:
            self._next_calibration_id += 1
            return self._next_calibration_id

    def request_input_calibration(
        self,
        reason: str,
        warmup_frames: int = 0,
        frame_count: int = INITIAL_BASELINE_FRAMES,
        *,
        wait: bool = True,
    ):
        """输入进程化模式：请 input 子进程开始一次标定采集。

        wait=True 时返回父进程侧 waiter（由 coordinator 在计算子进程
        上报 calibration_result 后 set）；wait=False 用于自动重标定。
        """
        with self.condition:
            if self.stopping:
                return None
            self._next_calibration_id += 1
            job_id = self._next_calibration_id
            waiter = None
            if wait:
                waiter = _InputCalibrationWaiter(str(reason))
                self._input_waiters.append(waiter)
            self.input_calibrating = True
        self._send_input_command((
            "request_calibration",
            int(job_id),
            str(reason),
            int(warmup_frames),
            int(frame_count),
        ))
        return waiter

    def bind_input_waiter(self, job_id: int, reason: str):
        """input 子进程开始某 job 后，把最早的 waiter 绑定到该 job。"""
        with self.condition:
            if not self._input_waiters:
                return None
            waiter = self._input_waiters.pop(0)
            self._input_bound[int(job_id)] = waiter
            return waiter

    def fail_first_input_waiter(self, message) -> bool:
        """input 子进程报告已有标定在进行时快速失败首个 waiter。"""
        with self.condition:
            if not self._input_waiters:
                return False
            waiter = self._input_waiters.pop(0)
            waiter.success = False
            waiter.error = str(message)
            waiter.done.set()
            return True

    def mark_input_calibrating(self) -> None:
        with self.condition:
            self.input_calibrating = True

    def clear_input_calibrating(self) -> None:
        with self.condition:
            self.input_calibrating = False

    def resolve_input_calibration(
        self, job_id: int, success: bool, error=None,
    ) -> None:
        """计算子进程完成 job_id 标定：解析 waiter 并通知 input 子进程。"""
        with self.condition:
            self.input_calibrating = False
            waiter = self._input_bound.pop(int(job_id), None)
            if waiter is not None:
                waiter.success = bool(success)
                waiter.error = (
                    None if error is None else str(error))
                waiter.done.set()
        self._send_input_command((
            "calibration_done",
            int(job_id),
            bool(success),
            None if error is None else str(error),
        ))

    def record_input_stats(
        self, published: int, dropped_new: int,
    ) -> None:
        with self.condition:
            self.published_count = max(
                self.published_count, int(published))
            self.dropped_new_frames = max(
                self.dropped_new_frames, int(dropped_new))

    def request_stop(self) -> None:
        with self.condition:
            if self.stopping:
                return
            self.stopping = True
            job = self.calibration_job
            self.calibration_job = None
            if job is not None and not job.done.is_set():
                job.success = False
                job.error = "sensor pipeline stopped"
                job.done.set()
            for waiter in self._input_waiters:
                waiter.success = False
                waiter.error = "sensor pipeline stopped"
                waiter.done.set()
            self._input_waiters = []
            for waiter in self._input_bound.values():
                waiter.success = False
                waiter.error = "sensor pipeline stopped"
                waiter.done.set()
            self._input_bound = {}
            self.condition.notify_all()
        self.stop_event.set()
        try:
            self.command_queue.put_nowait(("stop",))
        except (queue.Full, BrokenPipeError, EOFError, OSError):
            pass
        try:
            self.input_command_queue.put_nowait(("stop",))
        except (queue.Full, BrokenPipeError, EOFError, OSError):
            pass

    def record_processed(self, captured_at) -> None:
        if captured_at is None:
            return
        age_ms = max(
            0.0,
            (time.monotonic() - float(captured_at)) * 1000.0,
        )
        with self.condition:
            self.taken_count += 1
            self.last_processed_age_ms = age_ms

    def materialize_result(self, result):
        """Copy large result arrays out of shared memory and free the slot."""
        slot = result.pop("result_slot", None)
        if slot is None:
            return
        heat_view = shared_result_view(
            self.result_shared_storage,
            slot,
            0,
            TACTILE_RESULT_HEATMAP_BYTES,
        ).reshape(TACTILE_HEATMAP_SHAPE)
        result["heatmap"] = np.array(heat_view, dtype=np.uint8, copy=True)
        force_view = shared_result_view(
            self.result_shared_storage,
            slot,
            TACTILE_RESULT_HEATMAP_BYTES,
            TACTILE_RESULT_FORCE_BYTES,
        ).view(np.float32).reshape(TACTILE_FORCE_MATRIX_SHAPE)
        flags = shared_result_view(
            self.result_shared_storage,
            slot,
            TACTILE_RESULT_FLAG_OFFSET,
            8,
        )
        if flags[0]:
            pass
        else:
            result["heatmap"] = None
        if self.materialize_force_matrix and flags[1]:
            result["force_matrix"] = np.array(
                force_view, dtype=np.float32, copy=True)
        else:
            # 只回 heatmap+标量：完整矩阵由计算子进程直写 writer parquet，
            # 主进程不再保留（省 ~750KB/侧/帧 拷贝）。
            result["force_matrix"] = None
        try:
            self.free_result_slots.put_nowait(int(slot))
        except (queue.Full, BrokenPipeError, EOFError, OSError):
            pass

    def record_result_drops(self, count: int) -> None:
        with self.condition:
            self.dropped_result_count = max(
                self.dropped_result_count, int(count))

    def snapshot(self):
        with self.condition:
            return MappingProxyType({
                "published": self.published_count,
                "dropped_new_frames": self.dropped_new_frames,
                "dropped_results": self.dropped_result_count,
                "taken": self.taken_count,
                "age_ms": self.last_processed_age_ms,
                "calibrating": (
                    self.calibration_job is not None
                    or self.input_calibrating
                ),
                "stopping": self.stopping,
            })

    def close(self) -> None:
        """只允许在输入线程和计算子进程退出后关闭 IPC 管道。"""
        if self._closed:
            return
        self._closed = True
        for channel in (
            self.frame_queue,
            self.command_queue,
            self.result_queue,
            self.status_queue,
            self.input_command_queue,
            self.input_status_queue,
            self.free_frame_slots,
            self.free_result_slots,
        ):
            try:
                channel.close()
                channel.cancel_join_thread()
            except (AttributeError, OSError, ValueError):
                pass


@dataclass(frozen=True)
class TactileSideSnapshot:
    side: str
    video_index: Optional[int]
    sensor_present: bool
    initializing: bool
    input_alive: bool
    process_alive: bool
    process_pid: Optional[int]
    calibrating: bool
    force: Optional[tuple]
    heatmap: Optional[np.ndarray]
    error: Optional[str]


@dataclass(frozen=True)
class TactileSnapshot:
    generation: int
    running: bool
    mode: Optional[str]
    calibrating: bool
    left: TactileSideSnapshot
    right: TactileSideSnapshot
    flash_labels: Mapping[str, int]
    flash_full_params: Mapping[int, Mapping[str, object]]


class _SideState:
    def __init__(self, side: str):
        self.side = side
        self.video_index = None
        self.sensor = None
        self.initializing = False
        self.init_thread = None
        self.coordinator_thread = None
        self.input_thread = None
        self.input_process = None
        self.sensor_state = None
        self.process = None
        self.pipeline = None
        self.force = None
        self.heatmap = None
        self.error = None
        self.ready_event = threading.Event()

    def reset_for_start(self):
        self.video_index = None
        self.sensor = None
        self.initializing = False
        self.init_thread = None
        self.coordinator_thread = None
        self.input_thread = None
        self.input_process = None
        self.sensor_state = None
        self.process = None
        self.pipeline = None
        self.force = None
        self.heatmap = None
        self.error = None
        self.ready_event = threading.Event()


class TactileState:
    """Sightac 的唯一可变状态；外部只能读取不可变 snapshot。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.generation = 0
        self.running = False
        self.mode = None
        self.calibrating = False
        self.initializer_thread = None
        self.sides = {
            "left": _SideState("left"),
            "right": _SideState("right"),
        }
        self.flash_labels = {}
        self.flash_full_params = {}
        # 输入进程化标记：由 TactileProcessManager 构造时按开关写入。
        self.input_process_mode = False

    @staticmethod
    def _frozen_image(image):
        if image is None:
            return None
        copied = np.array(image, copy=True)
        copied.setflags(write=False)
        return copied

    @staticmethod
    def _process_alive(process) -> bool:
        if process is None:
            return False
        try:
            return bool(process.is_alive())
        except (AssertionError, ValueError):
            return False

    def _side_snapshot(self, side_state: _SideState):
        process = side_state.process
        pipeline = side_state.pipeline
        calibration = (
            pipeline is not None
            and bool(pipeline.snapshot()["calibrating"])
        )
        return TactileSideSnapshot(
            side=side_state.side,
            video_index=side_state.video_index,
            sensor_present=bool(
                side_state.sensor is not None
                or (
                    self.input_process_mode
                    and side_state.input_process is not None
                    and self._process_alive(side_state.input_process)
                )
            ),
            initializing=bool(side_state.initializing),
            input_alive=bool(
                (
                    side_state.input_thread is not None
                    and side_state.input_thread.is_alive()
                )
                or (
                    side_state.input_process is not None
                    and self._process_alive(side_state.input_process)
                )
            ),
            process_alive=self._process_alive(process),
            process_pid=(
                getattr(process, "pid", None)
                if process is not None
                else None
            ),
            calibrating=calibration,
            force=(
                None
                if side_state.force is None
                else tuple(side_state.force)
            ),
            heatmap=self._frozen_image(side_state.heatmap),
            error=(
                None
                if side_state.error is None
                else str(side_state.error)
            ),
        )

    def snapshot(self):
        with self.lock:
            flash_full = {}
            for index, params in self.flash_full_params.items():
                flash_full[int(index)] = MappingProxyType({
                    key: (
                        tuple(value)
                        if isinstance(value, (list, tuple))
                        else value
                    )
                    for key, value in params.items()
                })
            return TactileSnapshot(
                generation=int(self.generation),
                running=bool(self.running),
                mode=self.mode,
                calibrating=bool(self.calibrating),
                left=self._side_snapshot(self.sides["left"]),
                right=self._side_snapshot(self.sides["right"]),
                flash_labels=MappingProxyType(
                    dict(self.flash_labels)),
                flash_full_params=MappingProxyType(flash_full),
            )


class TactileProcessManager:
    """唯一创建/关闭 TouchSensor、cap、输入线程及 spawn 子进程。"""

    INIT_TIMEOUT = TACTILE_INIT_TIMEOUT
    PROCESS_READY_TIMEOUT = TACTILE_PROCESS_READY_TIMEOUT
    CALIBRATION_TIMEOUT = TACTILE_CALIBRATION_TIMEOUT
    STOP_DEADLINE = TACTILE_STOP_DEADLINE
    INPUT_UNBLOCK_TIMEOUT = TACTILE_INPUT_UNBLOCK_TIMEOUT
    PROCESS_JOIN_TIMEOUT = TACTILE_PROCESS_JOIN_TIMEOUT
    PROCESS_TERMINATE_TIMEOUT = TACTILE_PROCESS_TERMINATE_TIMEOUT
    OPEN_WARMUP_FRAMES = OPEN_WARMUP_FRAMES
    CONNECTED_WARMUP_FRAMES = CONNECTED_WARMUP_FRAMES

    def __init__(
        self,
        state: TactileState,
        discovery,
        *,
        cpu_map: Optional[Mapping[str, int]] = None,
        bind_thread: Optional[Callable[[str], object]] = None,
        bind_general_thread: Optional[Callable[[str], object]] = None,
        bind_coordinator: Optional[Callable[[str], object]] = None,
        unbind_thread: Optional[Callable[[str], object]] = None,
        publish_result: Optional[
            Callable[[str, object, Optional[tuple], object], None]
        ] = None,
        record_event: Optional[Callable[[str], None]] = None,
        record_stage: Optional[
            Callable[[str, Mapping[str, float]], None]
        ] = None,
        on_state: Optional[Callable[[TactileSnapshot], None]] = None,
        context=None,
        process_target=run_tactile_process,
        set_process_affinity: Optional[
            Callable[[int, set], None]
        ] = None,
        get_process_affinity: Optional[
            Callable[[int], set]
        ] = None,
        on_process_stop: Optional[Callable[[str, int], None]] = None,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        logger: Callable[..., None] = print,
        record_queue=None,
        record_gate=None,
        input_process_mode: Optional[bool] = None,
        materialize_force_matrix: Optional[bool] = None,
    ):
        self.state = state
        self.discovery = discovery
        requested_cpu_map = dict(cpu_map or {"left": 0, "right": 2})
        self.cpu_map = {
            "left": int(requested_cpu_map.get("left", -1)),
            "right": int(requested_cpu_map.get("right", -1)),
        }
        if min(self.cpu_map.values()) < 0:
            raise ValueError(f"invalid Sightac cpu_map: {self.cpu_map!r}")
        self.bind_thread = bind_thread
        self.bind_general_thread = bind_general_thread
        self.bind_coordinator = bind_coordinator
        self.unbind_thread = unbind_thread
        self.publish_result = publish_result
        self.record_event = record_event
        self.record_stage = record_stage
        self.on_state = on_state
        # 自诊断：按侧统计「力标量有效但力矩阵为空」的连续帧数。
        # GUI 热力图只依赖 fz 矩阵、力曲线只依赖三轴总力标量，都
        # 证明不了 250×250×3 完整矩阵是否产出。
        self._matrix_diag = {}
        # 显式使用 spawn，禁止 fork 继承 SDK/VideoCapture 句柄。
        self.context = (
            context
            if context is not None
            else multiprocessing.get_context("spawn")
        )
        self.process_target = process_target
        self.set_process_affinity = (
            set_process_affinity
            if set_process_affinity is not None
            else getattr(os, "sched_setaffinity", None)
        )
        self.get_process_affinity = (
            get_process_affinity
            if get_process_affinity is not None
            else getattr(os, "sched_getaffinity", None)
        )
        self.on_process_stop = on_process_stop
        self.thread_factory = thread_factory
        self.clock = clock
        self.sleep = sleep
        # 采集进程化（B）：tactile 计算子进程在录制期间直接把量化后的力
        # 矩阵写进该队列，主进程不再量化/put，避免 writer 反压拖 GUI。
        self._record_queue = record_queue
        self._record_gate = record_gate
        self.logger = logger
        self._spawn_lock = threading.Lock()
        # 输入进程化开关：KSQ_TACTILE_INPUT_PROCESS 默认开启（置 0 关闭）。
        # TouchSensor/claim/读帧/标定采集迁入独立 spawn 输入子进程，
        # 父进程不再创建 input 线程。
        self.input_process_mode = (
            KSQ_TACTILE_INPUT_PROCESS
            if input_process_mode is None
            else bool(input_process_mode)
        )
        self.state.input_process_mode = self.input_process_mode
        self.materialize_force_matrix = (
            KSQ_TACTILE_FORCE_MATRIX_MATERIALIZE
            if materialize_force_matrix is None
            else bool(materialize_force_matrix)
        )
        # 输入子进程自绑核：默认与对应计算子进程共用同一逻辑核（旧
        # 方案中 Sightac 输入线程也与计算进程同域，输入主要阻塞在 UVC
        # 读取上）。可用 KSQ_TACTILE_INPUT_CPUS_LEFT/RIGHT 覆盖。
        def _input_cpus(env_key: str, fallback: int):
            value = os.environ.get(env_key, "").strip()
            if not value:
                return (int(fallback),)
            return tuple(
                int(cpu) for cpu in value.split(",") if cpu.strip())
        self._input_cpus_explicit = {
            "left": bool(
                os.environ.get("KSQ_TACTILE_INPUT_CPUS_LEFT", "")
                .strip()),
            "right": bool(
                os.environ.get("KSQ_TACTILE_INPUT_CPUS_RIGHT", "")
                .strip()),
        }
        self.input_process_cpus = {
            "left": _input_cpus(
                "KSQ_TACTILE_INPUT_CPUS_LEFT", self.cpu_map["left"]),
            "right": _input_cpus(
                "KSQ_TACTILE_INPUT_CPUS_RIGHT", self.cpu_map["right"]),
        }
        if self.input_process_mode:
            self.logger(
                "[Tactile] KSQ_TACTILE_INPUT_PROCESS=1: "
                "input capture runs in spawn child processes "
                f"(cpus={self.input_process_cpus})"
            )

    def snapshot(self) -> TactileSnapshot:
        return self.state.snapshot()

    def set_performance_cpu_map(
        self,
        *,
        left: int,
        right: int,
    ) -> None:
        """Apply the current runtime CPU mapping for both Sightac workers.

        The lifecycle handoff stops the Open-generation workers before the
        next Open/Connect generation snapshots this mapping and creates its
        child processes.
        """
        requested = {"left": int(left), "right": int(right)}
        if min(requested.values()) < 0:
            raise ValueError(f"invalid Sightac cpu_map: {requested!r}")
        with self.state.lock:
            self.cpu_map = requested
        # 输入进程默认跟随计算子进程绑核；显式 KSQ_TACTILE_INPUT_CPUS_*
        # 覆盖时保持不变。
        if not self._input_cpus_explicit["left"]:
            self.input_process_cpus["left"] = (int(left),)
        if not self._input_cpus_explicit["right"]:
            self.input_process_cpus["right"] = (int(right),)

    def set_input_process_cpus(
        self,
        *,
        left: Optional[tuple] = None,
        right: Optional[tuple] = None,
    ) -> None:
        """输入子进程化时按当前 slot policy 覆盖 input 绑核。"""
        if left is not None:
            normalized = tuple(int(cpu) for cpu in left)
            if not normalized or min(normalized) < 0:
                raise ValueError(
                    f"invalid Sightac input cpus: {normalized!r}")
            self.input_process_cpus["left"] = normalized
        if right is not None:
            normalized = tuple(int(cpu) for cpu in right)
            if not normalized or min(normalized) < 0:
                raise ValueError(
                    f"invalid Sightac input cpus: {normalized!r}")
            self.input_process_cpus["right"] = normalized

    def _notify(self) -> None:
        if self.on_state is not None:
            try:
                self.on_state(self.snapshot())
            except Exception:
                pass

    def _active(self, generation: int) -> bool:
        with self.state.lock:
            return (
                self.state.running
                and self.state.generation == int(generation)
            )

    @staticmethod
    def _release_sensor(sensor) -> None:
        if sensor is None:
            return
        try:
            cap = getattr(sensor, "cap", None)
            if cap is not None and hasattr(cap, "release"):
                cap.release()
        except Exception:
            traceback.print_exc()

    def _set_error(self, side: str, message) -> None:
        with self.state.lock:
            self.state.sides[side].error = str(message)
        self.logger(f"[Tactile:{side}] ERROR: {message}")
        self._notify()

    def start_open(
        self,
        left="left",
        right="right",
        *,
        asynchronous: bool = True,
    ) -> int:
        return self.start(
            {"left": left, "right": right},
            mode="open",
            warmup_frames=self.OPEN_WARMUP_FRAMES,
            asynchronous=asynchronous,
        )

    def start_connected(
        self,
        left="left",
        right="right",
        *,
        asynchronous: bool = True,
    ) -> int:
        return self.start(
            {"left": left, "right": right},
            mode="connected",
            warmup_frames=self.CONNECTED_WARMUP_FRAMES,
            asynchronous=asynchronous,
        )

    def start(
        self,
        inputs: Mapping[str, object],
        *,
        mode: str,
        warmup_frames: int,
        asynchronous: bool = True,
        on_initialized: Optional[Callable[[int], None]] = None,
    ) -> int:
        """启动两路独立初始化；构造完成后可通知 Fays 再开流。"""
        if mode not in {"open", "connected"}:
            raise ValueError(f"unsupported tactile start mode: {mode}")
        self.stop()
        with self.state.lock:
            self.state.generation += 1
            generation = self.state.generation
            self.state.running = True
            self.state.mode = mode
            self.state.calibrating = False
            for side_state in self.state.sides.values():
                side_state.reset_for_start()
            initializer = self.thread_factory(
                target=self._initialize_all,
                args=(
                    generation,
                    {
                        "left": inputs.get("left", "left"),
                        "right": inputs.get("right", "right"),
                    },
                    int(warmup_frames),
                    on_initialized,
                ),
                daemon=True,
                name=f"tactile-init-{generation}",
            )
            self.state.initializer_thread = initializer
        initializer.start()
        if not asynchronous:
            initializer.join()
            self.wait_ready(timeout=self.PROCESS_READY_TIMEOUT)
        self._notify()
        return generation

    def _resolve_inputs(
        self, generation: int, inputs: Mapping[str, object],
    ):
        reserved = {
            side: inputs.get(side)
            for side in ("left", "right")
        }
        if all(
            getattr(getattr(camera, "node", None), "role", None) == side
            for side, camera in reserved.items()
        ):
            left_node = reserved["left"].node
            right_node = reserved["right"].node
            if (
                left_node.root_hub != right_node.root_hub
                or left_node.direct_parent_hub
                != right_node.direct_parent_hub
            ):
                raise RuntimeError(
                    "reserved Sightac left/right are not below the "
                    "same direct Hub"
                )
            resolved = {
                side: int(camera.node.video_index)
                for side, camera in reserved.items()
            }
            labels, full = self.discovery.cache_snapshot()
            with self.state.lock:
                if self.state.generation == generation:
                    self.state.flash_labels = dict(labels)
                    self.state.flash_full_params = {
                        int(index): {
                            key: (
                                list(value)
                                if isinstance(value, tuple)
                                else value
                            )
                            for key, value in params.items()
                        }
                        for index, params in full.items()
                    }
            self.logger(
                "[Tactile] Hub-pinned pair: "
                f"root={left_node.root_hub} "
                f"hub={left_node.direct_parent_hub} "
                f"left=video{resolved['left']} "
                f"right=video{resolved['right']}"
            )
            return resolved

        raise RuntimeError(
            "Sightac 必须使用本次动态 libusb/libuvc 相机套件；"
            "禁止回退到 V4L2 video 节点、旧缓存或旧物理端口约定"
        )

    # ------------------------------------------------------------------
    # 输入进程化（阶段1）：input 采帧迁入 spawn 子进程的路径。
    # ------------------------------------------------------------------
    def _ensure_sightac_syspath(self) -> None:
        """把 Sightac SDK 根目录加入父进程 sys.path。

        旧路径在父进程 init_sensor 时已由 discovery 加入；输入进程化时
        父进程不再构造 TouchSensor，需显式恢复，spawn 计算子进程才能
        import api_new 等 SDK 包。
        """
        ensure = getattr(
            self.discovery, "_ensure_sightac_import_path", None)
        if callable(ensure):
            try:
                ensure()
            except Exception as exc:
                self.logger(
                    f"[Tactile] sightac import path: {exc}")

    def _discovery_payload(self) -> dict:
        """把父进程 TactileDiscovery 的可序列化状态交给 input 子进程。"""
        discovery = self.discovery
        payload = {}
        for key in (
            "sysfs_root", "dev_root", "platform", "sightac_root",
            "api_package", "old_sensor_template_file", "full_logs",
        ):
            value = getattr(discovery, key, None)
            if value is not None:
                payload[key] = value
        cache = getattr(discovery, "flash_full_params_cache", None)
        if cache is not None:
            try:
                payload["flash_full_params_cache"] = {
                    int(index): dict(params)
                    for index, params in cache.items()
                }
            except Exception:
                pass
        labels = getattr(discovery, "flash_param_cache", None)
        if labels is not None:
            try:
                payload["flash_param_cache"] = {
                    int(index): value for index, value in labels.items()
                }
            except Exception:
                pass
        return payload

    def _reserved_input_spec(
        self, side: str, inputs: Mapping[str, object],
    ):
        """把 Connect 的 libuvc IPC 保留相机转成可 spawn 的 spec。"""
        reserved_camera = inputs.get(side)
        node = getattr(reserved_camera, "node", None)
        if getattr(node, "role", None) != side:
            return None
        if (
            getattr(reserved_camera, "transport", None)
            != "libuvc-ipc"
        ):
            return None
        return {
            "transport": "libuvc-ipc",
            "socket_path": str(node.device_path),
            "capture_format": tuple(
                getattr(
                    node, "capture_format",
                    (640, 480, 30.0, "MJPG"),
                ) or (640, 480, 30.0, "MJPG")
            ),
            "flash_params": getattr(
                reserved_camera, "flash_params", None),
        }

    def _spawn_input_process(
        self,
        generation: int,
        side: str,
        video_index: int,
        pipeline: TactileProcessBridge,
        inputs: Mapping[str, object],
        warmup_frames: int,
        initial_job_id: int,
    ) -> None:
        config = {
            "video_index": int(video_index),
            "mode": str(self.state.mode or "open"),
            "initial_job": {
                "job_id": int(initial_job_id),
                "reason": "initial",
                "warmup_frames": int(warmup_frames),
                "frame_count": INITIAL_BASELINE_FRAMES,
            },
            "discovery": self._discovery_payload(),
            "reserved": self._reserved_input_spec(side, inputs),
        }
        process = self.context.Process(
            target=run_tactile_input_process,
            args=(
                side,
                int(self.input_process_cpus[side][0]),
                config,
                pipeline.frame_queue,
                pipeline.free_frame_slots,
                pipeline.shared_frame_storage,
                pipeline.frame_slot_bytes,
                pipeline.command_queue,
                pipeline.input_command_queue,
                pipeline.input_status_queue,
                pipeline.stop_event,
                pipeline.begin_event,
            ),
            daemon=True,
            name=f"tactile-{side}-input-process-{generation}",
        )
        with self._spawn_lock:
            process.start()
        with self.state.lock:
            self.state.sides[side].input_process = process

    def _wait_input_ready(
        self,
        generation: int,
        side: str,
        pipeline: TactileProcessBridge,
    ) -> None:
        sensor_name = "sensor2" if side == "left" else "sensor3"
        try:
            status = pipeline.input_status_queue.get(
                timeout=self.INIT_TIMEOUT)
        except queue.Empty as exc:
            raise RuntimeError(
                f"{sensor_name} input process did not become ready"
            ) from exc
        kind = status[0]
        if kind == "fatal":
            message = status[1]
            detail = status[2] if len(status) > 2 else ""
            raise RuntimeError(
                f"{sensor_name} input process startup failed: "
                f"{message}\n{detail}")
        if kind != "ready":
            raise RuntimeError(
                f"{sensor_name} unexpected input startup message: "
                f"{kind}")
        _, child_pid, child_cpus, sensor_state = status
        with self.state.lock:
            side_state = self.state.sides[side]
            process = side_state.input_process
            side_state.initializing = False
            side_state.sensor_state = sensor_state
        expected = (int(self.input_process_cpus[side][0]),)
        if (
            process is None
            or int(child_pid) != process.pid
            or tuple(child_cpus) != expected
        ):
            raise RuntimeError(
                f"{sensor_name} input child verification failed: "
                f"pid={child_pid} cpus={tuple(child_cpus)} "
                f"expected={expected}")
        self.logger(
            f"[Tactile] input process bound: side={side} "
            f"pid={process.pid} logical-cpu={expected[0]}")

    def _initialize_all_input(
        self,
        generation: int,
        inputs: Mapping[str, object],
        warmup_frames: int,
        on_initialized: Optional[Callable[[int], None]],
    ) -> None:
        """输入进程化：在 spawn 子进程内构造/claim TouchSensor。

        与旧路径顺序一致：两路 TouchSensor 只在子进程内完成构造和 Flash
        操作并上报 sensor_state 后，才通知 Fays open/首帧尝试；随后
        coordinator 线程启动计算子进程并放行输入子进程开始读帧。
        """
        initialized_notified = False

        def notify_initialized():
            nonlocal initialized_notified
            if (
                initialized_notified
                or on_initialized is None
                or not self._active(generation)
            ):
                return
            initialized_notified = True
            try:
                on_initialized(generation)
            except Exception as exc:
                self.logger(
                    "[Tactile] initialized callback failed: "
                    f"{exc}"
                )

        self._ensure_sightac_syspath()
        try:
            resolved = self._resolve_inputs(generation, inputs)
            for side in ("left", "right"):
                if not self._active(generation):
                    return
                index = resolved.get(side)
                with self.state.lock:
                    self.state.sides[side].video_index = index
                if index is None:
                    self._set_error(side, "no Sightac device id")
                    with self.state.lock:
                        self.state.sides[side].ready_event.set()
                    continue
                pipeline = TactileProcessBridge(
                    self.context,
                    materialize_force_matrix=(
                        self.materialize_force_matrix),
                )
                initial_job_id = pipeline.next_job_id()
                with self.state.lock:
                    side_state = self.state.sides[side]
                    side_state.pipeline = pipeline
                    side_state.initializing = True
                try:
                    self._spawn_input_process(
                        generation, side, int(index), pipeline,
                        inputs, warmup_frames, initial_job_id,
                    )
                    self._wait_input_ready(
                        generation, side, pipeline)
                except Exception as exc:
                    pipeline.request_stop()
                    with self.state.lock:
                        side_state = self.state.sides[side]
                        side_state.initializing = False
                        side_state.ready_event.set()
                    self._stop_process(side_state.input_process)
                    self._set_error(side, exc)
                    traceback.print_exc()

            notify_initialized()
            for side in ("left", "right"):
                if not self._active(generation):
                    return
                with self.state.lock:
                    side_state = self.state.sides[side]
                    ok = (
                        side_state.pipeline is not None
                        and side_state.input_process is not None
                        and self._process_is_alive(
                            side_state.input_process)
                        and side_state.sensor_state is not None
                    )
                if not ok:
                    continue
                coordinator = self.thread_factory(
                    target=self._run_side_input_process,
                    args=(generation, side),
                    daemon=True,
                    name=(
                        f"tactile-{side}-coordinator-{generation}"),
                )
                with self.state.lock:
                    self.state.sides[side].coordinator_thread = (
                        coordinator)
                coordinator.start()
        except Exception as exc:
            self.logger(f"[Tactile] initialization error: {exc}")
            traceback.print_exc()
            notify_initialized()
        finally:
            self._notify()

    def _run_side_input_process(
        self, generation: int, side: str,
    ) -> None:
        """输入进程化 coordinator：启动计算子进程并消费结果。

        输入子进程已由 _initialize_all_input 启动并在 begin_event 上等待；
        本线程（主进程）只负责计算子进程与结果/状态汇总，不再创建 input
        线程，也不持有 TouchSensor/VideoCapture。
        """
        sensor_name = "sensor2" if side == "left" else "sensor3"
        pipeline = None
        process = None
        input_process = None
        try:
            self._bind_coordinator(side)
            if not self._active(generation):
                return
            with self.state.lock:
                side_state = self.state.sides[side]
                pipeline = side_state.pipeline
                input_process = side_state.input_process
                sensor_state = side_state.sensor_state
            if (
                pipeline is None
                or input_process is None
                or sensor_state is None
            ):
                raise RuntimeError(
                    f"{sensor_name} input process did not become ready")
            if not self._process_is_alive(input_process):
                raise RuntimeError(
                    f"{sensor_name} input process exited unexpectedly: "
                    f"exitcode={getattr(input_process, 'exitcode', None)}")
            cpu = int(self.cpu_map[side])
            self._ensure_sightac_syspath()
            process = self.context.Process(
                target=self.process_target,
                args=(
                    side,
                    cpu,
                    sensor_state,
                    pipeline.frame_queue,
                    pipeline.command_queue,
                    pipeline.result_queue,
                    pipeline.status_queue,
                    pipeline.stop_event,
                    pipeline.shared_frame_storage,
                    pipeline.free_frame_slots,
                    pipeline.frame_slot_bytes,
                    pipeline.result_shared_storage,
                    pipeline.free_result_slots,
                    pipeline.result_slot_bytes,
                    self._record_queue,
                    self._record_gate,
                ),
                daemon=True,
                name=f"tactile-{side}-process-{generation}",
            )
            with self._spawn_lock:
                process.start()
            self._verify_child_affinity(process, cpu)
            with self.state.lock:
                side_state = self.state.sides[side]
                if (
                    self.state.generation != generation
                    or not self.state.running
                ):
                    raise RuntimeError(
                        "tactile generation invalidated")
                side_state.process = process
            try:
                ready = pipeline.status_queue.get(
                    timeout=self.PROCESS_READY_TIMEOUT)
            except queue.Empty as exc:
                raise RuntimeError(
                    f"{sensor_name} subprocess did not become ready"
                ) from exc
            if ready[0] == "fatal":
                self._handle_status(
                    side, sensor_name, pipeline, ready)
            if ready[0] != "ready":
                raise RuntimeError(
                    f"{sensor_name} unexpected startup message: "
                    f"{ready[0]}")
            _, child_pid, child_cpus = ready
            if (
                int(child_pid) != process.pid
                or tuple(child_cpus) != (cpu,)
            ):
                raise RuntimeError(
                    f"{sensor_name} child verification failed: "
                    f"pid={child_pid} cpus={tuple(child_cpus)} "
                    f"expected={(cpu,)}")
            self.logger(
                f"[Tactile] process bound: side={side} "
                f"pid={process.pid} logical-cpu={cpu}")
            # 计算子进程就绪后放行输入子进程开始读帧。
            pipeline.begin_event.set()
            with self.state.lock:
                self.state.sides[side].ready_event.set()
            self._notify()

            while (
                self._active(generation)
                and self._process_is_alive(process)
                and self._process_is_alive(input_process)
                and not pipeline.stopping
            ):
                while True:
                    try:
                        status = pipeline.status_queue.get_nowait()
                    except queue.Empty:
                        break
                    self._handle_status(
                        side, sensor_name, pipeline, status)
                while True:
                    try:
                        istatus = (
                            pipeline.input_status_queue.get_nowait())
                    except queue.Empty:
                        break
                    self._handle_input_status(
                        side, sensor_name, pipeline, istatus)
                try:
                    result = pipeline.get_result(timeout=0.05)
                except queue.Empty:
                    continue
                self._publish_result(side, pipeline, result)

            if self._active(generation) and not pipeline.stopping:
                if not self._process_is_alive(process):
                    raise RuntimeError(
                        f"{sensor_name} subprocess exited unexpectedly: "
                        f"exitcode={getattr(process, 'exitcode', None)}")
                if not self._process_is_alive(input_process):
                    raise RuntimeError(
                        f"{sensor_name} input process exited "
                        f"unexpectedly: exitcode="
                        f"{getattr(input_process, 'exitcode', None)}")
        except Exception as exc:
            self._set_error(side, exc)
            traceback.print_exc()
        finally:
            if pipeline is not None:
                pipeline.request_stop()
            self._stop_process(process)
            self._stop_process(input_process)
            process_alive = self._process_is_alive(process)
            input_alive = self._process_is_alive(input_process)
            if (
                process is not None
                and not process_alive
                and self.on_process_stop is not None
            ):
                try:
                    self.on_process_stop(side, int(process.pid))
                except Exception:
                    pass
            with self.state.lock:
                side_state = self.state.sides[side]
                side_state.ready_event.set()
                if side_state.process is process and not process_alive:
                    side_state.process = None
                if (
                    side_state.input_process is input_process
                    and not input_alive
                ):
                    side_state.input_process = None
                if side_state.pipeline is pipeline and (
                    not process_alive and not input_alive
                ):
                    side_state.pipeline = None
                if (
                    side_state.sensor_state is not None
                    and not input_alive
                ):
                    side_state.sensor_state = None
                if (
                    side_state.coordinator_thread
                    is threading.current_thread()
                ):
                    # 引用保留到 stop 的退出确认。
                    pass
            if pipeline is not None:
                stats = pipeline.snapshot()
                self.logger(
                    f"[Tactile] {sensor_name} input-process FIFO: "
                    f"published={stats['published']} "
                    f"dropped_new={stats['dropped_new_frames']} "
                    f"dropped_result={stats['dropped_results']} "
                    f"taken={stats['taken']} "
                    f"input-pid={getattr(input_process, 'pid', None)} "
                    f"process-pid={getattr(process, 'pid', None)} "
                    f"mode=input-process transport=shared-memory"
                )
            if pipeline is not None and (
                not process_alive and not input_alive
            ):
                pipeline.close()
            self._notify()

    def _handle_input_status(
        self,
        side: str,
        sensor_name: str,
        pipeline: TactileProcessBridge,
        status,
    ) -> None:
        """处理 input 子进程上报的状态（输入进程化模式）。"""
        kind = status[0]
        if kind == "calibration_started":
            _, job_id, reason = status
            pipeline.mark_input_calibrating()
            waiter = pipeline.bind_input_waiter(job_id, reason)
            if waiter is not None:
                self.logger(
                    f"[Tactile] {sensor_name} calibration started: "
                    f"job={int(job_id)} reason={reason}")
            return
        if kind == "calibration_busy":
            _, job_id, reason = status
            self.logger(
                f"[Tactile] {sensor_name} calibration busy: "
                f"active job={int(job_id)} reason={reason}")
            pipeline.fail_first_input_waiter(
                "calibration already in progress")
            return
        if kind == "frame_activity":
            _, event_ns = status
            if self.record_event is not None:
                try:
                    self.record_event(
                        f"sightac_{side}_input", int(event_ns))
                except Exception:
                    pass
            return
        if kind == "input_stats":
            _, published, dropped_new, _empty_reads = status
            pipeline.record_input_stats(published, dropped_new)
            return
        if kind == "error":
            message = status[1]
            detail = status[2] if len(status) > 2 else ""
            self._set_error(side, message)
            if detail:
                self.logger(detail.rstrip())
            return
        if kind == "fatal":
            message = status[1]
            detail = status[2] if len(status) > 2 else ""
            raise RuntimeError(
                f"{sensor_name} input process startup failed: "
                f"{message}\n{detail}")

    def _calibrate_input(
        self, timeout: float = TACTILE_CALIBRATION_TIMEOUT,
    ):
        """输入进程化：并行请求左右手动标定并等待计算子进程完成。"""
        waiters = {}
        with self.state.lock:
            if not self.state.running:
                raise RuntimeError("tactile manager is not running")
            pipelines = {
                side: self.state.sides[side].pipeline
                for side in ("left", "right")
            }
            missing = [
                side for side, pipeline in pipelines.items()
                if pipeline is None
            ]
            if missing:
                raise RuntimeError(
                    f"{missing[0]} tactile pipeline is not running")
            self.state.calibrating = True
            for side in ("left", "right"):
                pipeline = pipelines[side]
                waiter = pipeline.request_input_calibration(
                    reason="manual",
                    warmup_frames=0,
                    frame_count=INITIAL_BASELINE_FRAMES,
                    wait=True,
                )
                if waiter is None:
                    self.state.calibrating = False
                    raise RuntimeError(
                        f"{side} tactile pipeline is stopping")
                waiters[side] = waiter
        self._notify()
        results = {}
        deadline = self.clock() + max(0.0, float(timeout))
        try:
            for side, waiter in waiters.items():
                remaining = max(0.0, deadline - self.clock())
                if not waiter.done.wait(timeout=remaining):
                    raise TimeoutError(
                        f"{side} calibration timed out")
                if not waiter.success:
                    raise RuntimeError(
                        f"{side} calibration failed: "
                        f"{waiter.error or 'unknown error'}")
                results[side] = True
                self.logger(
                    f"[Calib] {side} calibration done")
            return MappingProxyType(results)
        finally:
            with self.state.lock:
                self.state.calibrating = False
            self._notify()

    def _initialize_all(
        self,
        generation: int,
        inputs: Mapping[str, object],
        warmup_frames: int,
        on_initialized: Optional[Callable[[int], None]],
    ) -> None:
        if self.input_process_mode:
            self._initialize_all_input(
                generation, inputs, warmup_frames, on_initialized)
            return
        sensors = {}
        initialized_notified = False

        def notify_initialized():
            nonlocal initialized_notified
            if (
                initialized_notified
                or on_initialized is None
                or not self._active(generation)
            ):
                return
            initialized_notified = True
            try:
                on_initialized(generation)
            except Exception as exc:
                self.logger(
                    "[Tactile] initialized callback failed: "
                    f"{exc}"
                )

        try:
            resolved = self._resolve_inputs(generation, inputs)
            for side in ("left", "right"):
                if not self._active(generation):
                    return
                index = resolved.get(side)
                with self.state.lock:
                    self.state.sides[side].video_index = index
                if index is None:
                    self._set_error(side, "no Sightac device id")
                    with self.state.lock:
                        self.state.sides[side].ready_event.set()
                    continue
                reserved_camera = inputs.get(side)
                if (
                    getattr(
                        getattr(reserved_camera, "node", None),
                        "role",
                        None,
                    )
                    != side
                ):
                    reserved_camera = None
                sensor = self._initialize_sensor(
                    generation,
                    side,
                    int(index),
                    reserved_camera=reserved_camera,
                )
                if sensor is not None:
                    sensors[side] = sensor

            # 重构前顺序：两路 TouchSensor 只完成构造和 Flash 操作，
            # 随后 Fays 完成 open/首帧尝试，最后才启动任一路触觉 read。
            notify_initialized()
            for side, sensor in sensors.items():
                if not self._active(generation):
                    self._release_sensor(sensor)
                    continue
                sensor._ksq_initial_warmup_frames = int(
                    warmup_frames)
                coordinator = self.thread_factory(
                    target=self._run_side,
                    args=(generation, side, sensor),
                    daemon=True,
                    name=(
                        f"tactile-{side}-coordinator-{generation}"),
                )
                with self.state.lock:
                    side_state = self.state.sides[side]
                    side_state.sensor = sensor
                    side_state.coordinator_thread = coordinator
                coordinator.start()
        except Exception as exc:
            self.logger(f"[Tactile] initialization error: {exc}")
            traceback.print_exc()
            notify_initialized()
        finally:
            with self.state.lock:
                if (
                    self.state.initializer_thread
                    is threading.current_thread()
                ):
                    # 保留已退出线程对象到 stop/wait 检查后再清理。
                    pass
            self._notify()

    def _initialize_sensor(
        self,
        generation: int,
        side: str,
        video_index: int,
        *,
        reserved_camera=None,
    ):
        result = [None]
        error = [None]
        timed_out = threading.Event()

        def do_init():
            sensor = None
            try:
                if self.bind_thread is not None:
                    bound = self.bind_thread("input")
                    if bound is False:
                        raise RuntimeError(
                            "cannot bind Sightac init to CPU4")
                if reserved_camera is None:
                    sensor = self.discovery.init_sensor(video_index)
                else:
                    sensor = self.discovery.init_sensor(
                        video_index,
                        reserved_camera=reserved_camera,
                    )
                if (
                    sensor is None
                    or timed_out.is_set()
                    or not self._active(generation)
                ):
                    self._release_sensor(sensor)
                    return
                result[0] = sensor
            except Exception as exc:
                error[0] = exc

        thread = self.thread_factory(
            target=do_init,
            daemon=True,
            name=f"tactile-{side}-init-{generation}",
        )
        with self.state.lock:
            side_state = self.state.sides[side]
            side_state.initializing = True
            side_state.init_thread = thread
        thread.start()
        thread.join(timeout=self.INIT_TIMEOUT)
        if thread.is_alive():
            timed_out.set()
            with self.state.lock:
                self.state.sides[side].initializing = False
                self.state.sides[side].ready_event.set()
            self._set_error(
                side,
                f"initialization timed out after "
                f"{self.INIT_TIMEOUT:.1f}s (idx={video_index})",
            )
            return None
        with self.state.lock:
            self.state.sides[side].initializing = False
        if not self._active(generation):
            self._release_sensor(result[0])
            return None
        if error[0] is not None:
            with self.state.lock:
                self.state.sides[side].ready_event.set()
            self._set_error(side, error[0])
            return None
        if result[0] is None:
            with self.state.lock:
                self.state.sides[side].ready_event.set()
            self._set_error(side, "TouchSensor construction failed")
            return None
        self.logger(
            f"[Tactile] {side} TouchSensor idx={video_index} initialized")
        return result[0]

    def _bind_coordinator(self, side: str) -> None:
        if self.bind_coordinator is not None:
            if self.bind_coordinator(side) is False:
                raise RuntimeError(
                    f"cannot bind {side} tactile coordinator "
                    "to tactile compute CPU")
            return
        if self.bind_general_thread is None:
            return
        if self.bind_general_thread(
            f"tactile-{side}-coordinator"
        ) is False:
            raise RuntimeError(
                f"cannot bind {side} tactile coordinator "
                "to general CPUs")

    @staticmethod
    def _process_is_alive(process) -> bool:
        if process is None:
            return False
        try:
            return bool(process.is_alive())
        except (AssertionError, ValueError):
            return False

    def _verify_child_affinity(self, process, cpu: int) -> None:
        if (
            not sys.platform.startswith("linux")
            or self.set_process_affinity is None
            or self.get_process_affinity is None
        ):
            return
        self.set_process_affinity(process.pid, {int(cpu)})
        actual = tuple(sorted(
            self.get_process_affinity(process.pid)))
        if actual != (int(cpu),):
            raise RuntimeError(
                f"child affinity mismatch: expected={(int(cpu),)} "
                f"actual={actual}")

    def _run_side(
        self, generation: int, side: str, sensor,
    ) -> None:
        if self.input_process_mode:
            self._run_side_input_process(generation, side)
            return
        sensor_name = "sensor2" if side == "left" else "sensor3"
        empty_reads = 0
        pipeline = None
        process = None
        input_thread = None
        try:
            self._bind_coordinator(side)
            if not self._active(generation):
                return
            cpu = int(self.cpu_map[side])
            pipeline = TactileProcessBridge(
                self.context,
                materialize_force_matrix=(
                    self.materialize_force_matrix),
            )
            sensor_state = snapshot_sensor_state(sensor)
            sensor._ksq_pipeline = pipeline
            warmup_frames = int(
                getattr(
                    sensor, "_ksq_initial_warmup_frames", 0,
                ) or 0
            )
            initial_job = pipeline.request_calibration(
                reason="initial",
                warmup_frames=warmup_frames,
                frame_count=INITIAL_BASELINE_FRAMES,
            )
            if initial_job is None:
                raise RuntimeError("initial calibration was rejected")
            self.logger(
                f"[Tactile] {sensor_name} initial calibration queued: "
                f"warmup={warmup_frames}, "
                f"samples={INITIAL_BASELINE_FRAMES}"
            )
            process = self.context.Process(
                target=self.process_target,
                args=(
                    side,
                    cpu,
                    sensor_state,
                    pipeline.frame_queue,
                    pipeline.command_queue,
                    pipeline.result_queue,
                    pipeline.status_queue,
                    pipeline.stop_event,
                    pipeline.shared_frame_storage,
                    pipeline.free_frame_slots,
                    pipeline.frame_slot_bytes,
                    pipeline.result_shared_storage,
                    pipeline.free_result_slots,
                    pipeline.result_slot_bytes,
                    self._record_queue,
                    self._record_gate,
                ),
                daemon=True,
                name=f"tactile-{side}-process-{generation}",
            )
            with self._spawn_lock:
                process.start()
            self._verify_child_affinity(process, cpu)
            sensor._ksq_process = process
            with self.state.lock:
                side_state = self.state.sides[side]
                if (
                    self.state.generation != generation
                    or not self.state.running
                ):
                    raise RuntimeError("tactile generation invalidated")
                side_state.pipeline = pipeline
                side_state.process = process

            try:
                ready = pipeline.status_queue.get(
                    timeout=self.PROCESS_READY_TIMEOUT)
            except queue.Empty as exc:
                raise RuntimeError(
                    f"{sensor_name} subprocess did not become ready"
                ) from exc
            if ready[0] == "fatal":
                self._handle_status(
                    side, sensor_name, pipeline, ready)
            if ready[0] != "ready":
                raise RuntimeError(
                    f"{sensor_name} unexpected startup message: "
                    f"{ready[0]}")
            _, child_pid, child_cpus = ready
            if (
                int(child_pid) != process.pid
                or tuple(child_cpus) != (cpu,)
            ):
                raise RuntimeError(
                    f"{sensor_name} child verification failed: "
                    f"pid={child_pid} cpus={tuple(child_cpus)} "
                    f"expected={(cpu,)}"
                )
            self.logger(
                f"[Tactile] process bound: side={side} "
                f"pid={process.pid} logical-cpu={cpu}")

            input_thread = self.thread_factory(
                target=self._input_loop,
                args=(
                    generation,
                    side,
                    sensor,
                    pipeline,
                ),
                daemon=True,
                name=f"tactile-{side}-input-{generation}",
            )
            sensor._ksq_capture_thread = input_thread
            with self.state.lock:
                self.state.sides[side].input_thread = input_thread
                self.state.sides[side].ready_event.set()
            input_thread.start()
            self._notify()

            while (
                self._active(generation)
                and self._process_is_alive(process)
                and not pipeline.stopping
            ):
                while True:
                    try:
                        status = pipeline.status_queue.get_nowait()
                    except queue.Empty:
                        break
                    self._handle_status(
                        side, sensor_name, pipeline, status)
                try:
                    result = pipeline.get_result(timeout=0.05)
                except queue.Empty:
                    continue
                self._publish_result(side, pipeline, result)

            if (
                self._active(generation)
                and not pipeline.stopping
                and not self._process_is_alive(process)
            ):
                raise RuntimeError(
                    f"{sensor_name} subprocess exited unexpectedly: "
                    f"exitcode={getattr(process, 'exitcode', None)}")
        except Exception as exc:
            self._set_error(side, exc)
            traceback.print_exc()
        finally:
            if pipeline is not None:
                pipeline.request_stop()
            if (
                input_thread is not None
                and input_thread.is_alive()
                and input_thread is not threading.current_thread()
            ):
                input_thread.join(
                    timeout=self.INPUT_UNBLOCK_TIMEOUT)
            self._stop_process(process)
            process_alive = self._process_is_alive(process)
            if (
                process is not None
                and not process_alive
                and self.on_process_stop is not None
            ):
                try:
                    self.on_process_stop(side, int(process.pid))
                except Exception:
                    pass
            with self.state.lock:
                side_state = self.state.sides[side]
                side_state.ready_event.set()
                if side_state.input_thread is input_thread:
                    side_state.input_thread = (
                        input_thread
                        if input_thread is not None
                        and input_thread.is_alive()
                        else None
                    )
                if side_state.process is process and not process_alive:
                    side_state.process = None
                if side_state.pipeline is pipeline and not process_alive:
                    side_state.pipeline = None
                if (
                    side_state.coordinator_thread
                    is threading.current_thread()
                ):
                    # 引用保留到 stop 的退出确认。
                    pass
            if getattr(sensor, "_ksq_pipeline", None) is pipeline:
                sensor._ksq_pipeline = None
            if getattr(sensor, "_ksq_process", None) is process:
                sensor._ksq_process = None
            if (
                input_thread is not None
                and not input_thread.is_alive()
                and getattr(sensor, "_ksq_capture_thread", None)
                is input_thread
            ):
                sensor._ksq_capture_thread = None
            if pipeline is not None:
                stats = pipeline.snapshot()
                self.logger(
                    f"[Tactile] {sensor_name} FIFO: "
                    f"published={stats['published']} "
                    f"dropped_new={stats['dropped_new_frames']} "
                    f"dropped_result={stats['dropped_results']} "
                    f"taken={stats['taken']} "
                    f"mode=drop-new transport=shared-memory"
                )
            if pipeline is not None and not process_alive:
                pipeline.close()
            self._notify()

    def _input_loop(
        self,
        generation: int,
        side: str,
        sensor,
        pipeline: TactileProcessBridge,
    ) -> None:
        sensor_name = "sensor2" if side == "left" else "sensor3"
        empty_reads = 0
        if self.bind_thread is not None:
            if self.bind_thread("input") is False:
                self._set_error(
                    side, "cannot bind Sightac input CPU domain")
                pipeline.request_stop()
                return
        try:
            while self._active(generation) and not pipeline.stopping:
                try:
                    with pipeline.capture_lock:
                        if (
                            pipeline.stopping
                            or not self._active(generation)
                        ):
                            continue
                        raw_frame = getattr(
                            sensor, "_ksq_prefetched_frame", None)
                        if raw_frame is not None:
                            sensor._ksq_prefetched_frame = None
                        else:
                            raw_frame = sensor.read_raw_frame()
                        captured_at = self.clock()
                    if raw_frame is None:
                        empty_reads += 1
                        capture = getattr(sensor, "cap", None)
                        transport = getattr(capture, "transport", "")
                        if transport == "libuvc-ipc":
                            try:
                                capture.reconnect()
                            except Exception as exc:
                                self.logger(
                                    f"[TactileInput:{sensor_name}] "
                                    f"IPC reconnect failed: {exc}")
                        if empty_reads in (1, 10, 30, 60):
                            self.logger(
                                f"[TactileInput:{sensor_name}] no frame "
                                f"count={empty_reads} transport={transport!r} "
                                f"reconnects={getattr(capture, 'reconnect_count', 0)} "
                                f"error={getattr(capture, 'last_transport_error', '')!r}")
                        if empty_reads >= 60:
                            self._set_error(
                                side,
                                f"{sensor_name} Sightac IPC has no frame for "
                                f"60 reads (reconnects="
                                f"{getattr(capture, 'reconnect_count', 0)}, "
                                f"transport_error="
                                f"{getattr(capture, 'last_transport_error', '') or 'unknown'})",
                            )
                            pipeline.request_stop()
                        self.sleep(0.01)
                        continue
                    empty_reads = 0
                    if (
                        pipeline.stopping
                        or not self._active(generation)
                    ):
                        continue
                    if self.record_event is not None:
                        self.record_event(
                            f"sightac_{side}_input")
                    pipeline.submit_capture(
                        raw_frame, captured_at)
                except Exception as exc:
                    self.logger(
                        f"[TactileInput:{sensor_name}] ERROR: {exc}")
                    traceback.print_exc()
                    self.sleep(0.05)
        finally:
            if self.unbind_thread is not None:
                try:
                    self.unbind_thread("input")
                except Exception:
                    pass

    def _handle_status(
        self,
        side: str,
        sensor_name: str,
        pipeline: TactileProcessBridge,
        status,
    ) -> None:
        kind = status[0]
        if kind == "calibration_result":
            (
                _kind,
                job_id,
                success,
                error,
                reason,
                samples,
            ) = status
            if self.input_process_mode:
                # job 状态在 input 子进程：只转发完成并解析手动 waiter。
                pipeline.resolve_input_calibration(
                    job_id, success, error)
                if success:
                    self.logger(
                        f"[Tactile] {sensor_name} calibration done in "
                        f"{side} input process: reason={reason}, "
                        f"samples={samples}")
                else:
                    self._set_error(
                        side,
                        f"calibration failed ({reason}): {error}",
                    )
                return
            completed = pipeline.finish_calibration(
                job_id, success, error)
            if completed and success:
                self.logger(
                    f"[Tactile] {sensor_name} calibration done in "
                    f"{side} process: reason={reason}, "
                    f"samples={samples}")
            elif completed:
                self._set_error(
                    side,
                    f"calibration failed ({reason}): {error}",
                )
            return
        if kind == "recalibration_request":
            reason = status[1]
            if self.input_process_mode:
                pipeline.request_input_calibration(
                    reason=reason,
                    warmup_frames=0,
                    frame_count=INITIAL_BASELINE_FRAMES,
                    wait=False,
                )
                self.logger(
                    f"[Tactile] {sensor_name} calibration requested: "
                    f"{reason}")
                return
            job = pipeline.request_calibration(
                reason=reason,
                warmup_frames=0,
                frame_count=INITIAL_BASELINE_FRAMES,
            )
            if job is not None:
                self.logger(
                    f"[Tactile] {sensor_name} calibration requested: "
                    f"{job.reason}")
            return
        if kind == "result_dropped":
            pipeline.record_result_drops(status[1])
            return
        if kind == "error":
            message = status[1]
            detail = status[2] if len(status) > 2 else ""
            self._set_error(side, message)
            if detail:
                self.logger(detail.rstrip())
            return
        if kind == "fatal":
            message = status[1]
            detail = status[2] if len(status) > 2 else ""
            raise RuntimeError(
                f"{sensor_name} subprocess startup failed: "
                f"{message}\n{detail}")

    def _publish_result(
        self,
        side: str,
        pipeline: TactileProcessBridge,
        result,
    ) -> None:
        if result.get("kind", "frame") != "frame":
            return
        pipeline.materialize_result(result)
        received_at = self.clock()
        publish_started = time.perf_counter()
        heatmap = result["heatmap"]
        calculated_force = tuple(result["force"])
        calculated_matrix = result.get("force_matrix")
        with self.state.lock:
            side_state = self.state.sides[side]
            params = self.state.flash_full_params.get(
                side_state.video_index, {})
            calibration_source = str(params.get(
                "calibration_source", "hardware"))
            # 只有当前硬件自己的完整 Flash 才是有效力标定。旧相机模板或
            # vendor defaults 只能用于临时图像/热力图，绝不能把计算结果送入
            # 夹持判定、力曲线或 HDF5 力通道。三维力矩阵与合力同源，
            # 服从同一硬件 Flash 标定闸门；hardware-live-flash 是 libusb/libuvc
            # 迁移后的同一次硬件实读来源。
            force = (
                calculated_force
                if calibration_source in HARDWARE_CALIBRATION_SOURCES
                else None
            )
            force_matrix = (
                calculated_matrix
                if calibration_source in HARDWARE_CALIBRATION_SOURCES
                else None
            )
            side_state.heatmap = heatmap
            side_state.force = force
            diag = self._matrix_diag.setdefault(side, {
                "frames": 0, "matrix_frames": 0, "logged": False})
            diag["frames"] += 1
            if force_matrix is not None:
                diag["matrix_frames"] += 1
            if (
                not diag["logged"]
                and diag["frames"] >= 60
                and force is not None
                and diag["matrix_frames"] == 0
            ):
                diag["logged"] = True
                if self.logger is not None:
                    self.logger(
                        "[Gripper-Tactile] {} 侧连续 {} 帧力标量有效但"
                        "力矩阵为空：计算子进程未产出 fx/fy/fz 矩阵"
                        " (250x250x3)".format(side, diag["frames"]))
            local_publish_ms = (
                time.perf_counter() - publish_started) * 1000.0
            ipc_ms = max(
                0.0,
                (
                    received_at
                    - float(result.get("result_ready_at", received_at))
                ) * 1000.0,
            )
            diagnostics = dict(result.get("diagnostics", {}))
            diagnostics["publish_ms"] = (
                ipc_ms + local_publish_ms)
            pipeline.record_processed(result.get("captured_at"))
            if self.record_stage is not None:
                self.record_stage(side, diagnostics)
            if self.record_event is not None:
                self.record_event(f"sightac_{side}_process")
            if self.publish_result is not None:
                self.publish_result(side, heatmap, force, force_matrix)
            self._notify()

    def wait_ready(self, timeout: Optional[float] = None):
        """等待左右两路各自 ready 或独立失败。"""
        timeout = (
            self.PROCESS_READY_TIMEOUT
            if timeout is None
            else max(0.0, float(timeout))
        )
        deadline = self.clock() + timeout
        with self.state.lock:
            events = [
                self.state.sides[side].ready_event
                for side in ("left", "right")
            ]
        for event in events:
            remaining = max(0.0, deadline - self.clock())
            if not event.wait(timeout=remaining):
                return False
        return True

    def calibrate(
        self, timeout: float = TACTILE_CALIBRATION_TIMEOUT,
    ):
        """并行请求左右手动标定；协调线程本身不读帧也不做计算。"""
        if self.input_process_mode:
            return self._calibrate_input(timeout)
        jobs = []
        with self.state.lock:
            if not self.state.running:
                raise RuntimeError("tactile manager is not running")
            pipelines = {
                side: self.state.sides[side].pipeline
                for side in ("left", "right")
            }
            missing = [
                side for side, pipeline in pipelines.items()
                if pipeline is None
            ]
            if missing:
                raise RuntimeError(
                    f"{missing[0]} tactile pipeline is not running")
            self.state.calibrating = True
            for side in ("left", "right"):
                pipeline = pipelines[side]
                job = pipeline.request_calibration(
                    reason="manual",
                    warmup_frames=0,
                    frame_count=INITIAL_BASELINE_FRAMES,
                )
                if job is None:
                    self.state.calibrating = False
                    raise RuntimeError(
                        f"{side} tactile pipeline is stopping")
                jobs.append((side, job))
        self._notify()
        results = {}
        deadline = self.clock() + max(0.0, float(timeout))
        try:
            for side, job in jobs:
                remaining = max(0.0, deadline - self.clock())
                if not job.done.wait(timeout=remaining):
                    raise TimeoutError(
                        f"{side} calibration timed out")
                if not job.success:
                    raise RuntimeError(
                        f"{side} calibration failed: "
                        f"{job.error or 'unknown error'}")
                results[side] = True
                self.logger(
                    f"[Calib] {side} calibration done")
            return MappingProxyType(results)
        finally:
            with self.state.lock:
                self.state.calibrating = False
            self._notify()

    def calibrate_async(
        self,
        callback: Optional[Callable[[object, Optional[Exception]], None]]
        = None,
    ):
        def run():
            try:
                result = self.calibrate()
                error = None
            except Exception as exc:
                result, error = None, exc
            if callback is not None:
                callback(result, error)

        thread = self.thread_factory(
            target=run,
            daemon=True,
            name="tactile-manual-calibration",
        )
        thread.start()
        return thread

    def _stop_process(self, process) -> None:
        if process is None:
            return
        try:
            pid = process.pid
        except Exception:
            pid = None
        if pid is None:
            return
        if self._process_is_alive(process):
            process.join(timeout=self.PROCESS_JOIN_TIMEOUT)
        if self._process_is_alive(process):
            self.logger(
                f"[Tactile] process pid={pid} did not stop; "
                "terminating")
            process.terminate()
            process.join(
                timeout=self.PROCESS_TERMINATE_TIMEOUT)
        if self._process_is_alive(process):
            # 输入子进程可能阻塞在 V4L2 read；terminate 未退出时
            # SIGKILL 兜底，保证 stop() 能确认全部所有者退出。
            self.logger(
                f"[Tactile] process pid={pid} still alive; "
                "killing")
            try:
                process.kill()
            except Exception:
                pass
            process.join(
                timeout=self.PROCESS_TERMINATE_TIMEOUT)

    @staticmethod
    def _dedupe_threads(threads):
        result = []
        seen = set()
        for thread in threads:
            if thread is None or id(thread) in seen:
                continue
            seen.add(id(thread))
            result.append(thread)
        return result

    def stop(self) -> bool:
        """停止并确认 init/input/coordinator/process 全部退出后才成功返回。"""
        with self.state.lock:
            has_resources = (
                self.state.running
                or self.state.initializer_thread is not None
                or any(
                    side_state.sensor is not None
                    or side_state.init_thread is not None
                    or side_state.input_thread is not None
                    or side_state.coordinator_thread is not None
                    or side_state.process is not None
                    or side_state.input_process is not None
                    for side_state in self.state.sides.values()
                )
            )
            if not has_resources:
                return True
            self.state.generation += 1
            self.state.running = False
            self.state.calibrating = False
            initializer = self.state.initializer_thread
            side_states = list(self.state.sides.values())
            sensors = {
                side_state.side: side_state.sensor
                for side_state in side_states
            }
            pipelines = [
                side_state.pipeline for side_state in side_states
                if side_state.pipeline is not None
            ]
            threads = self._dedupe_threads(
                [initializer]
                + [
                    thread
                    for side_state in side_states
                    for thread in (
                        side_state.init_thread,
                        side_state.input_thread,
                        side_state.coordinator_thread,
                    )
                ]
            )
        for pipeline in pipelines:
            pipeline.request_stop()

        deadline = self.clock() + self.STOP_DEADLINE
        for thread in threads:
            if (
                not thread.is_alive()
                or thread is threading.current_thread()
            ):
                continue
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

        # V4L2 read 极端阻塞时，只释放仍有活输入线程的对应 cap 来唤醒它。
        for side_state in side_states:
            input_thread = side_state.input_thread
            if input_thread is not None and input_thread.is_alive():
                self.logger(
                    f"[Tactile] forcing blocked input to exit: "
                    f"{input_thread.name}")
                self._release_sensor(sensors[side_state.side])
        for side_state in side_states:
            input_thread = side_state.input_thread
            if (
                input_thread is not None
                and input_thread.is_alive()
                and input_thread is not threading.current_thread()
            ):
                input_thread.join(
                    timeout=self.INPUT_UNBLOCK_TIMEOUT)

        # 协调线程正常负责 graceful join；若它已卡住，manager 仍拥有最终回收权。
        for side_state in side_states:
            self._stop_process(side_state.process)
        for side_state in side_states:
            self._stop_process(side_state.input_process)
        for side_state in side_states:
            coordinator = side_state.coordinator_thread
            if (
                coordinator is not None
                and coordinator.is_alive()
                and coordinator is not threading.current_thread()
            ):
                coordinator.join(
                    timeout=self.PROCESS_TERMINATE_TIMEOUT)

        alive_threads = [
            thread.name for thread in threads
            if thread.is_alive()
        ]
        alive_processes = []
        for side_state in side_states:
            for proc in (side_state.process, side_state.input_process):
                if proc is not None and self._process_is_alive(proc):
                    alive_processes.append(
                        f"{side_state.side}:{getattr(proc, 'pid', None)}")
        if alive_threads or alive_processes:
            details = []
            if alive_threads:
                details.append(f"threads={alive_threads}")
            if alive_processes:
                details.append(f"processes={alive_processes}")
            error = (
                "tactile stop could not confirm all owners exited: "
                + " ".join(details)
            )
            # Record the real wait site; a thread name alone cannot distinguish
            # IPC reads, callbacks, locks, or child-process joins.
            frames = sys._current_frames()
            names = {t.ident: t.name for t in threading.enumerate()}
            stacks = [error + "\n"]
            for ident, frame in frames.items():
                stacks.append(
                    f"\nThread {names.get(ident, ident)}:\n"
                    + "".join(traceback.format_stack(frame)))
            del frames
            try:
                with tempfile.NamedTemporaryFile(
                        mode="w", encoding="utf-8", prefix="ksq-tactile-stop-",
                        suffix=".log", delete=False) as diagnostic:
                    diagnostic.write("".join(stacks))
                    diagnostic_path = diagnostic.name
                error += f"; thread dump: {diagnostic_path}"
                self.logger(f"[TactileStopTimeout] {error}")
            except OSError:
                self.logger("[TactileStopTimeout] " + "".join(stacks))
            for side_state in side_states:
                if side_state.error is None:
                    side_state.error = error
            self._notify()
            raise RuntimeError(error)

        for sensor in sensors.values():
            self._release_sensor(sensor)
        for pipeline in pipelines:
            pipeline.close()
        with self.state.lock:
            self.state.initializer_thread = None
            self.state.mode = None
            for side_state in self.state.sides.values():
                side_state.sensor = None
                side_state.initializing = False
                side_state.init_thread = None
                side_state.input_thread = None
                side_state.coordinator_thread = None
                side_state.process = None
                side_state.input_process = None
                side_state.sensor_state = None
                side_state.pipeline = None
                side_state.force = None
                side_state.heatmap = None
                side_state.ready_event.set()
        self._notify()
        return True

    close = stop
