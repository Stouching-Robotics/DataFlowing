"""
Task record persistence — data/tasks.json is the single source of truth.

- 进度分账（多电脑协同，后端聚合口径）：
    local_count    本机累计完成段数（increment_task_completed 加它）
    synced_count   后端已确认的本机段数（水位，只前进不后退）
    backend_count  最近一次已知后端全局数（merge/flush 用后端值无条件替换）
    completed_count = backend_count + (local_count - synced_count)
      —— 派生显示值，字段名保留使 UI 零改动，所有写路径经 _recount() 重算。
- 迁移回填：旧数据没有上述字段时，local/synced/backend 一次性回填为
  旧 completed_count（目录扫描兜底）——历史不补报、显示连续；
  此后以持久化值为准，上传后删本地文件也不影响进度。
- Task metadata (id, name, description, total_required, etc.) is persisted.
- Backend tasks are merged into the local record, keeping local-only tasks.

- 本机任务（source="local"）：
    任务页「＋ 新建任务」创建，id 前缀 local-，不进后端任务表、不参与进度上报。
    * **仅创建者可见**：created_by 记创建时的身份（"guest" 或用户名），
      filter_by_identity() 对本机任务按 created_by 过滤（缺失时对所有人可见，
      兼容手工写的数据）；assigned_user 恒为 None，仍然算「公共任务」，
      这样 merge_backend_tasks 的账号撤销清除不会删到它（另有显式豁免）。
    * **进度只在本机**：local_count 累计，synced_count/backend_count 恒 0，
      pending_sync_tasks() 跳过本机任务 —— 服务端没有对应任务，上报必被拒。
    * **删除是真删记录**：remove_local_task() 整条移除（后端不会再推送它，
      不需要 hidden 墓碑）；服务端任务仍走 mark_hidden()。
    * **名字即目录即上传项目名**：只允许字母/数字/汉字/下划线/连字符，
      保证 <RECORDING_DIR>/<name> 与 task_tag(name) 完全一致。
"""

from __future__ import annotations
import json
import os
import threading
import uuid
from datetime import datetime, timezone

from config import settings

_TASKS_FILE = os.path.join(settings.DATA_DIR, "tasks.json")

# ── 本机任务常量 ──────────────────────────────────────
LOCAL_TASK_PREFIX = "local-"
MAX_TASK_NAME_LEN = 64
MAX_TASK_DESCRIPTION_LEN = 500
MAX_TOTAL_REQUIRED = 100000

# Windows 保留设备名（不能作为目录名；大小写不敏感）
_WINDOWS_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)


