"""批次内容指纹 — 识别"换个包名重传同一份数据"。

采集端偶尔会把已经上传过的录制再推一遍,但**包名变了**(例如重装、
重开 App 后本地去重标记丢失,或包名规则从序号变成时间戳)。此时
``session.py`` 的 ``incoming_name in existing_ids`` 判不出重复,同一份
数据就会被当成新批次追加,审核页出现两条一模一样的数据,导出也会
重复计一份(实测:UMIGripper_AI 的 4 组批次视频字节级相同、
parquet 除 ``episode_index`` 外逐值相同)。

本模块给出一个与**包名无关**的内容身份:

    - 每个视频文件的 md5(按 source_key 排序)
    - canonical data parquet 的内容哈希(排除 ``episode_index`` ——
      同一次录制被追加成不同 episode 时,只有这一列会变)

合成为一个短字符串 key,存到 ``state/batch_fingerprints.json``。
上传时先算新批次的 key,再按项目查表:命中且目标 episode 仍然存在,
就判定为重复上传。

**失败一律放行**:算不出指纹、索引读不到、目标 episode 已被删除 ——
任一情况都按"新批次"正常入库(即本模块不存在时的行为),绝不因为
指纹机制本身的问题丢数据。
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from app.localstore import STATE_ROOT

INDEX_PATH = STATE_ROOT / "batch_fingerprints.json"
_LOCK = threading.Lock()

# 视频按块读;读取量固定,不随文件大小膨胀。
_CHUNK = 1 << 20


def _file_md5(path: Path) -> str | None:
    digest = hashlib.md5()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(_CHUNK), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def _parquet_content_hash(path: Path) -> str | None:
    """canonical data parquet 的内容哈希,排除 ``episode_index``。

    ``episode_index`` 是同一次录制被追加成不同 episode 时唯一会变的列,
    必须排除,否则重复上传算不出相同的 key。用 Arrow IPC 序列化取字节:
    对嵌套 list 列(手套 250x250x3 力矩阵、手部关键点)比对 pandas 的
    hash 更稳,且与行序无关的假设更少 —— 这里行序本身就是内容的一部分。
    """
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        return None
    try:
        table = pq.read_table(path)
        if "episode_index" in table.column_names:
            table = table.drop_columns(["episode_index"])
        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, table.schema) as writer:
            writer.write_table(table)
        return hashlib.md5(sink.getvalue().to_pybytes()).hexdigest()
    except Exception:
        return None


def compute_fingerprint(root: Path) -> str | None:
    """Staging 目录(已 normalize)的内容指纹;算不出返回 None。"""
    root = Path(root)
    parts: list[str] = []

    videos_root = root / "videos"
    if videos_root.is_dir():
        from app.lerobot_v21 import iter_video_streams

        entries: list[tuple[str, str]] = []
        for source, path in iter_video_streams(videos_root):
            digest = _file_md5(Path(path))
            if digest is None:
                return None
            entries.append((str(source), digest))
        if not entries:
            return None
        parts.append("|".join(f"v:{s}={d}" for s, d in sorted(entries)))

    data_files = sorted(
        path for path in (root / "data").rglob("*.parquet")
        if path.is_file() and "/meta/" not in path.as_posix().casefold()
    )
    if not data_files:
        return None
    digest = _parquet_content_hash(data_files[0])
    if digest is None:
        return None
    parts.append(f"d:{digest}")

    return ";".join(parts)


def _read_index() -> dict[str, dict[str, str]]:
    try:
        value = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_index(index: dict) -> None:
    try:
        INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = INDEX_PATH.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(INDEX_PATH)
    except OSError:
        pass


def lookup_duplicate(project_folder: str, fingerprint: str | None,
                     live_ids: set[str]) -> str | None:
    """返回与本次上传内容相同的、且**仍然存在**的 episode_id。

    ``live_ids`` 来自当前项目数据集的实际扫描结果:索引里指向已删除
    episode 的陈旧条目在这里被自然过滤掉,因此"删除后重新上传同一条
    录制"仍会正常入库,不会被误判成重复。
    """
    if not fingerprint:
        return None
    with _LOCK:
        index = _read_index()
    entries = index.get(str(project_folder))
    if not isinstance(entries, dict):
        return None
    episode_id = entries.get(str(fingerprint))
    if not episode_id:
        return None
    return str(episode_id) if str(episode_id) in live_ids else None


def record_fingerprint(project_folder: str, fingerprint: str | None,
                       episode_id: str) -> None:
    """记录已提交批次的内容指纹(失败静默:只是失去一次去重机会)。"""
    if not fingerprint:
        return
    with _LOCK:
        index = _read_index()
        if not isinstance(index, dict):
            index = {}
        entries = index.get(str(project_folder))
        if not isinstance(entries, dict):
            entries = {}
        entries[str(fingerprint)] = str(episode_id)
        # 同一 episode 换内容(同名重传)时清掉它名下的旧指纹,避免
        # 索引里留下永远指不到实际内容的条目。
        for key in [k for k, v in entries.items()
                    if str(v) == str(episode_id) and k != str(fingerprint)]:
            entries.pop(key, None)
        index[str(project_folder)] = entries
        _write_index(index)


def forget_fingerprint(episode_id: str) -> None:
    """批次被删除后清除其指纹条目。"""
    target = str(episode_id)
    with _LOCK:
        index = _read_index()
        changed = False
        for entries in index.values():
            if not isinstance(entries, dict):
                continue
            for key in [k for k, v in entries.items() if str(v) == target]:
                entries.pop(key, None)
                changed = True
        if changed:
            _write_index(index)


def describe(project_folder: str) -> dict[str, Any]:
    """诊断用:该项目已记录的内容指纹。"""
    with _LOCK:
        index = _read_index()
    entries = index.get(str(project_folder))
    return entries if isinstance(entries, dict) else {}
