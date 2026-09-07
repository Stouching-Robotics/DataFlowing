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
    "core.s80m_manager", "core.session_loader", "core.session_timeline",
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
]


def main():
    app = QApplication(sys.argv)
    # 测试用独立输出目录 + 注入最小管线，避免碰真实录制目录
    win = LiteWindow(pipeline=CameraPipeline(output_dir=OUT_ROOT))
    win.close()
    app.processEvents()

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
          f"禁止名单无导入，编码器探针已关闭")
    return 0


if __name__ == "__main__":
    sys.exit(main())
