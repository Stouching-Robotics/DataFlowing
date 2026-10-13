"""设备检测栏（左侧停靠面板）缩小 / 隐藏回归测试（offscreen）。

用法:
    QT_QPA_PLATFORM=offscreen venv/bin/python tools/tests/device_dock_collapse_test.py

背景（2026-10-13 的 bug）:
  面板底部提示「双击设备可重命名；右键夹爪可重读标定 / 配对序列号 / 串口诊断」
  是不换行的 QLabel，minimumSizeHint 就是整行文字宽（中文 + 夹爪在场时 289px），
  成了停靠栏的硬地板 —— 栏只能越拖越宽、拖不小；且 dock 无 Closable，没有任何
  隐藏入口。本测试是这两条的回归哨兵。

覆盖:
  1. 中文 + 夹爪：面板最小宽度远小于旧地板（<=150），且栏能真的拖到 DEVICE_DOCK_MIN_WIDTH
  2. dock 带 Closable（标题栏 × = 收起）
  3. 标题栏 × 收起 → 菜单勾选同步 + 偏好落盘
  4. 收起后切页（任务页 ↔ 采集页）不弹回
  5. 视图▸设备检测栏 / Ctrl+D 能收起也能恢复
  6. 宽度与收起状态持久化 → 新窗口按偏好恢复
  7. 中英文切换刷新菜单项文字
"""
import os
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QDockWidget

from config import settings
from config.i18n import lang_manager, tr
from core.device_detector import DeviceInfo
from ui.main_window import MainWindow

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


def wait(app, ms):
    t_end = time.time() + ms / 1000.0
    while time.time() < t_end:
        app.processEvents()
        time.sleep(0.01)


FAKE_GRIPPER = DeviceInfo(key="gripper:/dev/ttyACM9", kind="gripper",
                          display_name="UMI 夹爪 (fake)", serial="Fays-0001")
FAKE_UVC = DeviceInfo(key="uvc:usb-FakeCam_12345678", kind="uvc",
                      display_name="FakeCam", serial="12345678",
                      video_index=40)
FAKE_GLOVE = DeviceInfo(key="ble:AA:11:22:33:44:55", kind="data_ble",
                        display_name="Matrix Glove R", serial="AA:11:22:33:44:55",
                        address="AA:11:22:33:44:55")
FAKES = [FAKE_GRIPPER, FAKE_UVC, FAKE_GLOVE]


def make_window(app):
    """建窗口并停在采集页（设备栏可见）。调用方负责 close()。"""
    with patch("core.device_detector.detect_devices", return_value=FAKES):
        win = MainWindow()
        win.show()
        win._page_stack.setCurrentIndex(1)
        wait(app, 200)
        win._on_devices_scanned(FAKES)
    win._device_timer.stop()
    return win


def main():
    app = QApplication(sys.argv)
    lang_manager.set_language("zh")

    # 偏好全程指向临时文件（隔离真实 server_config.json）
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp_path = tmp.name
    tmp.close()
    _orig = settings.SERVER_CONFIG_FILE
    settings.SERVER_CONFIG_FILE = tmp_path
    try:
        return _main(app)
    finally:
        settings.SERVER_CONFIG_FILE = _orig
        for p in (tmp_path,):
            try:
                os.unlink(p)
            except OSError:
                pass


