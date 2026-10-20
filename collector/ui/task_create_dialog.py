"""本机任务对话框 —— 任务页「＋ 新建任务」/「✏ 改名」调用。

设计约定：
  * 只负责输入与实时校验，落盘由调用方经 ``core.task_record`` 完成
    （``create_local_task`` / ``rename_local_task``），错误码统一由本模块
    的 ``task_error_text`` 本地化。
  * 名称规则与 ``core.task_record.validate_task_name`` 完全一致：只允许
    字母/数字/汉字/下划线/连字符，去首尾空白后非空、≤ 64 字符、不重名。
    这样任务名 == 录制目录名 == 上传会话/项目名，不会出现三者不一致。
  * 本机任务仅创建者可见（身份在任务页创建时传入），对话框里如实提示。
  * 改名不迁移录制目录：任务已有本地目录时禁止改名（避免旧数据失去归属、
    上传队列路径脱节），改名对话框会把原因写清楚。
"""

from __future__ import annotations

import os

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIntValidator
from PyQt5.QtWidgets import (
    QDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QVBoxLayout,
)

from config import settings
from config.i18n import tr
from core import task_record
from core.helpers import list_task_episodes, task_dir_of

_LINE_EDIT_STYLE = (
    f"QLineEdit {{ background:{settings.COLOR_BG_WIDGET}; "
    f"color:{settings.COLOR_TEXT_PRIMARY}; "
    f"border:1px solid {settings.COLOR_BORDER}; "
    f"border-radius:4px; padding:6px 10px; font-size:13px; }}"
    f"QLineEdit:focus {{ border-color:{settings.COLOR_BTN_START}; }}"
    f"QLineEdit:disabled {{ color:{settings.COLOR_BTN_DISABLED_TEXT}; }}"
)
_PLAIN_STYLE = (
    f"QPlainTextEdit {{ background:{settings.COLOR_BG_WIDGET}; "
    f"color:{settings.COLOR_TEXT_PRIMARY}; "
    f"border:1px solid {settings.COLOR_BORDER}; "
    f"border-radius:4px; padding:4px 8px; font-size:12px; }}"
    f"QPlainTextEdit:focus {{ border-color:{settings.COLOR_BTN_START}; }}"
)
_LABEL_STYLE = (
    f"color:{settings.COLOR_TEXT_SECONDARY}; font-size:12px; "
    f"background:transparent; border:none;"
)
_HINT_STYLE = (
    f"color:{settings.COLOR_TEXT_HINT}; font-size:11px; "
    f"background:transparent; border:none;"
)
_ERROR_STYLE = (
    f"color:{settings.COLOR_ABNORMAL}; font-size:12px; "
    f"background:transparent; border:none;"
)

# 错误码 → 界面文案（源语言中文，config.i18n 提供英文）
_ERROR_TEXT = {
    "name_empty": "任务名称不能为空",
    "name_too_long": "任务名称最多 {} 个字符",
    "name_illegal": "任务名称只能包含字母、数字、汉字、下划线和连字符",
    "name_reserved": "任务名称不可用（系统保留名）",
    "name_duplicate": "已存在同名任务（可能属于其他账号或服务端）",
    "name_dir_exists": "该名称的录制目录已存在",
    "has_data": "该任务在本机已有录制目录（{} 段）；改名不迁移数据，请先上传并删除本地数据",
    "not_local": "只能修改本机任务",
    "task_not_found": "任务不存在",
    "total_invalid": "目标段数需在 1~{} 之间",
    "description_too_long": "任务描述最多 {} 个字符",
}


def task_error_text(code: str, *args) -> str:
    """错误码 → 当前语言文案；未知错误码回退为「操作失败：code」。"""
    text = _ERROR_TEXT.get(code)
    if text is None:
        return tr("操作失败：{}", code)
    return tr(text, *args)


def _identity_display(identity: str | None) -> str:
    return tr("游客") if not identity or identity == "guest" else str(identity)


def _label(text: str) -> QLabel:
    lb = QLabel(text)
    lb.setStyleSheet(_LABEL_STYLE)
    return lb


