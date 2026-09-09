"""夹爪控制板连接、响应解析和三组定时循环。"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from types import MappingProxyType
from typing import Callable, Mapping, Optional, Protocol

from .gripper_serial import GripperSerial
from .serial_command_worker import SerialCommandWorker


UI_LOOP_MS = 33
SLOW_LOOP_MS = 150
FAST_GRIP_LOOP_MS = 10
RESET_EVENT_DEBOUNCE_S = 3.0
# ESP32 declares the host offline after 1.5 s without a signal (HEARTBEAT
# in main.cpp).  The host-driven "?" poll runs on its own daemon thread so a
# stalled Tk main thread (e.g. a long synchronous save) cannot starve it and
# disconnect the physical record button.
HEARTBEAT_PERIOD_S = 1.0 / 30.0


class SensorLifecycle(Protocol):
    """Connection controller 唯一允许调用的跨设备生命周期接口。"""

    def prepare_connect(self) -> object:
        """只读预检并完成 Open 资源交接；失败应抛异常或返回 ``False``。"""

    def connected(self) -> object:
        """串口握手后启动 Fays/SLAM，并重新初始化左右 Sightac。"""

    def connect_failed(self) -> object:
        """收回部分 Connect 资源，并按需恢复发起前的 Open 状态。"""

    def verify_prepared_control_port(self, device_path: str) -> object:
        """确认交接后串口节点仍指向预检时的同一 ESP32。"""

    def disconnect(self) -> object:
        """按依赖逆序停止 SLAM、Sightac 和预览资源。"""


@dataclass(frozen=True)
class ConnectionSnapshot:
    """供 UI、录制和诊断读取的不可变连接快照。"""

    serial_generation: int
    connecting: bool
    connected: bool
    no_esp: bool
    loop_token: int
    connect_time: float
    board_fields: Mapping[str, str]
    actual: str
    as5600: Mapping[str, str]
    raw_state: str
    raw_actual: str
    raw_as5600: str
    updated: str
    error: Optional[str]


class ConnectionState:
    """连接状态的唯一可变源；只由 GripperConnectionController 写入。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.serial_generation = 0
        self.connecting = False
        self.connected = False
        self.no_esp = False
        self.loop_token = 0
        self.connect_time = 0.0
        self.attempt_generation = 0
        self.error = None
        self.board_state = {
            "state": {},
            "actual": "--",
            "as5600": {},
            "raw_state": "--",
            "raw_actual": "--",
            "raw_as5600": "--",
            "updated": "--",
        }

    def snapshot(self) -> ConnectionSnapshot:
        with self.lock:
            fields = MappingProxyType(dict(self.board_state["state"]))
            return ConnectionSnapshot(
                serial_generation=int(self.serial_generation),
                connecting=bool(self.connecting),
                connected=bool(self.connected),
                no_esp=bool(self.no_esp),
                loop_token=int(self.loop_token),
                connect_time=float(self.connect_time),
                board_fields=fields,
                actual=str(self.board_state["actual"]),
                as5600=MappingProxyType(
                    dict(self.board_state["as5600"])),
                raw_state=str(self.board_state["raw_state"]),
                raw_actual=str(self.board_state["raw_actual"]),
                raw_as5600=str(self.board_state["raw_as5600"]),
                updated=str(self.board_state["updated"]),
                error=None if self.error is None else str(self.error),
            )