def _main(app):
    print("── 1. 中文 + 夹爪：面板不再有宽地板，栏能拖到最窄 ──")
    win = make_window(app)
    min_w = settings.DEVICE_DOCK_MIN_WIDTH
    panel_min = win._device_panel.minimumSizeHint().width()
    check(panel_min <= 150,
          f"面板最小宽度 {panel_min}px（旧 bug 是 289px，单行提示顶宽）")
    check(win._device_panel._refresh_hint.wordWrap()
          and tr("双击设备可重命名") in win._device_panel._refresh_hint.text(),
          "底部提示是换行的那句（不再吃整行宽度当最小宽度）")
    win.resizeDocks([win._device_dock], [min_w], Qt.Horizontal)
    app.processEvents()
    actual = win._device_dock.width()
    check(abs(actual - min_w) <= 8,
          f"拖到最窄 {min_w}px → 实际 {actual}px（能缩小了）")
    win.resizeDocks([win._device_dock], [320], Qt.Horizontal)
    app.processEvents()
    check(win._device_dock.width() >= 300,
          f"拖宽到 320px → 实际 {win._device_dock.width()}px（能放大）")

    print("── 2. dock 带 Closable（标题栏有 × = 隐藏入口） ──")
    feats = win._device_dock.features()
    check(bool(feats & QDockWidget.DockWidgetClosable), "features 含 Closable")
    check(bool(feats & QDockWidget.DockWidgetMovable)
          and bool(feats & QDockWidget.DockWidgetFloatable),
          "Movable/Floatable 未被改动")

    print("── 3. 标题栏 × 收起 → 菜单勾选同步 + 偏好落盘 ──")
    win._device_dock.close()          # × 按钮走的就是 close()
    wait(app, 100)
    check(not win._device_dock.isVisible(), "× 收起后设备栏不可见")
    check(win._device_dock_user_hidden, "收起被记为用户意图")
    check(not win._device_dock_action.isChecked(), "菜单勾选同步为未选中")
    check(settings.load_device_dock_prefs()["visible"] is False,
          "收起状态已落盘")

    print("── 4. 收起后切页不弹回 ──")
    win._page_stack.setCurrentIndex(0)
    wait(app, 100)
    win._page_stack.setCurrentIndex(1)
    wait(app, 100)
    check(not win._device_dock.isVisible(),
          "任务页→采集页往返后仍是收起（不被页面逻辑弹回）")
    win._page_stack.setCurrentIndex(0)
    wait(app, 100)
    check(not win._device_dock.isVisible(), "任务选择页本来就不显示")

    print("── 5. 视图▸设备检测栏 / Ctrl+D：能收起也能恢复 ──")
    win._device_dock_action.trigger()          # 恢复显示（任务页上点）
    wait(app, 100)
    check(not win._device_dock.isVisible(),
          "任务页点显示：只记偏好，不把栏拉到任务页上")
    check(win._device_dock_action.isChecked()
          and not win._device_dock_user_hidden, "偏好已置为「显示」")
    win._page_stack.setCurrentIndex(1)
    wait(app, 150)
    check(win._device_dock.isVisible(), "回采集页后按偏好显示出来")
    win._device_dock_action.trigger()          # 再收起
    wait(app, 100)
    check(not win._device_dock.isVisible(), "菜单收起生效")
    check(win._device_dock_action.shortcut().toString() == "Ctrl+D",
          f"快捷键: {win._device_dock_action.shortcut().toString()}")
    win._device_dock_action.trigger()          # 恢复
    wait(app, 100)
    check(win._device_dock.isVisible(), "菜单恢复生效")

    print("── 6. 宽度/收起状态持久化 → 新窗口按偏好恢复 ──")
    win.resizeDocks([win._device_dock], [min_w], Qt.Horizontal)
    app.processEvents()
    saved_w = win._device_dock.width()
    win._save_device_dock_prefs()
    prefs = settings.load_device_dock_prefs()
    check(prefs["width"] == saved_w,
          f"宽度落盘: 存 {prefs['width']}px（实际 {saved_w}px）")
    check(prefs["visible"] is True, "可见偏好落盘")
    win.close()
    wait(app, 200)

    # 新窗口：应恢复刚存的窄宽度（首启即窄，不用再拖一次）
    win2 = make_window(app)
    restored = win2._device_dock.width()
    check(abs(restored - saved_w) <= 8,
          f"重启恢复栏宽: {restored}px（存 {saved_w}px）")
    # 存成「收起」→ 新窗口首启就是收起
    win2._device_dock.close()
    wait(app, 100)
    win2.close()
    wait(app, 200)
    win3 = make_window(app)
    check(not win3._device_dock.isVisible(),
          "上次收起 → 新窗口首启保持收起（采集页也不弹回来）")
    check(not win3._device_dock_action.isChecked(), "菜单勾选与之一致")
    win3._device_dock_action.trigger()          # 恢复，别把状态留给下个测试
    wait(app, 100)
    check(win3._device_dock.isVisible(), "恢复显示")

    print("── 7. 中英文切换刷新菜单项 ──")
    lang_manager.set_language("en")
    wait(app, 100)
    check(win3._device_dock_action.text() == "Device Panel",
          f"英文菜单项: {win3._device_dock_action.text()}")
    lang_manager.set_language("zh")
    wait(app, 100)
    check(win3._device_dock_action.text() == "设备检测栏",
          f"中文菜单项: {win3._device_dock_action.text()}")
    win3.close()
    wait(app, 200)

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        return 1
    print("PASS: 设备检测栏 缩小/隐藏 回归测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
