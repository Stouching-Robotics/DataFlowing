"""
DAQ 极简采集 —— 程序入口（lite 版）。

只保留: 连接设备 → 采集(parquet+MP4) → 上传。
无登录 / 任务页 / 回放 / 骨架解算 / qt-material 主题 / torch。
依赖白名单见 requirements-lite.txt；部署入口见 start_lite.bat / start_lite.sh。

用法:
    python main_lite.py
"""

import sys
import os

_base = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _base)

# ── 运行环境守卫（必须在 PyQt5 import 之前）────────────────────────
# 用错解释器（没激活 venv_lite / 激活了 conda 或别的 venv）时，真正的报错是
# "No module named 'PyQt5'" 这种看不出该怎么办的信息。这里提前判断关键依赖
# 是否真的缺，缺了就直接给出可照抄的启动命令（细节见模块注释）。
from core.startup_guard import enforce as _enforce_env

_enforce_env(_base, "lite")

from PyQt5.QtWidgets import QApplication, QDesktopWidget
from PyQt5.QtCore import Qt

from config import settings

# 低端机优化：跳过 10-20s 的 x265 速度探针，录制直接走 x264
# （egodata_writer 依据此开关在 start_episode 里选编码器）
settings.ENCODER_PROBE_ENABLED = False

from ui.lite_window import LiteWindow, LITE_QSS

# ── Qt 平台插件路径修复 ───────────────────────────────
# cv2（opencv-python 预编译轮子）首次 import 时会把
# QT_QPA_PLATFORM_PLUGIN_PATH 覆盖成它自带的 qt/plugins 目录，
# 其 xcb 插件与 PyQt5 的 Qt 库不兼容 → QApplication 创建即崩溃。
# 必须在所有 import 之后、QApplication 创建之前把路径抢回
# PyQt5 自带插件目录（与 main.py 同口径）。
import PyQt5
_qt_platforms = os.path.join(
    os.path.dirname(os.path.abspath(PyQt5.__file__)),
    "Qt5", "plugins", "platforms")
if os.path.isdir(_qt_platforms):
    os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = _qt_platforms


def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("DAQ 极简采集")
    app.setStyleSheet(LITE_QSS)

    window = LiteWindow()
    window.show()
    # 居中（与 main.py 同口径，WM 可能把首窗丢左上角）
    geo = window.frameGeometry()
    geo.moveCenter(QDesktopWidget().availableGeometry().center())
    window.move(geo.topLeft())
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