class GripperConnectionController:
    """协调串口连接，且仅通过注入的 ``lifecycle`` 管理其他设备。

    本类不持有 App、Tk widget、相机或 SLAM 句柄。UI 调度、定时器和业务事件都
    通过回调注入。
    """

    UI_LOOP_MS = UI_LOOP_MS
    SLOW_LOOP_MS = SLOW_LOOP_MS
    FAST_GRIP_LOOP_MS = FAST_GRIP_LOOP_MS

    def __init__(
        self,
        state: ConnectionState,
        serial_owner: GripperSerial,
        serial_commands: SerialCommandWorker,
        lifecycle: SensorLifecycle,
        *,
        dispatch: Optional[Callable[..., None]] = None,
        schedule: Optional[Callable[[int, Callable[[], None]], object]] = None,
        on_state: Optional[Callable[[ConnectionSnapshot], None]] = None,
        on_board_update: Optional[Callable[[ConnectionSnapshot], None]] = None,
        on_ui_tick: Optional[Callable[[], None]] = None,
        on_grip_check: Optional[Callable[[], None]] = None,
        on_reset_event: Optional[Callable[[], None]] = None,
        on_stop_event: Optional[Callable[[], None]] = None,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        logger: Callable[..., None] = print,
    ):
        if lifecycle is None:
            raise ValueError("an injected sensor lifecycle is required")
        self.state = state
        self.serial = serial_owner
        self.serial_commands = serial_commands
        self.lifecycle = lifecycle
        self.dispatch = dispatch or (
            lambda callback, *args: callback(*args))
        self.schedule = schedule or (
            lambda _delay_ms, callback: None)
        self.on_state = on_state
        self.on_board_update = on_board_update
        self.on_ui_tick = on_ui_tick
        self.on_grip_check = on_grip_check
        self.on_reset_event = on_reset_event
        self.on_stop_event = on_stop_event
        self._thread_factory = thread_factory
        self._clock = clock
        self._wall_clock = wall_clock
        self._logger = logger
        self._transition_lock = threading.RLock()
        self._connect_thread = None
        self._heartbeat_stop = None
        self._heartbeat_thread = None
        self._grip_stop = None
        self._grip_thread = None

    def snapshot(self) -> ConnectionSnapshot:
        return self.state.snapshot()

    def start(self) -> bool:
        return self.serial_commands.start()

    def clear_serial_queue(self) -> int:
        return self.serial_commands.clear()

    def stop_serial_worker(self) -> bool:
        return self.serial_commands.stop()

    def invalidate_loops(self) -> None:
        with self.state.lock:
            self.state.loop_token += 1

    def refresh_ports(self):
        return self.serial.list_ports()

    def _publish_state(self) -> None:
        if self.on_state is not None:
            snapshot = self.snapshot()
            try:
                self.dispatch(self.on_state, snapshot)
            except Exception:
                pass

    def _lifecycle_call(self, method_name: str, **kwargs):
        method = getattr(self.lifecycle, method_name, None)
        if not callable(method):
            raise RuntimeError(
                f"sensor lifecycle has no {method_name}() method")
        result = method(**kwargs)
        if result is False:
            raise RuntimeError(
                f"sensor lifecycle {method_name} rejected transition")
        return result

    def connect(self, port: str, baud: int = 115200) -> bool:
        """异步执行预检/交接和串口握手。"""
        if not str(port).strip():
            raise ValueError("serial port is required")
        with self.state.lock:
            if self.state.connecting or self.state.connected:
                return False
            self.state.connecting = True
            self.state.error = None
            self.state.attempt_generation += 1
            attempt = self.state.attempt_generation
        self._publish_state()
        thread = self._thread_factory(
            target=self._connect_worker,
            args=(str(port), int(baud), attempt),
            daemon=True,
            name=f"gripper-connect-{attempt}",
        )
        self._connect_thread = thread
        thread.start()
        return True

    def connect_sync(self, port: str, baud: int = 115200) -> bool:
        """测试和无 GUI 调用使用的同步连接入口，顺序与异步入口完全相同。"""
        if not str(port).strip():
            raise ValueError("serial port is required")
        with self.state.lock:
            if self.state.connecting or self.state.connected:
                return False
            self.state.connecting = True
            self.state.error = None
            self.state.attempt_generation += 1
            attempt = self.state.attempt_generation
        self._publish_state()
        ok, error = self._perform_connect(str(port), int(baud), attempt)
        return self._finish_connect(attempt, ok, error)

    def connect_no_esp(self) -> bool:
        """Start Fays/SLAM diagnostic mode without opening the ESP serial."""
        with self.state.lock:
            if self.state.connecting or self.state.connected:
                return False
            self.state.connecting = True
            self.state.no_esp = True
            self.state.error = None
            self.state.attempt_generation += 1
            attempt = self.state.attempt_generation
        self._publish_state()
        thread = self._thread_factory(
            target=self._connect_no_esp_worker,
            args=(attempt,),
            daemon=True,
            name=f"fays-no-esp-connect-{attempt}",
        )
        self._connect_thread = thread
        thread.start()
        return True

    def toggle_no_esp(self) -> bool:
        snapshot = self.snapshot()
        if snapshot.connecting or snapshot.connected:
            return self.disconnect()
        return self.connect_no_esp()

    def connect_no_esp_sync(self) -> bool:
        """Headless/test adapter for the no-ESP diagnostic path."""
        with self.state.lock:
            if self.state.connecting or self.state.connected:
                return False
            self.state.connecting = True
            self.state.no_esp = True
            self.state.error = None
            self.state.attempt_generation += 1
            attempt = self.state.attempt_generation
        self._publish_state()
        ok, error = self._perform_connect_no_esp(attempt)
        return self._finish_connect_no_esp(attempt, ok, error)

    def _connect_worker(self, port: str, baud: int, attempt: int) -> None:
        # ESP handshakes and SDK startup can block for seconds. Keep the full
        # transition off Tk, not just its preflight half.
        with self._transition_lock:
            ok, error = self._perform_connect(port, baud, attempt)
            self._finish_connect(attempt, ok, error)

    def _connect_no_esp_worker(self, attempt: int) -> None:
        with self._transition_lock:
            ok, error = self._perform_connect_no_esp(attempt)
            self._finish_connect_no_esp(attempt, ok, error)

    def _perform_connect(self, port: str, baud: int, attempt: int):
        self._logger(f"[Connect] Preparing Fays/Sightac handoff ...")
        try:
            # 必须先完成 Fays 身份预检和 Open 资源释放，失败时不得打开串口。
            self._lifecycle_call("prepare_connect")
            with self.state.lock:
                if attempt != self.state.attempt_generation:
                    raise RuntimeError("connection attempt invalidated")
            verify = getattr(
                self.lifecycle, "verify_prepared_control_port", None
            )
            if callable(verify) and verify(port) is False:
                raise RuntimeError(
                    "ESP32 control port identity verification failed"
                )
            self._logger(f"[Connect] Trying {port}...")
            ok = self.serial.connect(port, baud)
            self._logger(f"[Connect] Result: {'OK' if ok else 'FAIL'}")
            serial_error = getattr(self.serial, "last_error", None)
            return ok, None if ok else (
                serial_error or "serial handshake timeout"
            )
        except Exception as exc:
            return False, str(exc)

    def _perform_connect_no_esp(self, attempt: int):
        self._logger("[Connect no_esp] Preparing Fays handoff ...")
        try:
            self._lifecycle_call("prepare_connect")
            with self.state.lock:
                if attempt != self.state.attempt_generation:
                    raise RuntimeError("connection attempt invalidated")
            self._lifecycle_call("connected", fake_imu=True)
            return True, None
        except Exception as exc:
            return False, str(exc)

    def _finish_connect_no_esp(
        self, attempt: int, ok: bool, error: Optional[str],
    ) -> bool:
        with self.state.lock:
            current = attempt == self.state.attempt_generation
        if not current:
            return False
        if ok:
            now = self._clock()
            with self.state.lock:
                self.state.connecting = False
                self.state.connected = True
                self.state.no_esp = True
                self.state.loop_token += 1
                self.state.connect_time = now
                self.state.error = None
                token = self.state.loop_token
            self.dispatch(self.update_loops, token)
            self._publish_state()
            self._logger(
                "[Connect no_esp] SLAM started with diagnostic fake IMU")
            return True

        rollback_error = None
        try:
            self._lifecycle_call("connect_failed")
        except Exception as exc:
            rollback_error = str(exc)
        failure = error or "no-ESP diagnostic connection failed"
        if rollback_error:
            failure = f"{failure}; rollback failed: {rollback_error}"
        with self.state.lock:
            self.state.connecting = False
            self.state.connected = False
            self.state.no_esp = False
            self.state.loop_token += 1
            self.state.error = failure
        self._publish_state()
        return False

    def _finish_connect(
        self, attempt: int, ok: bool, error: Optional[str],
    ) -> bool:
        with self.state.lock:
            current = attempt == self.state.attempt_generation
        if not current:
            if ok:
                self.serial.disconnect()
            return False

        if ok and self.serial.is_connected and self.serial_commands.is_running:
            try:
                # 新协议握手：先让 ESP32 进入“已连接/等待初始化”，再通知
                # “初始化开始”，随后由 lifecycle 完成 SDK/SLAM/触觉初始化。
                if not self.send_control_signal("C"):
                    raise RuntimeError(
                        "ESP32 connect signal (C) rejected")
                if not self.send_control_signal("I"):
                    raise RuntimeError(
                        "ESP32 init-start signal (I) rejected")
                self._lifecycle_call("connected")
            except Exception as exc:
                # 初始化失败时通知 ESP32 进入异常状态（红闪）。
                try:
                    self.send_control_signal("E")
                except Exception:
                    pass
                error = str(exc)
                ok = False

        if ok and self.serial.is_connected and self.serial_commands.is_running:
            try:
                # 初始化完成 → ESP32 进入“等待开始录制”（绿常亮）。
                if not self.send_control_signal("F"):
                    raise RuntimeError(
                        "ESP32 init-complete signal (F) rejected")
            except Exception as exc:
                try:
                    self.send_control_signal("E")
                except Exception:
                    pass
                error = str(exc)
                ok = False

        if ok and self.serial.is_connected and self.serial_commands.is_running:
            generation = self.serial_commands.advance_generation()
            now = self._clock()
            with self.state.lock:
                if attempt != self.state.attempt_generation:
                    # A concurrent Cancel has invalidated this transition;
                    # its disconnect worker owns cleanup under _transition_lock.
                    return False
                self.state.connecting = False
                self.state.connected = True
                self.state.no_esp = False
                self.state.serial_generation = generation
                self.state.loop_token += 1
                self.state.connect_time = now
                self.state.error = None
                token = self.state.loop_token
            self._init_fields()
            self.dispatch(self.update_loops, token)
            self._publish_state()
            return True

        rollback_error = None
        try:
            self._lifecycle_call("connect_failed")
        except Exception as exc:
            rollback_error = str(exc)
            self._logger(
                f"[Connect] Open rollback failed: {rollback_error}")
        self.serial.disconnect()
        failure = error or "connection failed"
        if rollback_error:
            failure = (
                f"{failure}; Open rollback failed: {rollback_error}"
            )
        with self.state.lock:
            self.state.connecting = False
            self.state.connected = False
            self.state.no_esp = False
            self.state.loop_token += 1
            self.state.error = failure
        self._publish_state()
        return False

    def disconnect(self) -> bool:
        """停止注入 lifecycle 后断开串口，并使全部旧循环/命令失效。"""
        with self.state.lock:
            was_active = self.state.connecting or self.state.connected
            self.state.attempt_generation += 1
            self.state.loop_token += 1
            self.state.connecting = False
            self.state.connected = False
            self.state.no_esp = False
            self.state.connect_time = 0.0
            token = self.state.loop_token
        generation = self.serial_commands.clear()
        self._stop_heartbeat()
        self._stop_grip_checks()
        with self._transition_lock:
            error = None
            try:
                self._lifecycle_call("disconnect")
            except Exception as exc:
                error = str(exc)
            self.serial.disconnect()
        with self.state.lock:
            self.state.serial_generation = generation
            self.state.loop_token = token
            self.state.error = error
        self._publish_state()
        return was_active and error is None

    def close(self) -> None:
        """幂等停止连接、单 worker 和串口 owner。"""
        self.disconnect()
        self.serial_commands.stop()
        self.serial.disconnect()

    def send_async(
        self,
        command: str,
        callback: Optional[Callable[[str], None]] = None,
        key: Optional[str] = None,
        mode: str = "queue",
    ) -> bool:
        return self.serial_commands.submit(
            command, callback=callback, key=key, mode=mode)

    def send_sync(
        self,
        command: str,
        *,
        timeout: Optional[float] = None,
        key: Optional[str] = None,
        mode: str = "queue",
    ) -> str:
        return self.serial_commands.execute(
            command, timeout=timeout, key=key, mode=mode)

    def send_control_signal(
        self, command: str, *, timeout: float = 1.5,
    ) -> bool:
        """Send one host→ESP32 control signal (C/I/F/E) through the worker.

        Returns ``False`` when the signal is rejected, times out, or the
        worker is not running.  The ESP32 replies ``OK <NAME>`` on success.
        """
        try:
            response = self.serial_commands.execute(
                command, timeout=timeout, mode="queue")
        except Exception:
            return False
        return bool(response) and not response.startswith("ERR")

    def request_as5048a(
        self, callback: Optional[Callable[[Mapping[str, str]], None]] = None,
    ) -> bool:
        """异步读取 AS5048A；不绕过唯一串口 worker。"""
        def handled(response: str) -> None:
            values = self.parse_as5048a(response)
            if callback is not None:
                callback(values)

        return self.send_async(
            "G", handled, key="as5048a", mode="skip")

    def get_as5048a(self, timeout: Optional[float] = None):
        """同步读取 AS5048A，同样经过唯一串口 worker。"""
        return self.parse_as5048a(
            self.send_sync("G", timeout=timeout))

    @staticmethod
    def parse_as5048a(response: str):
        if not response or not response.startswith("AS5048A"):
            return MappingProxyType({})
        return MappingProxyType(dict(
            item.split("=", 1)
            for item in response.split()
            if "=" in item
        ))

    def _init_fields(self) -> None:
        self.send_async("?", self._handle_state_response)

    @staticmethod
    def parse_pairs(line: str):
        values = {}
        for part in str(line).split():
            if "=" in part:
                key, value = part.split("=", 1)
                values[key] = value
        return values

    def _handle_state_response(self, response: str) -> None:
        response = response or "--"
        event = ""
        fields = None
        with self.state.lock:
            board = self.state.board_state
            board["raw_state"] = response
            board["updated"] = time.strftime("%H:%M:%S")
            if response.startswith("STATE "):
                fields = self.parse_pairs(response)
                board["state"] = fields
                event = fields.get("EVT", "")
            elif response == "ERR":
                board["state"] = {}
            connect_time = self.state.connect_time

        if fields is not None:
            if event == "R":
                if self._clock() - connect_time < RESET_EVENT_DEBOUNCE_S:
                    self._logger("[Button] EVT=R ignored (debounce)")
                elif self.on_reset_event is not None:
                    self._logger(
                        "[Button] EVT=R → reset origin + recording")
                    self.on_reset_event()
            elif event == "S" and self.on_stop_event is not None:
                self._logger("[Button] EVT=S → stop recording")
                self.on_stop_event()

        snapshot = self.snapshot()
        if self.on_board_update is not None:
            self.on_board_update(snapshot)

    def _poll_info(self) -> None:
        self.send_async(
            "?", self._handle_state_response,
            key="poll_info", mode="skip",
        )

    def _loop_active(self, token: int) -> bool:
        with self.state.lock:
            return (
                token == self.state.loop_token
                and self.state.connected
                and (self.state.no_esp or self.serial.is_connected)
            )

    def _ui_loop(self, token: int) -> None:
        if not self._loop_active(token):
            return
        if self.on_ui_tick is not None:
            self.on_ui_tick()
        self.schedule(
            self.UI_LOOP_MS, lambda: self._ui_loop(token))

    def _fast_grip_loop(self, token: int) -> None:
        """Run force checks off the Tk main thread at the original period."""
        stop_event = self._grip_stop
        thread = self._grip_thread
        if thread is not None and thread.is_alive():
            stop_event.set()
            if thread is not threading.current_thread():
                thread.join(timeout=0.2)
        self._grip_stop = threading.Event()
        worker = self._thread_factory(
            target=self._grip_check_loop,
            args=(token, self._grip_stop),
            name="force-grip-check",
            daemon=True,
        )
        self._grip_thread = worker
        worker.start()

    def _grip_check_loop(self, token: int, stop_event):
        while not stop_event.wait(self.FAST_GRIP_LOOP_MS / 1000.0):
            if not self._loop_active(token):
                break
            if self.snapshot().no_esp:
                continue
            if self.on_grip_check is not None:
                self.on_grip_check()

    def _stop_grip_checks(self) -> None:
        stop_event = self._grip_stop
        thread = self._grip_thread
        self._grip_stop = None
        self._grip_thread = None
        if stop_event is not None:
            stop_event.set()
        if (
            thread is not None
            and thread is not threading.current_thread()
            and thread.is_alive()
        ):
            thread.join(timeout=0.5)

    def _start_heartbeat(self, token: int) -> None:
        """Start the ESP32 keep-alive "?" poll on its own daemon thread.

        The poll no longer rides ``root.after`` so a stalled Tk main thread
        (e.g. a long synchronous save) cannot let the ESP32 declare the host
        offline and stop accepting the physical record button.
        """
        stop_event = self._heartbeat_stop
        thread = self._heartbeat_thread
        if thread is not None and thread.is_alive():
            if stop_event is not None:
                stop_event.set()
            if thread is not threading.current_thread():
                thread.join(timeout=0.5)
        self._heartbeat_stop = threading.Event()
        heartbeat = self._thread_factory(
            target=self._heartbeat_loop,
            args=(token, self._heartbeat_stop),
            name="esp-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread = heartbeat
        heartbeat.start()

    def _heartbeat_loop(
        self, token: int, stop_event: threading.Event,
    ) -> None:
        while not stop_event.is_set():
            if self._loop_active(token) and not self.snapshot().no_esp:
                self._poll_info()
            stop_event.wait(HEARTBEAT_PERIOD_S)

    def _stop_heartbeat(self) -> None:
        stop_event = self._heartbeat_stop
        thread = self._heartbeat_thread
        self._heartbeat_stop = None
        self._heartbeat_thread = None
        if stop_event is not None:
            stop_event.set()
        if (
            thread is not None
            and thread is not threading.current_thread()
            and thread.is_alive()
        ):
            thread.join(timeout=0.5)

    def update_loops(self, token: Optional[int] = None) -> None:
        if token is None:
            with self.state.lock:
                token = self.state.loop_token
        if self._loop_active(token):
            self._start_heartbeat(token)
            self._ui_loop(token)
            self._fast_grip_loop(token)
