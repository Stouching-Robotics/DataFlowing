"""Lifecycle owner for the FaysSense ORB-SLAM3 bridge process.

Lock order is ``_lifecycle_lock`` then ``SlamState._lock``.  Stdout callbacks
are invoked without either lock.  All terminate/kill operations are centralized
in this controller; callers only use ``start()``, ``send_origin()`` and
``stop()``.
"""

from dataclasses import dataclass, replace
import json
import os
import queue
import shutil
import socket
import subprocess
import threading
import time
import traceback

from core.gripper.runtime.device_access import device_access_guard, FAYS_SDK_INITIALIZATION_LOCK, fays_config_device_guard

from .protocol import (
    PoseSample, parse_slam_line, pose_rejection_reason, quat_conjugate,
    quat_product, rotate_pose_z90,
)


STDOUT_QUEUE_CAPACITY = 2000
NATIVE_LOG_FLUSH_INTERVAL_S = 0.1
NATIVE_LOG_FLUSH_LINES = 50
PROCESS_WAIT_TIMEOUT_S = 5.0
WORKER_JOIN_TIMEOUT_S = 2.0
STDOUT_JOIN_TIMEOUT_S = 1.0
FINAL_WAIT_TIMEOUT_S = 3.0
STALL_TIMEOUT_S = 5.0
MONITOR_INTERVAL_S = 1.0
RUNNING_REPORT_INTERVAL_S = 60.0   # 心跳行稀疏化，避免刷屏；卡死检测仍 5s
ALLOWED_NICE_ADJUSTMENTS = frozenset({0, 5})


@dataclass(frozen=True)
class SlamSnapshot:
    running: bool
    pid: object
    status: str
    raw_pose_fps: float
    valid_pose_fps: float
    last_frame_id: int
    stalled: bool
    error: object
    stdout_backpressure_count: int
    pose_lost_count: int


class SlamState:
    """Single mutable state source written only by ``SlamProcessController``."""

    def __init__(self):
        self._lock = threading.RLock()
        self.process = None
        self.running = False
        self.status = "Disconnected"
        self.raw_pose_fps = 0.0
        self.valid_pose_fps = 0.0
        self.last_frame_id = -1
        self.stalled = False
        self.error = None
        self.stdout_backpressure_count = 0
        self.pose_lost_count = 0

    def snapshot(self):
        with self._lock:
            process = self.process
            return SlamSnapshot(
                running=self.running,
                pid=getattr(process, "pid", None),
                status=self.status,
                raw_pose_fps=self.raw_pose_fps,
                valid_pose_fps=self.valid_pose_fps,
                last_frame_id=self.last_frame_id,
                stalled=self.stalled,
                error=self.error,
                stdout_backpressure_count=self.stdout_backpressure_count,
                pose_lost_count=self.pose_lost_count,
            )


