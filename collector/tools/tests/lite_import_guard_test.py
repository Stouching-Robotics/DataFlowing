"""极简采集版导入隔离断言测试（无硬件，离屏 Qt）。

主防线: sys.modules 运行时断言 —— 构造 LiteWindow 后，禁止名单
（重依赖 + 主程序 UI 模块）不得出现在已导入模块中（含传递性导入）；
白名单（lite 采集/上传闭包）必须全部在。

若本测试红，说明某人给 lite 闭包引了重依赖（如 glove_widget 顶层
import solver/render_engine），venv_lite 用户装 requirements-lite.txt
后 `import main_lite` 就会失败。

用法:
    QT_QPA_PLATFORM=offscreen venv/bin/python tools/tests/lite_import_guard_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

# 必须先 import main_lite（生产入口），再构造窗口
import main_lite  # noqa: F401

from config import settings
from core.pipeline import CameraPipeline
from ui.lite_window import LiteWindow

OUT_ROOT = "/tmp/lite_guard_test"

# 禁止名单: 重依赖 / 主程序 UI / 被砍功能模块（出现即失败）
BLACKLIST = [
    "torch", "scipy", "loguru", "pydantic", "pyvista", "pyvistaqt",
    "mediapipe", "h5py", "qt_material", "ultralytics",
    # UVC 枚举在 lite 中不得依赖（Linux sysfs / Windows DShow 退化路径）
    "pygrabber", "comtypes",
    "stouch_glove_toolkit",
    "ui.main_window", "ui.glove_widget", "ui.camera_widget",
    "ui.camera_grid", "ui.device_panel", "ui.login_dialog",
    "ui.playback_dialog", "ui.task_page", "ui.upload_dialog",
    "ui.guide_dialog",
    "core.task_service", "core.glove_keypoint_solver",
    "core.render_engine", "core.sensor_hand_config",
    "core.hand_tracking", "core.hand_processor", "core.auto_labeler",
    # 注：core.s80m_manager 2026-09-16 起**在 lite 闭包内**（UMI 夹爪的
    # core/gripper/bridge.py 顶层 import 它的丢帧常量），已从禁止名单移出，
    # 与 core/gripper/* 一起由下面的夹爪闭包段断言。
    "core.session_loader", "core.session_timeline",
    "core.session_catalog", "core.recording_repository",
    "core.task_record", "core.device_manager", "core.device_naming",
    "core.exposure_controller",
]

# 白名单: lite 闭包顶层模块（必须在）
WHITELIST = [
    "main_lite", "ui.lite_window",
    "config.settings", "config.i18n",
    "core.pipeline", "core.egodata_writer", "core.encoder_probe",
    "core.depth_codec", "core.d435_manager", "core.stereo_depth",
    "core.d435_camera", "core.camera", "core.device_detector",
    "core.ble_engine", "core.usb_glove_engine",
    "core.uploader", "core.api_client", "core.database", "core.helpers",
    # 力矩阵编码的家（ui.main_window 只是 re-export）—— lite 直接用
    "core.gripper_codec",
]


def main():
    app = QApplication(sys.argv)
    # 测试用独立输出目录 + 注入最小管线，避免碰真实录制目录
    win = LiteWindow(pipeline=CameraPipeline(output_dir=OUT_ROOT))
    win.close()
    app.processEvents()

    # 夹爪闭包：扫描线程只在**真有夹爪插着**时才走到 core.gripper.bridge，
    # 且它在后台线程里（win.close() 早于它完成 → 断言会 flaky），所以这里
    # 显式导入一遍，让下面的禁止名单覆盖整个夹爪闭包 —— 确认它没把
    # torch/scipy 之类的重依赖拖进来（那是 lite 体积与客户机装不上的红线）。
    # 只在 Linux 上有意义：原生栈是 ELF，且 core/gripper 顶层 `import fcntl`
    # 在 Windows 上直接导入失败——那正是 Windows 包的预期降级路径。
    gripper_state = "跳过（非 Linux）"
    if sys.platform.startswith("linux"):
        import core.gripper.bridge   # noqa: F401
        import core.s80m_manager     # noqa: F401
        gripper_state = "已导入并断言"

    bad = sorted(m for m in BLACKLIST
                 if any(k == m or k.startswith(m + ".")
                        for k in sys.modules))
    missing = sorted(m for m in WHITELIST if m not in sys.modules)

    # ENCODER_PROBE_ENABLED 是 lite 入口的运行时优化，测试顺带断言
    probe_off = settings.ENCODER_PROBE_ENABLED is False

    if bad or missing or not probe_off:
        for m in bad:
            print(f"BLACKLIST VIOLATION: {m} 被导入")
        for m in missing:
            print(f"WHITELIST MISSING: {m} 未导入")
        if not probe_off:
            print("ENCODER_PROBE_ENABLED 应为 False（lite 入口运行时赋值）")
        print("FAIL")
        return 1
    print(f"PASS: {len(WHITELIST)} 个白名单模块全部就位，"
          f"禁止名单无导入，编码器探针已关闭，夹爪闭包 {gripper_state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