def _cancel_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setMinimumHeight(34)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setStyleSheet(
        f"QPushButton {{ background:{settings.COLOR_BTN_DEFAULT_BG}; "
        f"color:{settings.COLOR_TEXT_PRIMARY}; "
        f"border:1px solid {settings.COLOR_BORDER}; border-radius:5px; "
        f"padding:6px 20px; font-size:13px; }}"
        f"QPushButton:hover {{ background:{settings.COLOR_BTN_HOVER}; }}"
    )
    return btn


def _ok_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setMinimumHeight(34)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setStyleSheet(
        f"QPushButton {{ background:{settings.COLOR_BTN_START}; "
        f"color:white; font-weight:bold; font-size:13px; "
        f"border:none; border-radius:5px; padding:6px 24px; }}"
        f"QPushButton:hover {{ background:#388E3C; }}"
        f"QPushButton:disabled {{ background:{settings.COLOR_BTN_DISABLED_BG}; "
        f"color:{settings.COLOR_BTN_DISABLED_TEXT}; }}"
    )
    return btn


class TaskCreateDialog(QDialog):
    """新建本机任务：任务名称 + 目标段数 + 描述。exec_() 后经访问器取值。"""

    def __init__(self, parent=None, identity: str | None = "guest"):
        super().__init__(parent)
        self._identity = identity or "guest"
        self._ok_btn: QPushButton | None = None

        self.setWindowTitle(tr("新建采集任务"))
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setModal(True)
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 18)
        layout.setSpacing(10)

        title = QLabel(tr("新建采集任务"))
        title.setStyleSheet(
            f"font-size:18px; font-weight:bold; "
            f"color:{settings.COLOR_TEXT_PRIMARY}; background:transparent;")
        layout.addWidget(title)

        subtitle = QLabel(tr(
            "任务只保存在本机，仅当前身份（{}）可见；上传时按任务名匹配或"
            "自动创建服务器项目。", _identity_display(self._identity)))
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(_LABEL_STYLE)
        layout.addWidget(subtitle)

        form = QGridLayout()
        form.setVerticalSpacing(8)
        form.setHorizontalSpacing(10)

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText(tr("例如 grasp_cylinder_01"))
        self._name_edit.setMaxLength(task_record.MAX_TASK_NAME_LEN)
        self._name_edit.setStyleSheet(_LINE_EDIT_STYLE)
        self._name_edit.textChanged.connect(self._refresh_state)

        self._dir_label = QLabel("")
        self._dir_label.setWordWrap(True)
        self._dir_label.setStyleSheet(_HINT_STYLE)

        self._total_edit = QLineEdit("1")
        self._total_edit.setValidator(
            QIntValidator(1, task_record.MAX_TOTAL_REQUIRED, self))
        self._total_edit.setFixedWidth(110)
        self._total_edit.setStyleSheet(_LINE_EDIT_STYLE)
        self._total_edit.textChanged.connect(self._refresh_state)
        self._total_edit.returnPressed.connect(self.accept)
        total_row = QHBoxLayout()
        total_row.setContentsMargins(0, 0, 0, 0)
        total_row.setSpacing(8)
        total_row.addWidget(self._total_edit)
        unit = QLabel(tr("段"))
        unit.setStyleSheet(_LABEL_STYLE)
        total_row.addWidget(unit)
        total_row.addStretch(1)

        self._desc_edit = QPlainTextEdit()
        self._desc_edit.setPlaceholderText(tr("可选：任务说明，录制时悬停任务名显示"))
        self._desc_edit.setFixedHeight(64)
        self._desc_edit.setStyleSheet(_PLAIN_STYLE)
        self._desc_edit.textChanged.connect(self._refresh_state)

        form.addWidget(_label(tr("任务名称:")), 0, 0)
        form.addWidget(self._name_edit, 0, 1)
        form.addWidget(self._dir_label, 1, 1)
        form.addWidget(_label(tr("目标段数:")), 2, 0)
        form.addLayout(total_row, 2, 1)
        form.addWidget(_label(tr("任务描述:")), 3, 0, Qt.AlignTop)
        form.addWidget(self._desc_edit, 3, 1)
        layout.addLayout(form)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(_ERROR_STYLE)
        self._error_label.setVisible(False)
        layout.addWidget(self._error_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._cancel_btn = _cancel_button(tr("取消"))
        self._cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(self._cancel_btn)
        self._ok_btn = _ok_button(tr("创建任务"))
        self._ok_btn.clicked.connect(self.accept)
        buttons.addWidget(self._ok_btn)
        layout.addLayout(buttons)

        self._name_edit.returnPressed.connect(self.accept)
        self._refresh_state()

    # ── 访问器 ────────────────────────────────────────

    def name(self) -> str:
        return self._name_edit.text().strip()

    def total_required(self) -> int:
        text = self._total_edit.text().strip()
        return int(text) if text.isdigit() else 0

    def description(self) -> str:
        return self._desc_edit.toPlainText().strip()

    # ── 校验 ──────────────────────────────────────────

    def _validate(self) -> str:
        """返回错误码；空串 = 通过。"""
        ok, code = task_record.validate_task_name(self.name())
        if not ok:
            return code
        if not 1 <= self.total_required() <= task_record.MAX_TOTAL_REQUIRED:
            return "total_invalid"
        if len(self.description()) > task_record.MAX_TASK_DESCRIPTION_LEN:
            return "description_too_long"
        return ""

    def _refresh_state(self):
        name = self.name()
        code = self._validate()
        # 名字里含非法字符/超长时不要显示「清洗后」的目录预览（会误导用户
        # 以为能建成）；重名仍显示路径，用户能看到撞的是哪个目录。
        preview_ok = bool(name) and code not in (
            "name_too_long", "name_illegal", "name_reserved")
        self._dir_label.setText(
            tr("本机目录: {}", task_dir_of(settings.RECORDING_DIR, name))
            if preview_ok else "")
        if code:
            self._error_label.setText(self._error_text(code))
            self._error_label.setVisible(True)
            if self._ok_btn is not None:
                self._ok_btn.setEnabled(False)
            return

        # 名称合法但目录已存在（无任务记录的孤儿目录）：允许创建，但要说明
        warning = ""
        if name:
            task_dir = task_dir_of(settings.RECORDING_DIR, name)
            if os.path.isdir(task_dir):
                try:
                    n_eps = len(list_task_episodes(task_dir))
                except OSError:
                    n_eps = 0
                warning = tr(
                    "该名称的录制目录已存在（{} 段）。新任务进度从 0 开始，"
                    "录制会追加到同一目录。", n_eps)
        self._error_label.setText(warning)
        self._error_label.setStyleSheet(
            _HINT_STYLE if warning else _ERROR_STYLE)
        self._error_label.setVisible(bool(warning))
        if self._ok_btn is not None:
            self._ok_btn.setEnabled(True)

    @staticmethod
    def _error_text(code: str) -> str:
        if code == "total_invalid":
            return task_error_text(code, task_record.MAX_TOTAL_REQUIRED)
        if code == "name_too_long":
            return task_error_text(code, task_record.MAX_TASK_NAME_LEN)
        if code == "description_too_long":
            return task_error_text(code, task_record.MAX_TASK_DESCRIPTION_LEN)
        return task_error_text(code)

    def accept(self):
        code = self._validate()
        if code:
            self._error_label.setText(self._error_text(code))
            self._error_label.setStyleSheet(_ERROR_STYLE)
            self._error_label.setVisible(True)
            return
        super().accept()


class TaskRenameDialog(QDialog):
    """本机任务改名（不迁移录制目录；已有本地目录时禁止）。"""

    def __init__(self, parent=None, task: dict | None = None):
        super().__init__(parent)
        self._task = dict(task or {})
        self._task_id = str(self._task.get("id") or self._task.get("task_id") or "")
        self._old_name = str(self._task.get("name") or "")
        self._ok_btn: QPushButton | None = None

        self.setWindowTitle(tr("任务改名"))
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setModal(True)
        self.setMinimumWidth(500)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 18)
        layout.setSpacing(10)

        title = QLabel(tr("任务改名"))
        title.setStyleSheet(
            f"font-size:18px; font-weight:bold; "
            f"color:{settings.COLOR_TEXT_PRIMARY}; background:transparent;")
        layout.addWidget(title)

        current = QLabel(tr("当前名称：{}", self._old_name))
        current.setWordWrap(True)
        current.setStyleSheet(_LABEL_STYLE)
        layout.addWidget(current)

        form = QGridLayout()
        form.setVerticalSpacing(8)
        form.setHorizontalSpacing(10)
        self._name_edit = QLineEdit(self._old_name)
        self._name_edit.setMaxLength(task_record.MAX_TASK_NAME_LEN)
        self._name_edit.setStyleSheet(_LINE_EDIT_STYLE)
        self._name_edit.selectAll()
        self._name_edit.textChanged.connect(self._refresh_state)
        self._name_edit.returnPressed.connect(self.accept)
        self._dir_label = QLabel("")
        self._dir_label.setWordWrap(True)
        self._dir_label.setStyleSheet(_HINT_STYLE)
        form.addWidget(_label(tr("新名称:")), 0, 0)
        form.addWidget(self._name_edit, 0, 1)
        form.addWidget(self._dir_label, 1, 1)
        layout.addLayout(form)

        # 已有录制目录 → 禁止改名（改名不迁移数据）
        self._has_data = False
        old_dir = task_dir_of(settings.RECORDING_DIR, self._old_name)
        if self._old_name and os.path.isdir(old_dir):
            self._has_data = True
            try:
                n_eps = len(list_task_episodes(old_dir))
            except OSError:
                n_eps = 0
            self._data_note = task_error_text("has_data", n_eps)
        else:
            self._data_note = ""

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(_ERROR_STYLE)
        layout.addWidget(self._error_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = _cancel_button(tr("取消"))
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self._ok_btn = _ok_button(tr("确定改名"))
        self._ok_btn.clicked.connect(self.accept)
        buttons.addWidget(self._ok_btn)
        layout.addLayout(buttons)

        self._refresh_state()

    def new_name(self) -> str:
        return self._name_edit.text().strip()

    def _refresh_state(self):
        name = self.new_name()
        code = ""
        if self._has_data:
            code = "has_data"
        elif name == self._old_name:
            code = "name_unchanged"
        else:
            ok, err = task_record.validate_task_name(
                name, exclude_id=self._task_id or None)
            if not ok:
                code = err
            elif os.path.isdir(task_dir_of(settings.RECORDING_DIR, name)):
                code = "name_dir_exists"

        preview_ok = (name and name != self._old_name
                      and code not in ("has_data", "name_too_long",
                                       "name_illegal", "name_reserved"))
        self._dir_label.setText(
            tr("本机目录: {}", task_dir_of(settings.RECORDING_DIR, name))
            if preview_ok else "")

        if code == "has_data":
            text = self._data_note
        elif code == "name_unchanged":
            text = tr("新名称与当前名称相同")
        elif code:
            text = self._error_text(code)
        else:
            text = ""

        self._error_label.setText(text)
        self._error_label.setStyleSheet(
            _HINT_STYLE if code == "name_unchanged" else _ERROR_STYLE)
        self._error_label.setVisible(bool(text))
        if self._ok_btn is not None:
            self._ok_btn.setEnabled(not code or code == "name_unchanged")

    @staticmethod
    def _error_text(code: str) -> str:
        if code == "name_too_long":
            return task_error_text(code, task_record.MAX_TASK_NAME_LEN)
        return task_error_text(code)

    def accept(self):
        if self._has_data:
            return
        if self.new_name() == self._old_name:
            super().accept()
            return
        ok, code = task_record.validate_task_name(
            self.new_name(), exclude_id=self._task_id or None)
        if not ok:
            self._error_label.setText(self._error_text(code))
            self._error_label.setStyleSheet(_ERROR_STYLE)
            self._error_label.setVisible(True)
            return
        if os.path.isdir(task_dir_of(settings.RECORDING_DIR, self.new_name())):
            self._error_label.setText(task_error_text("name_dir_exists"))
            self._error_label.setStyleSheet(_ERROR_STYLE)
            self._error_label.setVisible(True)
            return
        super().accept()
