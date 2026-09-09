"""带 generation 的单一串口命令调度线程。"""

from __future__ import annotations

from dataclasses import dataclass
import queue
import threading
from typing import Callable, Optional


class SerialCommandCancelled(RuntimeError):
    """命令因断开、清队列、停止或 latest 替换而失效。"""


@dataclass
class _Completion:
    event: threading.Event
    response: Optional[str] = None
    error: Optional[BaseException] = None

    def finish(
        self,
        response: Optional[str] = None,
        error: Optional[BaseException] = None,
    ) -> None:
        if self.event.is_set():
            return
        self.response = response
        self.error = error
        self.event.set()


@dataclass
class _Command:
    command: str
    callback: Optional[Callable[[str], None]]
    key: Optional[str]
    mode: str
    generation: int
    completion: Optional[_Completion] = None


class SerialCommandWorker:
    """顺序执行全部同步/异步应用命令。

    ``queue`` 保留顺序；同 key 的 ``skip`` 忽略重复提交；``latest`` 在当前命令
    尚未结束时只保留最后一次值。``clear`` 和重连会推进 generation，旧命令不能
    再发布回调。
    """

    STOP_TIMEOUT = 1.0
    VALID_MODES = frozenset({"queue", "skip", "latest"})

    def __init__(
        self,
        send_command: Callable[[str], str],
        dispatch_callback: Optional[
            Callable[[Callable[[str], None], str], None]
        ] = None,
        *,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ):
        self._send_command = send_command
        self._dispatch_callback = (
            dispatch_callback
            if dispatch_callback is not None
            else lambda callback, response: callback(response)
        )
        self._thread_factory = thread_factory
        self._queue = queue.Queue()
        self._state_lock = threading.Lock()
        self._pending_keys = set()
        self._latest_by_key = {}
        self._generation = 0
        self._running = False
        self._thread = None
        self._active = None

    @property
    def is_running(self) -> bool:
        with self._state_lock:
            return self._running

    @property
    def generation(self) -> int:
        with self._state_lock:
            return self._generation

    @property
    def thread(self):
        return self._thread

    def start(self) -> bool:
        """启动唯一 worker；重复调用不创建第二条串口线程。"""
        with self._state_lock:
            if self._running:
                return False
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("previous serial worker is still exiting")
            self._running = True
            thread = self._thread_factory(
                target=self._run,
                daemon=True,
                name="serial-command-worker",
            )
            self._thread = thread
        thread.start()
        return True

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            with self._state_lock:
                self._active = item
            try:
                response = self._send_command(item.command)
            except BaseException as exc:  # worker 必须继续服务后续命令
                response = "ERR"
                send_error = exc
            else:
                send_error = None

            next_item = None
            with self._state_lock:
                current = (
                    self._running
                    and item.generation == self._generation
                )
                latest = None
                if item.key:
                    if item.generation == self._generation and self._running:
                        latest = self._latest_by_key.pop(
                            item.key, None)
                        next_item = latest
                        if latest is None:
                            self._pending_keys.discard(item.key)
                    else:
                        self._pending_keys.discard(item.key)
                        stale = self._latest_by_key.pop(item.key, None)
                        self._cancel_item(
                            stale, "serial generation advanced")
                has_newer = (
                    item.key is not None
                    and item.mode == "latest"
                    and latest is not None
                )
                self._active = None
            if next_item is not None:
                self._queue.put(next_item)

            if item.completion is not None:
                if send_error is not None:
                    item.completion.finish(error=send_error)
                elif not current or has_newer:
                    item.completion.finish(error=SerialCommandCancelled(
                        "serial command became stale"))
                else:
                    item.completion.finish(response=response)

            if current and item.callback and not has_newer:
                try:
                    self._dispatch_callback(item.callback, response)
                except Exception:
                    # UI dispatcher 的生命周期独立于串口 worker；窗口已销毁时
                    # 不能让唯一命令线程随之退出。
                    pass

        with self._state_lock:
            self._active = None
            self._running = False

    @staticmethod
    def _cancel_item(item, reason: str) -> None:
        if item is not None and item.completion is not None:
            item.completion.finish(
                error=SerialCommandCancelled(reason))

    def _invalidate_locked(self, reason: str) -> None:
        self._generation += 1
        self._pending_keys.clear()
        latest = tuple(self._latest_by_key.values())
        self._latest_by_key.clear()
        for item in latest:
            self._cancel_item(item, reason)

    def clear(self) -> int:
        """推进 generation，并丢弃断开前尚未执行的命令。"""
        with self._state_lock:
            self._invalidate_locked("serial queue cleared")
            generation = self._generation
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                # 不得吞掉 stop 已发布的唯一唤醒哨兵。
                self._queue.put(None)
                break
            self._cancel_item(item, "serial queue cleared")
        return generation

    def advance_generation(self) -> int:
        """成功重连后建立新的回调 generation。"""
        with self._state_lock:
            self._invalidate_locked("serial connection generation advanced")
            return self._generation

    def submit(
        self,
        command: str,
        callback: Optional[Callable[[str], None]] = None,
        key: Optional[str] = None,
        mode: str = "queue",
        *,
        _completion: Optional[_Completion] = None,
    ) -> bool:
        """提交命令；返回 ``False`` 表示被同 key 的 ``skip`` 合并。"""
        if mode not in self.VALID_MODES:
            raise ValueError(f"unsupported serial command mode: {mode}")
        with self._state_lock:
            if not self._running:
                raise RuntimeError("serial command worker is not running")
            item = _Command(
                command=str(command),
                callback=callback,
                key=key,
                mode=mode,
                generation=self._generation,
                completion=_completion,
            )
            if key:
                if key in self._pending_keys:
                    if mode == "latest":
                        previous = self._latest_by_key.get(key)
                        self._latest_by_key[key] = item
                        self._cancel_item(
                            previous, "serial latest command superseded")
                        return True
                    self._cancel_item(
                        item, "duplicate serial command skipped")
                    return False
                self._pending_keys.add(key)
        self._queue.put(item)
        return True

    def execute(
        self,
        command: str,
        *,
        timeout: Optional[float] = None,
        key: Optional[str] = None,
        mode: str = "queue",
    ) -> str:
        """通过同一 worker 同步执行命令。"""
        completion = _Completion(threading.Event())
        accepted = self.submit(
            command, key=key, mode=mode, _completion=completion)
        if not accepted:
            raise SerialCommandCancelled(
                "duplicate serial command skipped")
        if not completion.event.wait(timeout=timeout):
            raise TimeoutError(f"serial command timed out: {command}")
        if completion.error is not None:
            raise completion.error
        return completion.response or "ERR"

    # ``request`` 是语义更明确的同步别名。
    request = execute

    def stop(self) -> bool:
        """停止 worker，并保持原实现的 1 秒 join 上限。"""
        with self._state_lock:
            if not self._running and (
                    self._thread is None or not self._thread.is_alive()):
                return True
            self._running = False
            self._invalidate_locked("serial worker stopped")
            self._cancel_item(self._active, "serial worker stopped")
            thread = self._thread
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                self._cancel_item(item, "serial worker stopped")
        self._queue.put(None)
        if (thread is not None and thread.is_alive()
                and thread is not threading.current_thread()):
            thread.join(timeout=self.STOP_TIMEOUT)
        return not (thread is not None and thread.is_alive())