class SlamProcessController:
    """Start, parse, monitor and stop the single Fays bridge process."""

    def __init__(
        self,
        state,
        *,
        executable,
        vocabulary,
        settings,
        device_config,
        trajectory,
        work_dir,
        runtime_env_factory,
        cpu_roles,
        on_pose=None,
        on_fays_rates=None,
        on_orb_diagnostic=None,
        on_mp_cleanup=None,
        on_status=None,
        frame_snapshot=None,
        log=None,
        process_factory=subprocess.Popen,
        sdk_device_guard_factory=fays_config_device_guard,
        thread_factory=threading.Thread,
        clock=time.monotonic,
        wall_clock=time.time,
        sleep=time.sleep,
        taskset_finder=lambda: shutil.which("taskset"),
        nice_finder=lambda: shutil.which("nice"),
        stdbuf_finder=lambda: shutil.which("stdbuf"),
        nice_adjustment=0,
        set_affinity=os.sched_setaffinity,
        get_affinity=os.sched_getaffinity,
        on_process_stop=None,
        auto_origin=True,
        native_log_path=None,
        pose_paths=(
            "/dev/shm/orb_pose.json.tmp",
            "/dev/shm/orb_pose.json",
        ),
    ):
        self.state = state
        self.executable = executable
        self.vocabulary = vocabulary
        self.settings = settings
        self.device_config = device_config
        self.trajectory = trajectory
        self.work_dir = work_dir
        self.runtime_env_factory = runtime_env_factory
        self.cpu_roles = cpu_roles
        self._on_pose = on_pose or (lambda _pose: None)
        self._on_fays_rates = on_fays_rates or (lambda _rates: None)
        self._on_orb_diagnostic = (
            on_orb_diagnostic or (lambda _diagnostic: None)
        )
        self._on_mp_cleanup = on_mp_cleanup or (lambda _diagnostic: None)
        self._on_status = on_status or (lambda _status: None)
        self._frame_snapshot = frame_snapshot or (lambda: None)
        self._log = log or print
        self._process_factory = process_factory
        self._sdk_device_guard_factory = sdk_device_guard_factory
        self._thread_factory = thread_factory
        self._clock = clock
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._taskset_finder = taskset_finder
        self._nice_finder = nice_finder
        self._stdbuf_finder = stdbuf_finder
        self._nice_adjustment = int(nice_adjustment)
        if self._nice_adjustment not in ALLOWED_NICE_ADJUSTMENTS:
            raise ValueError(
                "Fays nice adjustment must be 0 or 5")
        self._set_affinity = set_affinity
        self._get_affinity = get_affinity
        self._on_process_stop = on_process_stop
        self._auto_origin = bool(auto_origin)
        self._native_log_path = native_log_path
        self._pose_tmp_path, self._pose_path = pose_paths
        self._backend = "orb"
        self._runtime_arguments = None
        self._service_container = None
        self._service_host = None
        self._service_port = None

        self._lifecycle_lock = threading.RLock()
        self._process_stop_lock = threading.Lock()
        self._service_lock = threading.Lock()
        self._service_active = False
        self._stop_event = threading.Event()
        self._worker_thread = None
        self._monitor_thread = None
        self._generation = 0
        self._fake_imu = False
        self._origin_sent_pids = set()
        self._initial_origin_callback = None
        self._initial_origin_generation = None
        self._pose_buffer = {}
        self._pose_buffer_lock = threading.Lock()
        self._last_accepted_pose = None
        self._pose_rejecting = False
        # 原点后姿态相对化：设原点事件后捕获首帧显示姿态作为基准，
        # 此后所有姿态左乘基准的逆 → 第一帧姿态恒为单位矩阵，
        # 帧轴与轨迹同基准（相对原点），见 _publish_pose。
        self._pose_rotation_zero = None
        self._pose_rotation_zero_pending = True
        self._last_pose_reject_log = float("-inf")
        self._unmatched_lines = 0
        self._started_event = threading.Event()
        self._sdk_ready_event = threading.Event()
        self._sdk_initialization_done = threading.Event()
        self._pose_raw_seq = 0
        self._pose_lost_count = 0
        self._last_accepted_seq = 0
        self._latest_pose = None
        self._latest_pose_lock = threading.Lock()

    def wait_sdk_ready(self, timeout=45.0):
        """Wait for SDK + factory calibration, not SLAM tracking/origin."""
        if self._backend != "orb":
            return self.wait_started(timeout)
        if not self._sdk_initialization_done.wait(timeout):
            return False
        snapshot = self.state.snapshot()
        return self._sdk_ready_event.is_set() and snapshot.running and not snapshot.error

    @property
    def running(self):
        return self.state.snapshot().running

    @property
    def backend(self):
        return self._backend

    def set_backend(self, backend):
        normalized = str(backend or "").strip().lower()
        if normalized not in {"orb", "aikit"}:
            raise ValueError(f"unsupported localization backend: {backend!r}")
        with self._lifecycle_lock:
            if self.running:
                raise RuntimeError(
                    "cannot change localization backend while SLAM is running"
                )
            self._backend = normalized
            self._runtime_arguments = None
            self._service_container = None
            self._service_host = None
            self._service_port = None
        self._log(f"[SLAM] Localization backend selected: {normalized}")

    def snapshot(self):
        return self.state.snapshot()

    def pose_buffer_snapshot(self):
        with self._pose_buffer_lock:
            return dict(self._pose_buffer)

    def clear_pose_buffer(self):
        with self._pose_buffer_lock:
            self._pose_buffer.clear()

    def read_latest_pose(self):
        """Single-slot non-blocking read of the most recent accepted pose.

        Returns ``(pose, raw_seq, lost_count)`` or ``(None, 0, 0)`` when
        no pose has been accepted yet.  This is the dedicated control/GUI
        channel — it always returns the freshest sample without queue
        contention.
        """
        with self._latest_pose_lock:
            return (
                self._latest_pose,
                self._pose_raw_seq,
                self._pose_lost_count,
            )

    def pose_lost_count(self):
        with self._latest_pose_lock:
            return self._pose_lost_count

    def _set_state(self, **changes):
        with self.state._lock:
            for name, value in changes.items():
                setattr(self.state, name, value)
            status = self.state.status
        if "status" in changes:
            self._on_status(status)

    def start(self, generation=0, fake_imu=False):
        """Start one worker.  Existing process ownership is released first."""
        self.stop()
        with self._lifecycle_lock:
            self._generation = generation
            self._fake_imu = bool(fake_imu)
            self._initial_origin_generation = None
            self._last_accepted_pose = None
            self._pose_rejecting = False
            self._last_pose_reject_log = float("-inf")
            self._pose_raw_seq = 0
            self._pose_lost_count = 0
            self._last_accepted_seq = 0
            with self._latest_pose_lock:
                self._latest_pose = None
            self._stop_event.clear()
            self._started_event.clear()
            self._sdk_ready_event.clear()
            self._sdk_initialization_done.clear()
            self._set_state(
                running=True,
                process=None,
                status="Starting",
                raw_pose_fps=0.0,
                valid_pose_fps=0.0,
                last_frame_id=-1,
                stalled=False,
                error=None,
                stdout_backpressure_count=0,
                pose_lost_count=0,
            )
            worker = self._thread_factory(
                target=self._run,
                args=(generation,),
                name="slam-supervisor",
                daemon=True,
            )
            monitor = self._thread_factory(
                target=self._monitor_frames,
                args=(generation,),
                name="slam-monitor",
                daemon=True,
            )
            self._worker_thread = worker
            self._monitor_thread = monitor
            worker.start()
            monitor.start()
        return True

    def set_initial_origin_callback(self, callback):
        """Register the lifecycle continuation invoked after READY newline."""
        self._initial_origin_callback = callback

    def initial_origin_sent(self):
        return self._initial_origin_generation == self._generation

    def wait_started(self, timeout=10.0):
        return self._started_event.wait(timeout)

    @property
    def nice_adjustment(self):
        return self._nice_adjustment

    def set_nice_adjustment(self, adjustment):
        target = int(adjustment)
        if target not in ALLOWED_NICE_ADJUSTMENTS:
            raise ValueError("Fays nice adjustment must be 0 or 5")
        if self.running:
            raise RuntimeError(
                "cannot change Fays nice adjustment while SLAM is running")
        self._nice_adjustment = target

    def configure_runtime(
        self, *, executable, settings, calibration_serial=None,
        backend="orb", arguments=None, service_container=None,
        service_host=None, service_port=None,
    ):
        """Select one localization bridge/config pair before process start."""

        with self._lifecycle_lock:
            with self.state._lock:
                process = self.state.process
                active = bool(
                    self.state.running
                    or (
                        process is not None
                        and process.poll() is None
                    )
                )
            if active:
                raise RuntimeError(
                    "cannot change Fays calibration while SLAM is running"
                )
            executable_path = os.path.abspath(os.fspath(executable))
            settings_path = os.path.abspath(os.fspath(settings))
            if not os.path.isfile(settings_path):
                raise RuntimeError(
                    "Fays 定位配置不存在，拒绝启动: "
                    f"{settings_path}"
                )
            if not os.path.isfile(executable_path):
                raise RuntimeError(
                    "Fays 定位二进制不存在，拒绝启动: "
                    f"{executable_path}"
                )
            if not os.access(executable_path, os.X_OK):
                raise RuntimeError(
                    "Fays 定位二进制不可执行，拒绝启动: "
                    f"{executable_path}"
                )
            self.executable = executable_path
            self.settings = settings_path
            normalized_backend = str(backend or "orb").strip().lower()
            if normalized_backend not in {"orb", "aikit"}:
                raise ValueError(
                    f"unsupported localization backend: {backend!r}"
                )
            self._backend = normalized_backend
            self._runtime_arguments = (
                None if arguments is None
                else tuple(os.fspath(value) for value in arguments)
            )
            self._service_container = (
                str(service_container).strip()
                if service_container else None
            )
            self._service_host = (
                str(service_host).strip() if service_host else None
            )
            self._service_port = (
                int(service_port) if service_port is not None else None
            )
            serial = str(calibration_serial or "").strip()
            if serial:
                self._log(
                    "[SLAM] Runtime profile selected: "
                    f"backend={self._backend} serial={serial} "
                    f"settings={self.settings}"
                )

    def _build_launch(self):
        taskset = self._taskset_finder()
        if not taskset:
            raise RuntimeError(
                "taskset is required for Fays CPU9 background isolation"
            )
        background = tuple(self.cpu_roles["fays_background"])
        if not background:
            raise RuntimeError("Fays background CPU role is empty")
        launch_cpus = tuple(sorted({
            cpu
            for role in (
                "fays_input",
                "fays_prepare",
                "fays_left_orb",
                "fays_right_orb",
                "fays_track",
                "fays_background",
            )
            for cpu in self.cpu_roles.get(role, ())
        }))
        if not launch_cpus:
            raise RuntimeError("Fays ORB process CPU domain is empty")
        if self._runtime_arguments is None:
            if self._backend != "orb":
                raise RuntimeError(
                    "AIKit runtime arguments were not configured"
                )
            arguments = (
                self.vocabulary,
                self.settings,
                self.device_config,
                self.trajectory,
            )
        else:
            arguments = self._runtime_arguments
        command = [self.executable, *arguments]
        stdbuf = self._stdbuf_finder()
        if not stdbuf:
            raise RuntimeError(
                "stdbuf is required for line-buffered Fays stdout"
            )
        command = [stdbuf, "-oL", "-eL", *command]
        cpu_spec = ",".join(str(cpu) for cpu in launch_cpus)
        launch_command = [taskset, "--cpu-list", cpu_spec, *command]
        if self._nice_adjustment:
            nice = self._nice_finder()
            if not nice:
                raise RuntimeError(
                    "nice is required for the dual-device Fays profile")
            launch_command = [
                nice,
                "-n",
                str(self._nice_adjustment),
                *launch_command,
            ]
        environment = self.runtime_env_factory()
        for name in (
            "GOMP_CPU_AFFINITY",
            "OMP_PLACES",
            "KMP_AFFINITY",
            "KMP_HW_SUBSET",
            "KMP_PLACE_THREADS",
        ):
            environment.pop(name, None)
        environment.update({
            "OMP_PROC_BIND": "FALSE",
            "OMP_DYNAMIC": "FALSE",
            "OMP_NUM_THREADS": str(len(launch_cpus)),
            "KSQ_FAYS_INPUT_CPU": str(
                (self.cpu_roles.get("fays_input") or (4,))[0]
            ),
            "KSQ_FAYS_PREPARE_CPU": str(
                (self.cpu_roles.get("fays_prepare") or (5,))[0]
            ),
            "KSQ_FAYS_TRACK_CPU": str(
                (self.cpu_roles.get("fays_track") or (8,))[0]
            ),
            "KSQ_FAYS_BACKGROUND_CPU": str(
                (self.cpu_roles.get("fays_background") or (9,))[0]
            ),
            "KSQ_FAYS_ORB_LEFT_CPU": str(
                (self.cpu_roles.get("fays_left_orb") or (6,))[0]
            ),
            "KSQ_FAYS_ORB_RIGHT_CPU": str(
                (self.cpu_roles.get("fays_right_orb") or (7,))[0]
            ),
        })
        # Connect-no-ESP requests fake IMU only from the ORB bridge.  AIKit is
        # still allowed in that UI mode and continues to consume the real S80M
        # IMU inside its service container.
        if self._fake_imu and self._backend == "orb":
            environment["KSQ_FAYS_FAKE_IMU"] = "1"
        else:
            environment.pop("KSQ_FAYS_FAKE_IMU", None)
        # ASan diagnostic builds must load the sanitizer runtime before any
        # instrumented library; the linker cannot guarantee DT_NEEDED order
        # across the vendor libraries, so preload it explicitly. Inactive for
        # every normal binary name.
        if "_asan" in self.executable:
            libasan = "/lib/x86_64-linux-gnu/libasan.so.5"
            if os.path.exists(libasan):
                environment["LD_PRELOAD"] = libasan + (
                    os.pathsep + environment["LD_PRELOAD"]
                    if environment.get("LD_PRELOAD") else "")
        return launch_command, environment, launch_cpus

    def _docker_command(self, *arguments, check=True):
        docker = shutil.which("docker")
        if not docker:
            raise RuntimeError("docker is required for AIKit mode")
        result = subprocess.run(
            [docker, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if check and result.returncode != 0:
            detail = result.stdout.strip() or f"exit={result.returncode}"
            raise RuntimeError(
                f"docker {' '.join(arguments)} failed: {detail}"
            )
        return result

    def _start_aikit_service(self):
        if self._backend != "aikit":
            return False
        if not (
            self._service_container
            and self._service_host
            and self._service_port
        ):
            raise RuntimeError("AIKit service profile is incomplete")
        with self._service_lock:
            self._docker_command("inspect", self._service_container)
            running = self._docker_command(
                "inspect", "--format", "{{.State.Running}}",
                self._service_container,
            ).stdout.strip().lower() == "true"
            if running:
                # The device calibration and volatile /dev/video nodes are
                # selected immediately before this call.  Restart a lingering
                # service so it cannot keep the previous device configuration.
                self._log(
                    "[AIKIT_SERVICE] Reloading current device configuration"
                )
                stopped = self._docker_command(
                    "stop", "--time", "5", self._service_container,
                    check=False,
                )
                if stopped.returncode != 0:
                    raise RuntimeError(
                        "AIKit service could not reload configuration: "
                        + stopped.stdout.strip()
                    )
            self._log(
                "[AIKIT_SERVICE] Starting container="
                + self._service_container
            )
            self._docker_command("start", self._service_container)
            self._service_active = True
        deadline = self._clock() + 20.0
        while not self._stop_event.is_set() and self._clock() < deadline:
            try:
                with socket.create_connection(
                    (self._service_host, self._service_port), timeout=0.5
                ):
                    self._log(
                        "[AIKIT_SERVICE] Ready address="
                        f"{self._service_host}:{self._service_port}"
                    )
                    return True
            except OSError:
                self._sleep(0.2)
        logs = self._docker_command(
            "logs", "--tail", "40", self._service_container,
            check=False,
        ).stdout.strip()
        raise RuntimeError(
            "AIKit service did not become ready: " + logs[-2000:]
        )

    def _stop_aikit_service(self):
        with self._service_lock:
            if not self._service_active or not self._service_container:
                return
            self._service_active = False
            result = self._docker_command(
                "stop", "--time", "5", self._service_container,
                check=False,
            )
        if result.returncode != 0:
            self._log(
                "[AIKIT_SERVICE] Stop failed: " + result.stdout.strip()
            )
        else:
            self._log("[AIKIT_SERVICE] Stopped")

    def _drain_stdout(
        self, process, line_queue, drain_stop, native_log=None,
    ):
        log_buffer = []
        last_flush = time.monotonic()
        try:
            for line in process.stdout:
                if drain_stop.is_set():
                    break
                arrival_monotonic = self._clock()
                arrival_wall_time = self._wall_clock()
                if native_log is not None:
                    log_buffer.append(json.dumps({
                        "time": arrival_wall_time,
                        "arrival_monotonic": arrival_monotonic,
                        "line": line.rstrip("\n"),
                    }, ensure_ascii=False) + "\n")
                    now = time.monotonic()
                    if (
                        len(log_buffer) >= NATIVE_LOG_FLUSH_LINES
                        or now - last_flush >= NATIVE_LOG_FLUSH_INTERVAL_S
                    ):
                        try:
                            native_log.writelines(log_buffer)
                            native_log.flush()
                        except OSError:
                            pass
                        log_buffer.clear()
                        last_flush = now
                item = (line, arrival_monotonic, arrival_wall_time)
                while not drain_stop.is_set():
                    try:
                        line_queue.put(item, timeout=0.1)
                        break
                    except queue.Full:
                        with self.state._lock:
                            self.state.stdout_backpressure_count += 1
        except Exception:
            pass
        finally:
            if native_log is not None and log_buffer:
                try:
                    native_log.writelines(log_buffer)
                    native_log.flush()
                except OSError:
                    pass

    def _log_native_tail(self):
        """进程意外退出时把原生 stdout 日志最后几行打进应用日志。

        SIGSEGV 类崩溃无回溯可打，最后的输出行是唯一现场；drain 线程
        可能尚未完成最终 flush，稍等再读。
        """
        path = self._native_log_path
        if not path:
            return
        try:
            time.sleep(0.3)
            with open(path, encoding="utf-8", errors="replace") as stream:
                lines = stream.readlines()
            if lines:
                self._log("[SLAM] 原生进程最后输出 ({} 行):".format(
                    len(lines)))
                for line in lines[-15:]:
                    self._log("[SLAM-NATIVE] " + line.rstrip()[:240])
        except OSError as exc:
            self._log("[SLAM] 读取原生日志失败: {}".format(exc))

    def _publish_pose(self, pose):
        # S80M 坐标约定（正对=+Y、Z=上、X=右）：见
        # protocol.rotate_pose_z90（真机实测定案，位置与姿态统一
        # 纯左乘 R_z(+90°)，新 ORB 核心后 native=X=正对 Y=左 Z=上）。
        # 此处是位姿唯一发布入口，UI/落盘/pose.json/回填缓冲全部统一旋转。
        pose = rotate_pose_z90(pose)
        # 原点后姿态相对化：C++ 设原点只把位置归零，静止首帧的姿态
        # 仍带部署校正/启动朝向的常数旋转（旋转跳变的根源，两轮
        # 变换形式修正都没能抵消它）。改为捕获首帧显示姿态为基准，
        # 全部姿态左乘基准的逆 → 首帧姿态恒为单位矩阵（帧轴与世界
        # 轴对齐，不跳变），此后姿态 = 相对原点的真实转动，与轨迹
        # （位置相对原点）同基准。位置不动，保持已验证的轨迹映射。
        if self._pose_rotation_zero_pending:
            self._pose_rotation_zero = pose.rotation
            self._pose_rotation_zero_pending = False
        if self._pose_rotation_zero is not None:
            pose = replace(
                pose,
                rotation=quat_product(
                    quat_conjugate(self._pose_rotation_zero),
                    pose.rotation,
                ),
            )
        self._on_pose(pose)
        timestamp = pose.timestamp
        x, y, z = pose.position
        qx, qy, qz, qw = pose.rotation
        with self._pose_buffer_lock:
            self._pose_buffer[timestamp] = (
                x, y, z, qx, qy, qz, qw
            )
            cutoff = timestamp - 2.0
            for key in [
                key for key in self._pose_buffer if key < cutoff
            ]:
                del self._pose_buffer[key]
        if not self._pose_tmp_path or not self._pose_path:
            return
        try:
            encoded = json.dumps({
                "ts": timestamp,
                "x": x,
                "y": y,
                "z": z,
                "qx": qx,
                "qy": qy,
                "qz": qz,
                "qw": qw,
            })
            with open(self._pose_tmp_path, "w", encoding="utf-8") as stream:
                stream.write(encoded)
            os.replace(self._pose_tmp_path, self._pose_path)
        except OSError:
            pass

    def send_origin(self, reason="origin reset"):
        """Write the bridge's newline protocol; never use SIGUSR1."""
        with self.state._lock:
            process = self.state.process
        if (
            process is None
            or process.poll() is not None
            or process.stdin is None
        ):
            return False
        pid = process.pid
        if reason == "initial origin" and pid in self._origin_sent_pids:
            return True
        try:
            process.stdin.write("\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as error:
            self._log(f"[SLAM] Failed to send {reason}: {error}")
            return False
        if reason == "initial origin":
            self._origin_sent_pids.add(pid)
            self._initial_origin_generation = self._generation
            self._log("[SLAM] READY; automatic origin command sent")
            callback = self._initial_origin_callback
            if callback is not None:
                callback(self._generation)
        else:
            self._log("[SLAM] Origin reset command sent")
        return True

    def _handle_event(
        self, event, counters, arrival_monotonic=None,
        arrival_wall_time=None,
    ):
        if event.kind == "fays_rates":
            self._on_fays_rates(event.payload)
            return
        if event.kind == "orb_diagnostic":
            self._on_orb_diagnostic(event.payload)
            return
        if event.kind == "mp_cleanup":
            self._on_mp_cleanup(event.payload)
            return
        # 周期诊断行（ORB_STAGE/FAYS-AFFINITY 每秒各一条）只在
        # slam_stdout.log 全量留档，不打 GUI 日志避免刷屏；
        # 崩溃取证由 _log_native_tail 读回。
        if event.kind in ("orb_stage", "affinity"):
            return
        if event.kind == "ready":
            self._set_state(status="READY - Press ENTER")
            if self._auto_origin:
                self.send_origin("initial origin")
            else:
                self._log(f"[SLAM] {event.raw}")
            return
        if event.kind == "origin":
            self._last_accepted_pose = None
            self._pose_rejecting = False
            # 位置在原点帧被 C++ 归零，姿态却带着部署校正/启动朝向的
            # 常数残留（上次跳变的根源）；每个 origin 事件（含 NEW
            # ORIGIN）都重新捕获首帧姿态基准，与位置重归零保持一致。
            self._pose_rotation_zero = None
            self._pose_rotation_zero_pending = True
            self._set_state(status="Tracking")
            self._log(f"[SLAM] {event.raw}")
            return
        if event.kind == "status":
            self._log(f"[SLAM] {event.raw}")
            return
        if event.kind == "pose":
            pose = event.payload
            counters["raw"] += 1
            self._pose_raw_seq += 1
            pose = replace(
                pose,
                sequence=self._pose_raw_seq,
                arrival_monotonic=arrival_monotonic,
                arrival_wall_time=arrival_wall_time,
            )
            rejection = pose_rejection_reason(
                pose, self._last_accepted_pose,
            )
            if rejection is not None:
                now = self._wall_clock()
                if (
                    not self._pose_rejecting
                    or now - self._last_pose_reject_log >= 1.0
                ):
                    self._log(f"[SLAM] Pose held: {rejection}")
                    self._last_pose_reject_log = now
                if not self._pose_rejecting:
                    self._set_state(status="Tracking (pose held)")
                self._pose_rejecting = True
                return
            if self._pose_rejecting:
                self._log("[SLAM] Pose continuity recovered")
            self._pose_rejecting = False
            self._last_accepted_pose = pose
            counters["valid"] += 1
            self._publish_pose(pose)
            with self._latest_pose_lock:
                if self._last_accepted_seq:
                    gap = self._pose_raw_seq - self._last_accepted_seq - 1
                    if gap > 0:
                        self._pose_lost_count += gap
                self._last_accepted_seq = self._pose_raw_seq
                self._latest_pose = pose
                lost_count = self._pose_lost_count
            self._set_state(pose_lost_count=lost_count)
            self._set_state(status="Tracking")
            return
        if event.kind == "error":
            self._log(f"[SLAM] ⚠ {event.raw}")
            self._set_state(
                status=f"Error: {event.raw[:60]}",
                error=event.raw,
            )
            return
        self._unmatched_lines += 1
        if self._unmatched_lines <= 50:
            self._log(f"[SLAM] {event.raw}")
        elif self._unmatched_lines == 51:
            self._log("[SLAM] (suppressing further unmatched lines)")

    def _run(self, generation):
        sdk_guard = None
        sdk_device_guard = None
        sdk_deadline = None
        process = None
        drain_thread = None
        drain_stop = threading.Event()
        line_queue = queue.Queue(maxsize=STDOUT_QUEUE_CAPACITY)
        native_log = None
        counters = {"raw": 0, "valid": 0}
        fps_started = self._wall_clock()
        service_started = self._backend == "aikit"
        try:
            if service_started:
                self._start_aikit_service()
            launch_command, environment, launch_cpus = self._build_launch()
            role_text = (
                f"input={(self.cpu_roles.get('fays_input') or (4,))[0]}, "
                f"prep={(self.cpu_roles.get('fays_prepare') or (5,))[0]}, "
                f"ORB={self.cpu_roles['fays_left_orb'][0]}/"
                f"{self.cpu_roles['fays_right_orb'][0]}, "
                f"track/output="
                f"{(self.cpu_roles.get('fays_track') or (8,))[0]}, "
                f"background={self.cpu_roles['fays_background'][0]}, "
                f"general={','.join(str(cpu) for cpu in (self.cpu_roles.get('general') or (10, 11)))}, "
                f"nice=+{self._nice_adjustment}"
            )
            self._log(
                f"[SLAM] Starting (Fays {role_text}): "
                + " ".join(launch_command)
            )
            if self._backend == "orb":
                sdk_guard = device_access_guard(
                    FAYS_SDK_INITIALIZATION_LOCK, timeout=45,
                    cancelled=self._stop_event.is_set)
                sdk_guard.__enter__()
                sdk_device_guard = self._sdk_device_guard_factory(self.device_config)
                sdk_device_guard.__enter__()
                sdk_deadline = time.monotonic() + 30.0
            process = self._process_factory(
                launch_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                text=True,
                cwd=self.work_dir,
                env=environment,
            )
            if self._native_log_path:
                try:
                    os.makedirs(
                        os.path.dirname(
                            os.path.abspath(self._native_log_path)
                        ),
                        exist_ok=True,
                    )
                    native_log = open(
                        self._native_log_path,
                        "a",
                        encoding="utf-8",
                    )
                    native_log.write(json.dumps({
                        "time": time.time(),
                        "event": "native_process_started",
                        "pid": int(process.pid),
                        "device_config": self.device_config,
                    }, ensure_ascii=False) + "\n")
                    native_log.flush()
                except OSError:
                    native_log = None
            self._set_affinity(process.pid, set(launch_cpus))
            if set(self._get_affinity(process.pid)) != set(launch_cpus):
                raise RuntimeError(
                    "Fays child did not accept the full Fays CPU boundary: "
                    f"{','.join(str(cpu) for cpu in launch_cpus)}"
                )
            with self.state._lock:
                if (
                    self._stop_event.is_set()
                    or generation != self._generation
                ):
                    raise RuntimeError("stale SLAM generation")
                self.state.process = process
                self.state.status = "Waiting IMU init..."
            self._on_status("Waiting IMU init...")
            self._started_event.set()
            drain_thread = self._thread_factory(
                target=self._drain_stdout,
                args=(process, line_queue, drain_stop, native_log),
                name="slam-stdout-drain",
                daemon=True,
            )
            drain_thread.start()

            while (
                not self._stop_event.is_set()
                and generation == self._generation
            ):
                if sdk_guard is not None and time.monotonic() > sdk_deadline:
                    raise RuntimeError("Fays SDK 初始化超时，停止本夹爪连接")
                try:
                    item = line_queue.get(timeout=0.5)
                except queue.Empty:
                    if process.poll() is not None:
                        if (not self._stop_event.is_set()
                                and generation == self._generation):
                            error = f"SLAM subprocess exited unexpectedly (code={process.returncode})"
                            self._log(f"[SLAM] {error}")
                            self._log_native_tail()
                            self._set_state(status=f"Error: {error}", error=error,
                                            running=False)
                        break
                    continue
                if isinstance(item, tuple):
                    line, arrival_monotonic, arrival_wall_time = item
                else:
                    line = item
                    arrival_monotonic = None
                    arrival_wall_time = None
                if any(marker in line for marker in (
                    "SetupImu(): VIDIOC_S_FMT failed", "ImuInit(): setup imu failed")):
                    raise RuntimeError("Fays IMU 初始化失败，停止本夹爪连接: " + line.strip())
                if sdk_guard is not None and "[FAYS-CALIB] orb_runtime_yaml=" in line:
                    # SDK construction AND calibration/YAML export completed.
                    sdk_guard.__exit__(None, None, None)
                    sdk_guard = None
                    self._sdk_ready_event.set()
                    self._sdk_initialization_done.set()
                event = parse_slam_line(line)
                if event is not None:
                    self._handle_event(
                        event,
                        counters,
                        arrival_monotonic,
                        arrival_wall_time,
                    )
                now = self._wall_clock()
                elapsed = now - fps_started
                if elapsed >= 1.0:
                    self._set_state(
                        raw_pose_fps=counters["raw"] / elapsed,
                        valid_pose_fps=counters["valid"] / elapsed,
                    )
                    counters = {"raw": 0, "valid": 0}
                    fps_started = now
        except Exception as error:
            self._log(f"[SLAM] Failed to start/run: {error}")
            self._set_state(status=f"Error: {error}", error=str(error))
            traceback.print_exc()
        finally:
            self._started_event.set()
            drain_stop.set()
            if (
                drain_thread is not None
                and drain_thread.is_alive()
                and drain_thread is not threading.current_thread()
            ):
                drain_thread.join(timeout=STDOUT_JOIN_TIMEOUT_S)
            self._terminate_process(process)
            if sdk_device_guard is not None:
                sdk_device_guard.__exit__(None, None, None)
                sdk_device_guard = None
            if sdk_guard is not None:
                sdk_guard.__exit__(None, None, None)
                sdk_guard = None
            if service_started:
                try:
                    self._stop_aikit_service()
                except Exception as error:
                    self._log(f"[AIKIT_SERVICE] Stop failed: {error}")
            if native_log is not None:
                try:
                    native_log.write(json.dumps({
                        "time": time.time(),
                        "event": "native_process_stopped",
                        "returncode": (
                            None if process is None else process.returncode
                        ),
                    }, ensure_ascii=False) + "\n")
                    native_log.close()
                except OSError:
                    pass
            if process is not None and self._on_process_stop is not None:
                try:
                    self._on_process_stop(int(process.pid))
                except Exception:
                    pass
            with self.state._lock:
                if self.state.process is process:
                    self.state.process = None
                self.state.running = False
                if not str(self.state.status).startswith("Error:"):
                    self.state.status = "Stopped"
            self._on_pose(None)
            self._on_status(self.state.snapshot().status)
            self._sdk_initialization_done.set()
            self._log("[SLAM] Worker stopped.")

    def _monitor_frames(self, generation):
        last_frame = -1
        last_change = self._clock()
        last_report = 0.0
        stalled = False
        while (
            not self._stop_event.wait(MONITOR_INTERVAL_S)
            and generation == self._generation
        ):
            snapshot = self._frame_snapshot()
            frame_id = getattr(snapshot, "frame_id", -1)
            now = self._clock()
            if frame_id >= 0 and frame_id != last_frame:
                last_frame = frame_id
                last_change = now
                self._set_state(last_frame_id=frame_id, stalled=False)
                if stalled:
                    self._log("[SLAM] Fays video stream recovered")
                    stalled = False
            status = self.state.snapshot().status
            if frame_id >= 0 and now - last_report >= RUNNING_REPORT_INTERVAL_S:
                self._log(
                    f"[SLAM] Running: frame={frame_id}, state={status}"
                )
                last_report = now
            if (
                frame_id >= 0
                and now - last_change >= STALL_TIMEOUT_S
                and not stalled
            ):
                stalled = True
                self._set_state(stalled=True)
                self._log(
                    "[SLAM] ERROR: Fays video stream has stalled for 5 seconds"
                )

    def _terminate_process(self, process):
        if process is None or process.poll() is not None:
            return
        with self._process_stop_lock:
            if process.poll() is not None:
                return
            try:
                process.terminate()
                process.wait(timeout=PROCESS_WAIT_TIMEOUT_S)
                return
            except Exception:
                pass
            try:
                process.kill()
                process.wait(timeout=FINAL_WAIT_TIMEOUT_S)
            except Exception:
                pass

    def stop(self, join=True):
        """Stop monitor/stdout/process; safe before or after partial start."""
        with self._lifecycle_lock:
            self._generation += 1
            self._stop_event.set()
            with self.state._lock:
                self.state.running = False
                process = self.state.process
                self.state.process = None
            worker = self._worker_thread
            monitor = self._monitor_thread
            self._worker_thread = None
            self._monitor_thread = None
        self._terminate_process(process)
        if self._backend == "aikit":
            try:
                self._stop_aikit_service()
            except Exception as error:
                self._log(f"[AIKIT_SERVICE] Stop failed: {error}")
        if join:
            for thread in (worker, monitor):
                if (
                    thread is not None
                    and thread.is_alive()
                    and thread is not threading.current_thread()
                ):
                    thread.join(timeout=WORKER_JOIN_TIMEOUT_S)
        self.clear_pose_buffer()
        self._on_pose(None)
        self._set_state(
            status="Disconnected",
            raw_pose_fps=0.0,
            valid_pose_fps=0.0,
            last_frame_id=-1,
            stalled=False,
            stdout_backpressure_count=0,
            pose_lost_count=0,
        )
        process_stopped = process is None or process.poll() is not None
        return process_stopped and all(
            thread is None or not thread.is_alive()
            for thread in (worker, monitor)
        )
