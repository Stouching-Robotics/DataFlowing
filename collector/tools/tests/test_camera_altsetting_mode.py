#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""相机等时档起始档位 —— 离线自检（不碰真相机/真服务/真 USB）。

    venv/bin/python tools/tests/test_camera_altsetting_mode.py

背景：DECXIN RGB 的 alt7（10.24 MB/s）在整机三路一起出流时每 68~116 秒必
停摆一次，alt6（7.552 MB/s）同样三路下 30.00 fps 长跑不停，而两档出帧率一
模一样。所以起始档位取档位表里最保守的那一档，而不是扫描程序报的首选档
——首选档多出来的每帧余量不值一次停摆。

覆盖:
  1. 起始档位 = 档位表最后一档（DECXIN 得 alt6，不是扫描程序报的 alt7）
  2. 只有一个档位的机型（Sightac）原样返回，不硬凑
  3. 服务上一轮学到的档位优先于「最保守档」
  4. 学到的档位不在档位表里（文件被改坏/跨版本）→ 当没学到，退回最保守档
  5. 扫描程序与服务端档位表脱节 → 报错，且不让学到的值把错误盖过去
  6. 落盘键与服务端 C 代码逐字一致（服务写、Python 读，只能靠这条对齐）
  7. 生成的 ini 确实带上生效的 forced_* 三项与档位来源注释

退出码 0 = 全部通过。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from unittest import mock                                       # noqa: E402

from core.gripper.devices import uvc_camera_service            # noqa: E402
from core.gripper.devices.uvc_camera_service import (           # noqa: E402
    UvcCameraServiceError, UvcCameraServiceManager, _mode_state_key,
)

FAILS = []

DECXIN_VID, DECXIN_PID = 0x1BCF, 0x2D4F
SIGHTAC_VID, SIGHTAC_PID = 0x0C45, 0x636F
CONTROLLER = "0000:74:00.4"
SERIAL = "DECXIN-01"


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


def sightac_candidate(port):
    return {
        "vid": SIGHTAC_VID, "pid": SIGHTAC_PID,
        "altsetting": 3, "descriptor_payload": 800,
        "port": port, "streaming_interface": 1,
        "usb_serial": f"SN-{port}",
    }


def decxin_candidate():
    """扫描程序报的是相机自己的首选档（alt7）——这正是不能被照抄的那个值。"""
    return {
        "vid": DECXIN_VID, "pid": DECXIN_PID,
        "altsetting": 7, "descriptor_payload": 1280,
        "port": "2.2.2", "streaming_interface": 1,
        "usb_serial": SERIAL,
    }


def make_records():
    return [{
        "bus": 1,
        "sightac": {"left": sightac_candidate("2.3"),
                    "right": sightac_candidate("2.4")},
        "decxin": decxin_candidate(),
    }]


def manager():
    """绕开 __init__（它要真二进制路径），只借用 _starting_mode/_write_config。"""
    return object.__new__(UvcCameraServiceManager)


def write_state(state_dir, key, altsetting, payload):
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    (Path(state_dir) / f"{key}.mode").write_text(
        f"altsetting={altsetting}\npayload={payload}\n", encoding="utf-8")


def test_conservative_rung_is_the_default():
    start = UvcCameraServiceManager._starting_mode
    mode, source = start(decxin_candidate(), CONTROLLER)
    check(mode == (6, 944), f"DECXIN 起始档位是 alt6/944，实得 {mode}")
    check(source == "conservative", f"来源标成 conservative，实得 {source}")

    mode, source = start(sightac_candidate("2.3"), CONTROLLER)
    check(mode == (3, 800), f"Sightac 单档机型原样返回，实得 {mode}")
    check(source == "preferred", f"单档机型来源是 preferred，实得 {source}")


def test_last_rung_rule_generalises():
    """规则是「最后一档」而不是「DECXIN 的 alt6」：换个三档机型也得成立。"""
    key = (0x1234, 0x5678)
    original = uvc_camera_service._MODE_LADDERS.get(key)
    uvc_camera_service._MODE_LADDERS[key] = ((9, 1500), (8, 1200), (5, 700))
    try:
        candidate = {"vid": 0x1234, "pid": 0x5678,
                     "altsetting": 9, "descriptor_payload": 1500,
                     "port": "1.1", "streaming_interface": 1, "usb_serial": "X"}
        mode, source = UvcCameraServiceManager._starting_mode(
            candidate, CONTROLLER)
        check(mode == (5, 700),
              f"三档机型的起始档位是最后一档 alt5/700，实得 {mode}")
        check(source == "conservative", f"来源是 conservative，实得 {source}")
    finally:
        if original is None:
            uvc_camera_service._MODE_LADDERS.pop(key, None)
        else:
            uvc_camera_service._MODE_LADDERS[key] = original


