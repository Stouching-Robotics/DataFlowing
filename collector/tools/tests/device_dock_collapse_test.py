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
  8. 正常关窗 / 最小化不算「用户收起」（旧 bug：每次退出都写坏偏好）
  9. 浮动窗口的宽度不污染记忆栏宽
  10. 配置写不进去时 closeEvent 不 abort（PyQt5 虚方法抛异常 = SIGABRT 134）
  11. 离屏进程不得写真实 server_config.json（防测试/探针污染用户偏好）
"""
import json
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
    w_before_collapse = win._device_dock.width()   # × 之前栏是可见的，宽度应被记住
    win._device_dock.close()          # × 按钮走的就是 close()
    wait(app, 100)
    check(not win._device_dock.isVisible(), "× 收起后设备栏不可见")
    check(win._device_dock_user_hidden, "收起被记为用户意图")
    check(not win._device_dock_action.isChecked(), "菜单勾选同步为未选中")
    check(settings.load_device_dock_prefs()["visible"] is False,
          "收起状态已落盘")
    # 收起是最主要的手动落盘时机：把「拖窄 → 点 × → 重启」这条链路也钉住
    check(abs(settings.load_device_dock_prefs()["width"] - w_before_collapse) <= 8,
          f"× 收起时记住了收起前的栏宽：存 "
          f"{settings.load_device_dock_prefs()['width']}px（收起前 {w_before_collapse}px）")

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

    print("── 8. 正常关窗 / 最小化 不得被当成「用户收起」 ──")
    # 旧 bug：关窗时 Qt 把 dock 藏起来 → visibilityChanged(False)，代码把它当成
    # 用户点了 ×，于是每次正常退出都把偏好覆写成「已收起」，下次启动凭空消失。
    settings.save_device_dock_prefs(True, settings.DEVICE_DOCK_DEFAULT_WIDTH)
    win4 = make_window(app)
    check(win4._device_dock.isVisible(), "起点：采集页设备栏可见、偏好为「显示」")
    win4.resizeDocks([win4._device_dock], [min_w], Qt.Horizontal)
    app.processEvents()
    w_before = win4._device_dock.width()
    win4.close()                       # 等价于用户点窗口右上角 ×
    wait(app, 200)
    check(not win4._device_dock_user_hidden,
          "关主窗口没被当成用户收起（旧 bug：visibilityChanged 误判）")
    prefs = settings.load_device_dock_prefs()
    check(prefs["visible"] is True, f"关窗后偏好仍是「显示」：{prefs}")
    check(abs(prefs["width"] - w_before) <= 8,
          f"关窗时抓到的是真实栏宽：存 {prefs['width']}px（关闭前 {w_before}px）")
    win5 = make_window(app)
    check(win5._device_dock.isVisible(),
          "重开窗口设备栏照常显示（旧 bug 是凭空消失）")

    win5.showMinimized()               # 最小化同样只让 dock 变不可见
    wait(app, 200)
    check(not win5._device_dock_user_hidden, "最小化没被当成用户收起")
    check(settings.load_device_dock_prefs()["visible"] is True,
          "最小化后偏好仍是「显示」")
    win5.showNormal()
    wait(app, 200)
    check(win5._device_dock.isVisible(), "还原窗口后设备栏回来")

    print("── 9. 浮动窗口的宽度不污染记忆栏宽 ──")
    win5.resizeDocks([win5._device_dock], [300], Qt.Horizontal)
    app.processEvents()
    win5._save_device_dock_prefs()     # 停靠态抓一次（= 收起前/退出前的口径）
    docked_saved = settings.load_device_dock_prefs()["width"]
    check(abs(docked_saved - win5._device_dock.width()) <= 8,
          f"停靠态记忆栏宽 = 实际 {win5._device_dock.width()}px")
    win5._device_dock.setFloating(True)
    wait(app, 200)
    win5._device_dock.resize(520, win5._device_dock.height())
    wait(app, 200)
    check(win5._device_dock.isFloating(), "已浮动")
    float_w = win5._device_dock.width()
    win5._save_device_dock_prefs()
    after = settings.load_device_dock_prefs()["width"]
    check(abs(float_w - docked_saved) > 20,
          f"浮动宽度 {float_w}px 与记忆栏宽 {docked_saved}px 确实不同（断言才有意义）")
    check(abs(after - docked_saved) <= 8,
          f"浮动宽度没写进偏好（仍是 {after}px）")
    win5._device_dock.close()          # 浮动窗口的 ×
    wait(app, 200)
    check(win5._device_dock_user_hidden, "浮动窗口 × 也算用户收起")
    check(settings.load_device_dock_prefs()["visible"] is False, "收起已落盘")
    win5._device_dock_action.trigger()  # 恢复
    wait(app, 300)
    check(win5._device_dock.isVisible(), "菜单能恢复（Qt 记忆浮动位置，回来仍浮动）")
    check(abs(settings.load_device_dock_prefs()["width"] - docked_saved) <= 8,
          "恢复过程未改记忆栏宽")
    win5._device_dock.setFloating(False)   # 拖回停靠区
    wait(app, 200)

    print("── 10. 配置写不进去时不得把退出流程带崩 ──")
    # PyQt5 里虚方法抛未捕获异常 = qFatal → SIGABRT(134)，后面的停线程/拆 worker
    # 全不执行；一次偏好写失败绝不该让程序「崩溃退出」。
    bad_dir = tempfile.mkdtemp()
    orig_cfg = settings.SERVER_CONFIG_FILE
    settings.SERVER_CONFIG_FILE = bad_dir      # open(dir, "w") → IsADirectoryError
    try:
        win5._save_device_dock_prefs()         # 不抛异常才算过关
        check(True, "写盘失败只丢一次偏好，不抛异常")
        win5.close()
        wait(app, 200)
        check(True, "配置不可写时 closeEvent 正常走完（没 abort）")
    except Exception as exc:
        check(False, f"写盘失败抛出了 {exc!r} → 真实关窗会 abort")
    finally:
        settings.SERVER_CONFIG_FILE = orig_cfg
        try:
            os.rmdir(bad_dir)
        except OSError:
            pass

    print("── 11. 离屏进程不得写真实 server_config.json ──")
    # 2026-10-13 事故：离屏脚本/测试建真 MainWindow 后点 × 收起，把 visible=false
    # 写进用户真实配置 → 用户下次启动设备栏凭空消失。这里用「假默认路径」当沙箱，
    # 在完全不动真实配置的前提下验证闸门。
    sandbox_dir = tempfile.mkdtemp()
    sandbox = os.path.join(sandbox_dir, "server_config.json")
    with open(sandbox, "w", encoding="utf-8") as f:
        f.write("{}")
    o_cfg, o_def = settings.SERVER_CONFIG_FILE, settings._DEFAULT_SERVER_CONFIG_FILE
    try:
        settings.SERVER_CONFIG_FILE = sandbox
        settings._DEFAULT_SERVER_CONFIG_FILE = sandbox      # 假装这就是用户真实路径
        settings.save_device_dock_prefs(False, 587)
        with open(sandbox, encoding="utf-8") as f:
            check(json.load(f) == {},
                  "离屏 + 默认路径 → 拒写（用户真实偏好不会被探针改掉）")
        # 显式重定向过的路径（测试/探针的正确姿势）→ 照常放行，持久化断言不受影响
        settings._DEFAULT_SERVER_CONFIG_FILE = os.path.join(sandbox_dir, "other.json")
        settings.save_device_dock_prefs(False, 587)
        with open(sandbox, encoding="utf-8") as f:
            check(json.load(f).get("device_dock") == {"visible": False, "width": 587},
                  "显式重定向过的路径照常落盘")
    finally:
        settings.SERVER_CONFIG_FILE = o_cfg
        settings._DEFAULT_SERVER_CONFIG_FILE = o_def
        try:
            os.unlink(sandbox)
            os.rmdir(sandbox_dir)
        except OSError:
            pass

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        return 1
    print("PASS: 设备检测栏 缩小/隐藏 回归测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