class TaskValidationError(ValueError):
    """本机任务创建/改名被拒。code 供 UI 本地化，不把中文写进核心层。"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail

    def __str__(self) -> str:  # 日志/调试可读
        return f"{self.code}: {self.detail}" if self.detail else self.code


_lock = threading.RLock()  # reentrant — merge_backend_tasks calls save_tasks/load_tasks

_SEED_TASKS = [
    {
        "id": "task_001",
        "name": "cylinder_grasping",
        "description": "Grasp cylinder parts with right hand, record 16x16 tactile sensor data.",
        "status": "pending",
        "total_required": 1000,
        "assigned_at": "2026-08-07T09:00:00Z",
        "params": {"object": "cylinder", "hand": "right"},
    },
    {
        "id": "task_002",
        "name": "cube_placement",
        "description": "Place cube objects from point A to point B using both hands.",
        "status": "pending",
        "total_required": 500,
        "assigned_at": "2026-08-06T14:30:00Z",
        "params": {"object": "cube", "hand": "both"},
    },
    {
        "id": "task_003",
        "name": "valve_rotation",
        "description": "Rotate circular valve 90 degrees with single hand, record torque and tactile feedback.",
        "status": "pending",
        "total_required": 800,
        "assigned_at": "2026-08-05T11:00:00Z",
        "params": {"object": "valve", "hand": "right", "angle": 90},
    },
]


# ═══════════════════════════════════════════════════════════
#  Public API
# ═══════════════════════════════════════════════════════════

def load_tasks(identity: str | None = None) -> list[dict]:
    """Load all tasks. Filters out hidden tasks.

    completed_count 以持久化值为准（increment_task_completed 累计）；
    文件中缺该字段（旧版本数据）时一次性按目录扫描回填并写回，
    此后删会话文件不再影响进度。

    Status is auto-computed: pending(0%), in_progress(>0%), completed(100%).

    排序: 按 assigned_at 从新到旧；null/空/解析失败排最后。

    identity（新增，默认 None 兼容旧调用）:
      None   → 返回全部任务（含他人创建的本机任务，仅供内部/统计调用）
      "guest" → 仅公共任务（无 assigned_user 字段）+ 游客创建的本机任务
      用户名  → 公共任务 + assigned_user == 该用户的任务
               + 该用户创建的本机任务

    注意: 除迁移回填外不写回 tasks.json —— hidden 任务必须保留在文件中，
    否则后端再次推送时无法识别为已删除，任务会死而复生。
    """
    with _lock:
        tasks = _read_raw()
    need_backfill = False
    for t in tasks:
        if _ensure_sync_fields(t):
            need_backfill = True
        _recount(t)
        t["status"] = _compute_status(t)
    if need_backfill:
        with _lock:
            _write({"tasks": tasks, "updated_at": datetime.now().isoformat()})
    visible = [t for t in tasks if not t.get("hidden")]
    visible = filter_by_identity(visible, identity)
    visible.sort(key=_assigned_at_key, reverse=True)  # 新→旧；null/空/非法排最后
    return visible


def filter_by_identity(tasks: list[dict], identity: str | None = None) -> list[dict]:
    """按身份过滤任务列表（纯函数，供 UI 对已有列表即时过滤）。

    identity 语义与 load_tasks 相同；返回新列表，不修改原列表。
    本机任务按 **created_by** 过滤（仅创建者可见）；assigned_user 语义只
    用于服务端任务（本机任务 assigned_user 恒为 None）。created_by 缺失的
    本机任务视为「无主」，对所有身份可见（兼容手工写的 tasks.json）。
    """
    if identity is None:
        return list(tasks)
    out: list[dict] = []
    for t in tasks:
        if is_local_task(t):
            if _local_visible(t, identity):
                out.append(t)
            continue
        if identity == "guest":
            if not _assigned_user(t):
                out.append(t)
        elif not _assigned_user(t) or _assigned_user(t) == identity:
            out.append(t)
    return out


def save_tasks(tasks: list[dict]):
    """Persist task metadata，含 completed_count（录制完成次数持久化权威值）。"""
    clean = [dict(t) for t in tasks]
    with _lock:
        _write({"tasks": clean, "updated_at": datetime.now().isoformat()})


# ═══════════════════════════════════════════════════════════
#  本机任务（采集端自建）—— 创建 / 改名 / 删除 / 校验
# ═══════════════════════════════════════════════════════════

def is_local_task(task: dict) -> bool:
    """是否采集端自建任务。source 缺失时按 id 前缀兜底（双保险）。"""
    if not isinstance(task, dict):
        return False
    if task.get("source") == "local":
        return True
    return str(_tid(task) or "").startswith(LOCAL_TASK_PREFIX)


def new_local_task_id() -> str:
    """本机任务 id：local-YYYYmmdd-HHMMSS-<4hex>，与后端 UUID 不会相撞。"""
    return "local-{}-{:04x}".format(
        datetime.now().strftime("%Y%m%d-%H%M%S"), uuid.uuid4().int & 0xFFFF)


def validate_task_name(name: str, exclude_id: str | None = None
                       ) -> tuple[bool, str]:
    """校验本机任务名，返回 (ok, 错误码)。错误码由 UI 本地化。

    规则（保证 name == task_tag(name)，即名字、录制目录、上传项目名一致）：
      * 去首尾空白后非空、≤ 64 字符；
      * 只允许字母/数字/汉字/下划线/连字符（空格、点、斜杠等一律拒绝）；
      * Windows 保留设备名（CON/PRN/AUX/NUL/COM1-9/LPT1-9）拒绝；
      * 与任何现有任务重名（大小写不敏感、含 hidden 与种子任务）拒绝。
    """
    with _lock:
        tasks = _read_raw()
    return _validate_name_locked(tasks, name, exclude_id)


def create_local_task(name: str, total_required: int, description: str = "",
                      created_by: str = "guest") -> dict:
    """新建本机任务并落盘，返回任务 dict。

    失败抛 TaskValidationError（code 见 validate_task_name / total_invalid /
    description_too_long）。任务仅创建者可见，进度只在本机累计。
    """
    total = _validate_total(total_required)
    desc = (description or "").strip()
    if len(desc) > MAX_TASK_DESCRIPTION_LEN:
        raise TaskValidationError("description_too_long")

    with _lock:
        tasks = _read_raw()
        ok, code = _validate_name_locked(tasks, name)
        if not ok:
            raise TaskValidationError(code)
        existing_ids = {_tid(t) for t in tasks}
        task_id = new_local_task_id()
        while task_id in existing_ids:
            task_id = new_local_task_id()
        task = {
            "id": task_id,
            "name": (name or "").strip(),
            "description": desc,
            "source": "local",
            "created_by": created_by or "guest",
            "status": "pending",
            "total_required": total,
            "assigned_at": datetime.now(timezone.utc).isoformat(),
            "assigned_user": None,
            # 进度分账三字段显式写 0：避开旧数据迁移回填（否则会去扫目录）
            "local_count": 0,
            "synced_count": 0,
            "backend_count": 0,
            "completed_count": 0,
        }
        tasks.append(task)
        _write({"tasks": tasks, "updated_at": datetime.now().isoformat()})
    task["status"] = _compute_status(task)
    return dict(task)


def rename_local_task(task_id: str, new_name: str,
                      allow_with_data: bool = False) -> dict:
    """本机任务改名。只允许改本机任务，且默认拒绝「已有录制目录」的任务。

    改名不迁移录制目录：本地已有数据时改名字会让旧目录失去归属、且可能
    与上传队列里的路径脱节，因此默认拒绝（code="has_data"）；新名字的
    目录已存在（孤儿目录）也拒绝（code="name_dir_exists"）。
    """
    with _lock:
        tasks = _read_raw()
        target = _find(tasks, task_id)
        if target is None:
            raise TaskValidationError("task_not_found")
        if not is_local_task(target):
            raise TaskValidationError("not_local")
        old_name = (target.get("name") or "").strip()
        clean = (new_name or "").strip()
        if clean == old_name:
            target["status"] = _compute_status(target)
            return dict(target)
        ok, code = _validate_name_locked(tasks, clean, exclude_id=task_id)
        if not ok:
            raise TaskValidationError(code)
        if not allow_with_data:
            old_dir = _local_task_dir(old_name)
            if old_dir and os.path.isdir(old_dir):
                raise TaskValidationError("has_data")
        new_dir = _local_task_dir(clean)
        if new_dir and os.path.isdir(new_dir):
            raise TaskValidationError("name_dir_exists")
        target["name"] = clean
        target["status"] = _compute_status(target)
        _write({"tasks": tasks, "updated_at": datetime.now().isoformat()})
        return dict(target)


def remove_local_task(task_id: str) -> bool:
    """硬移除本机任务（不写 hidden 墓碑）。

    后端永远不会推送本机任务，不需要防复活；整条移除后同名任务可以重建。
    只对 source=local 的条目生效，服务端任务返回 False（仍走 mark_hidden）。
    """
    with _lock:
        tasks = _read_raw()
        target = _find(tasks, task_id)
        if target is None or not is_local_task(target):
            return False
        kept = [t for t in tasks if _tid(t) != task_id]
        _write({"tasks": kept, "updated_at": datetime.now().isoformat()})
        return True


def _local_visible(task: dict, identity: str | None) -> bool:
    """本机任务可见性：只有创建者身份可见；无 created_by 视为无主（所有人可见）。"""
    creator = task.get("created_by")
    if not creator:
        return True
    return creator == identity


def _local_task_dir(name: str) -> str:
    """本机任务录制目录（懒 import helpers，避免 task_record 顶层拉重依赖）。"""
    if not name:
        return ""
    from core.helpers import task_dir_of
    return task_dir_of(settings.RECORDING_DIR, name)


def _validate_name_locked(tasks: list[dict], name: str,
                          exclude_id: str | None = None) -> tuple[bool, str]:
    """名字校验（调用方持 _lock 并传入当前任务表）。"""
    clean = (name or "").strip()
    if not clean:
        return False, "name_empty"
    if len(clean) > MAX_TASK_NAME_LEN:
        return False, "name_too_long"
    if any(not (c.isalnum() or c in "_-") for c in clean):
        return False, "name_illegal"
    if clean.upper() in _WINDOWS_RESERVED_NAMES:
        return False, "name_reserved"
    key = clean.casefold()
    for t in tasks:
        if exclude_id and _tid(t) == exclude_id:
            continue
        if str(t.get("name") or "").casefold() == key:
            return False, "name_duplicate"
    return True, ""


def _validate_total(total_required) -> int:
    """目标段数：1 ~ MAX_TOTAL_REQUIRED 的整数（bool 不算）。"""
    if isinstance(total_required, bool) or not isinstance(total_required, int):
        raise TaskValidationError("total_invalid")
    if total_required < 1 or total_required > MAX_TOTAL_REQUIRED:
        raise TaskValidationError("total_invalid")
    return total_required


def merge_backend_tasks(backend_tasks: list[dict], view_scope: str | None = None) -> list[dict]:
    """Merge backend tasks into local record. Returns merged list.

    - Backend tasks update existing entries by id, new ones are added.
    - Local-only tasks are preserved.
    - Invalid/incomplete tasks are filtered out.
    - 同名去重: 同一 (name, assigned_user) 组出现多个可见任务时，只保留
      assigned_at 最新的一个（后端推送的优先胜出——"根据后台指令更新任务细节"），
      其余标 hidden。公共任务（均无 assigned_user）分组结果与旧版等价。
    - view_scope（新增）: 当前会话身份。"guest"/None 不触发撤销清除；
      用户名时，本地 assigned_user == 该用户名、且本轮后端未返回的任务
      （已被后端撤销）从本地清除。游客轮询绝不触发撤销清除——防止把
      他人指派任务误删。本机任务（source=local）无论身份一律豁免：它们
      不在后端任务表里，按「后端没返回」清除会直接删掉自建任务。
    """
    with _lock:
        local = _read_raw()

    backend_ids = {_tid(t) for t in backend_tasks}

    # ── 阶段 0: 撤销清除（仅账号轮询；游客/无身份完全跳过）──
    if view_scope not in (None, "guest"):
        local = [
            lt for lt in local
            if is_local_task(lt)
            or not (_assigned_user(lt) == view_scope and _tid(lt) not in backend_ids)
        ]

    candidates: list[dict] = []

    # ── 阶段 1: 后端任务（按 id 合并，逻辑与现状一致）──
    for bt in backend_tasks:
        if not _is_valid(bt):
            continue
        bid = _tid(bt)
        local_match = _find(local, bid)
        backend_global = max(0, int(bt.get("completed_count") or 0))
        if local_match:
            # 保留本地覆盖字段
            for key in ("total_required", "params", "assigned_user"):
                if key in local_match and key not in bt:
                    bt[key] = local_match[key]
            # 进度分账：后端全局数权威；本机完成数/已同步水位沿用本地
            _ensure_sync_fields(local_match)
            bt["local_count"] = local_match["local_count"]
            bt["synced_count"] = local_match["synced_count"]
            bt["backend_count"] = backend_global
            # 保留删除标记：用户删过的任务不能被后端推送复活
            if local_match.get("hidden"):
                bt["hidden"] = True
        else:
            # 新任务：本机 0 段全同步；backend_count 取后端全局数
            # （保留 current_count 作新任务初值的旧语义）
            bt["local_count"] = 0
            bt["synced_count"] = 0
            bt["backend_count"] = backend_global
        candidates.append(bt)

    # ── 阶段 2: 本地独有任务 ──
    for lt in local:
        if not _is_valid(lt):
            continue
        if _tid(lt) not in backend_ids:
            candidates.append(dict(lt))

    # ── 阶段 3: 同名去重（(name, assigned_user) 墓碑）──
    groups: dict[tuple, list[dict]] = {}
    for c in candidates:
        groups.setdefault((c["name"], _assigned_user(c)), []).append(c)
    for group in groups.values():
        visible = [g for g in group if not g.get("hidden")]
        if len(visible) <= 1:
            continue  # 无碰撞（含整组已 hidden）→ 不动
        backend_visible = [v for v in visible if _tid(v) in backend_ids]
        pool = backend_visible if backend_visible else visible
        winner = max(pool, key=_assigned_at_key)
        for g in visible:
            if g is not winner:
                g["hidden"] = True  # 持久化墓碑，防止复活

    for t in candidates:
        _ensure_sync_fields(t)
        _recount(t)
        t["status"] = _compute_status(t)
    save_tasks(candidates)
    return load_tasks()


def mark_hidden(task_id: str):
    """Mark a task as hidden so it no longer appears in the UI."""
    with _lock:
        tasks = _read_raw()
        for t in tasks:
            if _tid(t) == task_id:
                t["hidden"] = True
                break
        _write({"tasks": tasks, "updated_at": datetime.now().isoformat()})


def refresh_progress() -> list[dict]:
    """Reload task list with persisted completion counts."""
    return load_tasks()


def increment_task_completed(task_name: str, task_id: str | None = None,
                             assigned_user: str | None = None) -> dict | None:
    """一次录制完成：把对应任务的录制完成次数持久化 +1，返回更新后的任务。

    匹配优先级：
      1) task_id 提供时按 id 精确匹配（须 name 一致）
      2) (name, assigned_user) 精确匹配（assigned_user 为 None 时匹配公共任务）
      3) 同名条目唯一时回退唯一匹配（兼容旧数据/手动临时任务名）
      4) 多名同名且身份不匹配 → 返回 None 不计数（跨身份串扰防护）
    task_id/assigned_user 由调用方从所选任务（_current_task）提供，
    而非会话身份——避免"登录用户录制公共任务却按用户名匹配"的错配。
    任务不在 tasks.json（手动临时任务名）时不计数，返回 None。
    会话目录随后被删除也不影响该计数——进度以持久化值为准。
    """
    if not task_name:
        return None
    with _lock:
        tasks = _read_raw()
        target = None
        if task_id:
            for t in tasks:
                if _tid(t) == task_id and t.get("name") == task_name:
                    target = t
                    break
        if target is None:
            for t in tasks:
                if (t.get("name") == task_name
                        and _assigned_user(t) == (assigned_user or None)):
                    target = t
                    break
        if target is None:
            matches = [t for t in tasks if t.get("name") == task_name]
            if len(matches) == 1:
                target = matches[0]
        if target is None:
            return None
        _ensure_sync_fields(target)
        target["local_count"] = int(target.get("local_count", 0) or 0) + 1
        _recount(target)
        target["status"] = _compute_status(target)
        _write({"tasks": tasks, "updated_at": datetime.now().isoformat()})
        return dict(target)


def pending_sync_tasks() -> list[dict]:
    """所有 local_count > synced_count 的任务快照（含 hidden，不按身份过滤——
    身份不符由服务端 401/403 处理，跳过本轮即可）。

    纯读路径：直接 _read_raw()，跳过未迁移记录（无 local_count），
    绝不触发迁移写盘（flush 只上报，不改变本地计数语义）。
    本机任务（source=local）显式跳过：后端没有对应任务，上报必被拒 400。
    """
    with _lock:
        tasks = _read_raw()
    out = []
    for t in tasks:
        if is_local_task(t):
            continue
        local = t.get("local_count")
        if not isinstance(local, int):
            continue
        synced = int(t.get("synced_count", 0) or 0)
        tid = _tid(t)
        if local > synced and tid:
            out.append({"id": tid, "name": t.get("name", ""),
                        "local_count": local, "synced_count": synced})
    return out


def mark_synced(task_id: str, synced_count: int, backend_count: int) -> dict | None:
    """flush 成功后回写同步状态。

    synced_count 必须是「快照水位 + 本次增量」（不是当前 local_count——
    快照与响应之间 UI 线程可能又录了一段）；水位只前进不后退；
    backend_count 用响应值无条件替换（服务端权威，迁移基线可能高于真实
    全局数，不能 max-前向）。返回更新后的任务。
    """
    with _lock:
        tasks = _read_raw()
        target = _find(tasks, task_id)
        if target is None or not isinstance(target.get("local_count"), int):
            return None
        if synced_count > (int(target.get("synced_count", 0) or 0)):
            target["synced_count"] = synced_count
        target["backend_count"] = max(0, int(backend_count or 0))
        _recount(target)
        target["status"] = _compute_status(target)
        _write({"tasks": tasks, "updated_at": datetime.now().isoformat()})
        return dict(target)


def task_by_id(task_id: str) -> dict | None:
    tasks = load_tasks()
    return _find(tasks, task_id)


# ═══════════════════════════════════════════════════════════
#  Internal
# ═══════════════════════════════════════════════════════════

def _read_raw() -> list[dict]:
    if not os.path.isfile(_TASKS_FILE):
        _init_seed()
    try:
        with open(_TASKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("tasks", [])
    except (json.JSONDecodeError, OSError):
        _init_seed()
        return [dict(t) for t in _SEED_TASKS]


def _write(data: dict):
    os.makedirs(os.path.dirname(_TASKS_FILE), exist_ok=True)
    tmp = _TASKS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _TASKS_FILE)


def _init_seed():
    _write({"tasks": [dict(t) for t in _SEED_TASKS], "updated_at": datetime.now().isoformat()})


def _tid(task: dict) -> str:
    return task.get("id", task.get("task_id", ""))


def _assigned_user(task: dict):
    """任务的指派用户；null/空串/缺失统一归一化为 None（= 公共任务）。"""
    return task.get("assigned_user") or None


def _assigned_at_key(task: dict) -> tuple:
    """排序键: 带有效时间的排前，null/空/解析失败排后。
    (0, raw) 与 (1, ts) 首元素不同，跨类不会比较第二元素。"""
    raw = task.get("assigned_at") or ""
    if not raw:
        return (0, "")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return (1, dt.timestamp())
    except ValueError:
        return (0, raw)  # 解析失败: 排最后，按原始字符串保序


def _find(tasks: list[dict], task_id: str) -> dict | None:
    for t in tasks:
        if _tid(t) == task_id:
            return t
    return None


def _is_valid(task: dict) -> bool:
    name = task.get("name", task.get("task_name", ""))
    tid = task.get("id", task.get("task_id", ""))
    return bool(name and name.strip()) and bool(tid and tid.strip()) and task.get("total_required", 0) > 0


def _compute_status(task: dict) -> str:
    total = task.get("total_required", 0)
    completed = task.get("completed_count", 0)
    if total <= 0:
        return "pending"
    if completed >= total:
        return "completed"
    if completed > 0:
        return "in_progress"
    return "pending"


def _count_with_fallback(task: dict) -> int:
    """任务的录制完成次数：优先持久化值，缺字段（旧版本数据）回退目录扫描。"""
    cur = task.get("completed_count")
    if isinstance(cur, int) and cur >= 0:
        return cur
    return _count_sessions(task.get("name", ""))


def _count_sessions(task_name: str) -> int:
    """扫描录制目录统计 episode 数 —— 仅用于旧数据迁移回填初值，非进度权威来源。

    v1.1.0 池化布局：list_task_episodes（videos/data 文件组扫描，与
    episodes 每段文件一致——删除时两者同步移除）；旧格式任务目录
    回退按会话子目录计数。
    """
    if not task_name:
        return 0
    task_dir = os.path.join(settings.RECORDING_DIR, task_name)
    if not os.path.isdir(task_dir):
        return 0
    try:
        from core.helpers import detect_session_format, list_task_episodes
        if detect_session_format(task_dir) == "pooled":
            return len(list_task_episodes(task_dir))
    except ImportError:
        pass
    count = 0
    try:
        for entry in os.listdir(task_dir):
            ses_dir = os.path.join(task_dir, entry)
            if not os.path.isdir(ses_dir):
                continue
            if os.path.isfile(os.path.join(ses_dir, "meta", "info.json")):
                count += 1
    except OSError:
        pass
    return count


def _ensure_sync_fields(task: dict) -> bool:
    """补全 local/synced/backend 三字段（迁移）；返回是否发生迁移（需写回）。

    迁移（幂等，字段存在且为合法 int 即跳过）：
      local_count   = 旧 completed_count（目录扫描兜底）
      synced_count  = local_count（历史不补报，避免重复计数）
      backend_count = local_count（历史基线保显示连续；首次 merge/flush
                      用真实全局数无条件替换）
    """
    changed = False
    local = task.get("local_count")
    if not isinstance(local, int) or local < 0:
        task["local_count"] = _count_with_fallback(task)
        changed = True
    synced = task.get("synced_count")
    if not isinstance(synced, int) or synced < 0:
        task["synced_count"] = task["local_count"]
        changed = True
    backend = task.get("backend_count")
    if not isinstance(backend, int) or backend < 0:
        task["backend_count"] = task["local_count"]
        changed = True
    return changed


def _recount(task: dict) -> int:
    """显示值 = backend_count + (local_count - synced_count)，写回 completed_count。

    防御：synced 不超 local（异常水位按已同步处理，宁可少显示不可多显示）。
    """
    local = int(task.get("local_count", 0) or 0)
    synced = min(int(task.get("synced_count", 0) or 0), local)
    task["completed_count"] = int(task.get("backend_count", 0) or 0) + (local - synced)
    return task["completed_count"]