def test_learned_mode_wins():
    key = (0x1234, 0x5678)
    original = uvc_camera_service._MODE_LADDERS.get(key)
    uvc_camera_service._MODE_LADDERS[key] = ((9, 1500), (8, 1200), (5, 700))
    with tempfile.TemporaryDirectory() as state_dir:
        old_dir = uvc_camera_service.CAMERA_MODE_STATE_DIR
        uvc_camera_service.CAMERA_MODE_STATE_DIR = state_dir
        try:
            candidate = {"vid": 0x1234, "pid": 0x5678,
                         "altsetting": 9, "descriptor_payload": 1500,
                         "port": "1.1", "streaming_interface": 1,
                         "usb_serial": "X"}
            # 服务从 alt9 退到 alt8 后落盘的就是这个
            write_state(state_dir,
                        _mode_state_key(0x1234, 0x5678, CONTROLLER, "X"),
                        8, 1200)
            mode, source = UvcCameraServiceManager._starting_mode(
                candidate, CONTROLLER)
            check(mode == (8, 1200),
                  f"学到的 alt8/1200 盖过最保守档 alt5/700，实得 {mode}")
            check(source == "learned", f"来源是 learned，实得 {source}")

            # 表外的值（跨版本残留）当没学到
            write_state(state_dir,
                        _mode_state_key(0x1234, 0x5678, CONTROLLER, "X"),
                        4, 999)
            mode, source = UvcCameraServiceManager._starting_mode(
                candidate, CONTROLLER)
            check(mode == (5, 700),
                  f"表外的学到值被忽略，退回最保守档，实得 {mode}")
            check(source == "conservative", f"来源是 conservative，实得 {source}")
        finally:
            uvc_camera_service.CAMERA_MODE_STATE_DIR = old_dir
            if original is None:
                uvc_camera_service._MODE_LADDERS.pop(key, None)
            else:
                uvc_camera_service._MODE_LADDERS[key] = original


def test_desync_is_never_masked_by_a_learned_value():
    """扫描程序报表外档位 = 装了新扫描程序没重编服务，必须报出来。

    学到的值在这儿是个陷阱：它能让这台机器「看起来没问题」，把脱节一直瞒
    到换机器那天——那时服务会 rc=2 直接退出，把整个夹爪一起带走。
    """
    with tempfile.TemporaryDirectory() as state_dir:
        old_dir = uvc_camera_service.CAMERA_MODE_STATE_DIR
        uvc_camera_service.CAMERA_MODE_STATE_DIR = state_dir
        try:
            write_state(state_dir,
                        _mode_state_key(DECXIN_VID, DECXIN_PID, CONTROLLER,
                                        SERIAL), 6, 944)
            candidate = decxin_candidate()
            candidate["altsetting"] = 7
            candidate["descriptor_payload"] = 944   # 拆开配的自相矛盾档位
            try:
                UvcCameraServiceManager._starting_mode(candidate, CONTROLLER)
                check(False, "表外档位应当抛 UvcCameraServiceError")
            except UvcCameraServiceError as error:
                check("脱节" in str(error),
                      f"报错文案点明档位表脱节，实得「{error}」")
        finally:
            uvc_camera_service.CAMERA_MODE_STATE_DIR = old_dir


def test_state_key_matches_the_service():
    """服务端 C 测试里写死了这个文件名（tests/test_altsetting_ladder.c）。
    两边对不上时，学到的东西服务写了、Python 读不到，表现是「每次开机都重
    新学一遍」——不会报错，只会一直多赔一次停摆。"""
    key = _mode_state_key(DECXIN_VID, DECXIN_PID, CONTROLLER, SERIAL)
    check(key == "1bcf_2d4f_0000_74_00_4_DECXIN-01",
          f"落盘键与服务端一致，实得 {key}")


def test_write_config_emits_an_effective_mode():
    with tempfile.TemporaryDirectory() as state_dir:
        old_dir = uvc_camera_service.CAMERA_MODE_STATE_DIR
        uvc_camera_service.CAMERA_MODE_STATE_DIR = state_dir
        try:
            with mock.patch.object(uvc_camera_service,
                                   "_usb_controller_for_bus",
                                   return_value=CONTROLLER):
                runtime_dir, config_path, sockets = manager()._write_config(
                    make_records())
            text = config_path.read_text(encoding="utf-8")
            check(str(runtime_dir) in text, "socket 落在本次运行的临时目录里")

            decxin_block = text.split("[unit_1_decxin]", 1)[1]
            check("forced_altsetting=6" in decxin_block
                  and "forced_payload=944" in decxin_block,
                  "DECXIN 段写的是生效的 alt6/944")
            check("forced_altsetting=7" not in decxin_block,
                  "DECXIN 段没有把扫描程序报的 alt7 照抄进去")
            check("# 起始档位来源: conservative" in decxin_block,
                  "DECXIN 段标注了档位来源")
            check(f"usb_serial={SERIAL}" in decxin_block
                  and f"usb_controller={CONTROLLER}" in decxin_block
                  and f"state_dir={state_dir}" in decxin_block,
                  "DECXIN 段带上了落盘键需要的三个字段")

            left_block = text.split("[unit_1_left]", 1)[1].split(
                "[unit_1_right]", 1)[0]
            check("forced_altsetting=3" in left_block
                  and "forced_payload=800" in left_block,
                  "Sightac 段写的是 alt3/800")
            check(set(sockets) == {(1, "left"), (1, "right"), (1, "decxin")},
                  f"socket 表覆盖三路，实得 {sorted(sockets)}")
        finally:
            uvc_camera_service.CAMERA_MODE_STATE_DIR = old_dir


def main():
    print("相机等时档起始档位自检")
    test_conservative_rung_is_the_default()
    test_last_rung_rule_generalises()
    test_learned_mode_wins()
    test_desync_is_never_masked_by_a_learned_value()
    test_state_key_matches_the_service()
    test_write_config_emits_an_effective_mode()
    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 项失败:")
        for item in FAILS:
            print(f"   - {item}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
