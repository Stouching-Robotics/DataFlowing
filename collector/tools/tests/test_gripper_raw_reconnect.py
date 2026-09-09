"""FaysRawStreamClient 断线自动重连编排单测（离线，无硬件）。

直接驱动 _run 的重连状态机，_connect/_stream_loop 用脚本化假实现：
- A: 首连成功 → 中途服务端断开 → 1s 后重连成功 → on_reconnected 触发
     → 流继续，无 error
- B: 中途断开后重连持续失败 → 累计超时 → 致命 error（on_error 触发）
- C: 首连失败 → 立即致命，不进入重连
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import core.gripper.recording.fays_raw_client as raw_mod
from core.gripper.recording.fays_raw_client import (
    FaysRawStreamClient, FaysRawStreamError)


class FakeSock:
    def close(self):
        pass

    def shutdown(self, how):
        pass


class ScriptedClient(FaysRawStreamClient):
    """_connect 按脚本返回/抛错；_stream_loop 按脚本抛断或等 stop。"""

    def __init__(self, connect_script, stream_script):
        super().__init__(
            "/tmp/fake_raw.sock", on_stereo=lambda *a: None,
            logger=lambda m: None)
        self._connect_script = list(connect_script)  # sock | FaysRawStreamError
        self._stream_script = list(stream_script)    # FaysRawStreamError | None(等stop)
        self.connect_calls = 0
        self.stream_calls = 0

    def _connect(self, deadline_s=30.0):
        self.connect_calls += 1
        item = self._connect_script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def _stream_loop(self, sock):
        self.stream_calls += 1
        item = self._stream_script.pop(0)
        if isinstance(item, Exception):
            raise item
        self._stop.wait(10)
        return None


def run_scenario(connect_script, stream_script, stop_after=2.0):
    client = ScriptedClient(connect_script, stream_script)
    errors = []
    reconnects = []
    client._on_error = errors.append
    client._on_reconnected = lambda: reconnects.append(1)
    client.start()
    time.sleep(stop_after)
    client.stop()
    return client, errors, reconnects


def scenario_a_first_connect_ok_then_reconnect():
    sock1, sock2 = FakeSock(), FakeSock()
    client, errors, reconnects = run_scenario(
        connect_script=[sock1, sock2],
        stream_script=[FaysRawStreamError("closed unexpectedly"), None],
    )
    ok = (
        not errors
        and len(reconnects) == 1
        and client._reconnect_count == 1
        and client.connect_calls == 2
        and client.stream_calls == 2
        and client.connected.is_set()
    )
    print(("  PASS" if ok else "  FAIL"),
          "A: 中途断开→重连成功→on_reconnected 触发，无 error",
          f"(connect={client.connect_calls} stream={client.stream_calls} "
          f"reconnects={len(reconnects)})")
    return ok


def scenario_b_reconnect_timeout_fatal():
    client, errors, reconnects = run_scenario(
        connect_script=[FakeSock()] + [FaysRawStreamError("not ready")] * 50,
        stream_script=[FaysRawStreamError("closed unexpectedly")],
    )
    ok = (
        len(errors) == 1
        and "reconnect failed" in errors[0]
        and not reconnects
        and client._reconnect_count == 1
        and client.connect_calls >= 2
    )
    print(("  PASS" if ok else "  FAIL"),
          "B: 重连持续失败→累计超时→致命 error（on_error 触发一次）",
          f"(error={errors[:1]})")
    return ok


def scenario_c_first_connect_fatal():
    client, errors, reconnects = run_scenario(
        connect_script=[FaysRawStreamError("raw socket not ready")],
        stream_script=[],
        stop_after=0.8,
    )
    ok = (
        len(errors) == 1
        and not reconnects
        and client._reconnect_count == 0
        and client.connect_calls == 1
        and client.stream_calls == 0
    )
    print(("  PASS" if ok else "  FAIL"),
          "C: 首连失败→立即致命，不进入重连循环")
    return ok


def main():
    # 缩短重连节奏，测试秒级跑完
    raw_mod.RECONNECT_ATTEMPT_INTERVAL_S = 0.05
    raw_mod.RECONNECT_TOTAL_TIMEOUT_S = 0.6
    results = [
        scenario_a_first_connect_ok_then_reconnect(),
        scenario_b_reconnect_timeout_fatal(),
        scenario_c_first_connect_fatal(),
    ]
    if all(results):
        print("\nPASS: FaysRawStreamClient 重连编排测试全部通过")
        return 0
    print("\nFAIL: 存在未通过场景")
    return 1


if __name__ == "__main__":
    sys.exit(main())
