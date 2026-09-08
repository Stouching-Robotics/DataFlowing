"""
HTTP API 客户端 —— 对接 Data Acquisition 服务器。

真实 API:
  POST   /api/v1/session/upload          — 上传整个 session 的 zip 包
  GET    /api/v1/sessions                — 查询 sessions 列表
  GET    /api/v1/session/{id}            — session 详情
  DELETE /api/v1/session/{id}            — 删除 session
  GET    /api/v1/video/{id}/{cam}/stream — 视频流
  GET    /health                         — 健康检查
"""

from __future__ import annotations
import os
import time
from typing import Optional, Callable

import requests
import urllib3
from urllib3.util import Timeout as _Urllib3Timeout
from requests.adapters import HTTPAdapter


CONNECT_TIMEOUT = 10
READ_TIMEOUT = 1800        # 大会话（数 GB）上传 + 服务器解包入库可能耗时数分钟，设 30 分钟


class _PatientSendConnection(urllib3.connection.HTTPConnection):
    """连接子类：请求体发送阶段用读超时窗口，而不是连接超时。

    urllib3 的 connectionpool 在发请求体前把 conn.timeout 设为
    connect_timeout（本工程 10s），request() 顶层随即用该值对 socket
    做 settimeout —— 即发送大请求体时 socket 超时实际是"连接超时"。
    服务器收到大 zip 后一边入库一边慢读（导入队列繁忙时单次 sendall
    可停顿数秒~数分钟），停顿一超 10s 上传就被误杀为
    ConnectionError(('Connection aborted.', TimeoutError('timed out')))。
    这里把发送阶段的 socket 超时换成读超时窗口，发送完即恢复原值。
    """

    def request(self, method, url, body=None, headers=None, *args, **kwargs):
        saved = self.timeout
        # conn.timeout 此时是 urlopen 写入的 connect_timeout（数值）。
        # 非数值（如 Timeout 对象）说明不是这个路径，交给基类处理。
        if saved is not None and not isinstance(saved, _Urllib3Timeout):
            self.timeout = READ_TIMEOUT
        try:
            return super().request(method, url, body=body, headers=headers,
                                   *args, **kwargs)
        finally:
            self.timeout = saved


class _PatientSendHTTPSConnection(_PatientSendConnection,
                                  urllib3.connection.HTTPSConnection):
    pass


class _PatientSendHTTPConnectionPool(urllib3.HTTPConnectionPool):
    ConnectionCls = _PatientSendConnection


class _PatientSendHTTPSConnectionPool(urllib3.HTTPSConnectionPool):
    ConnectionCls = _PatientSendHTTPSConnection


class _PatientSendAdapter(HTTPAdapter):
    """把发送超时加长的连接池装入 session。"""

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)
        # PoolManager 默认指向模块级 dict（多实例共享），须整体替换实例属性
        self.poolmanager.pool_classes_by_scheme = {
            "http": _PatientSendHTTPConnectionPool,
            "https": _PatientSendHTTPSConnectionPool,
        }


class APIClient:
    """服务器 REST API 客户端。

    session: 可选复用已有 requests.Session（如 TaskService 的已认证 session）。
    """

    def __init__(self, base_url: str, session: requests.Session = None):
        self.base_url = base_url.rstrip("/")
        self._own_session = session is None
        self._session = session if session is not None else requests.Session()
        self._session.headers.update({"User-Agent": "DAQ-SDK/1.0"})
        # 大请求体上传时发送阶段的 socket 超时改为读超时窗口，
        # 否则服务器慢读停顿 >10s 会误杀上传（见 _PatientSendConnection）。
        self._session.mount("http://", _PatientSendAdapter())
        self._session.mount("https://", _PatientSendAdapter())

    def close(self):
        if self._own_session:
            self._session.close()

    # ── 健康检查 ──────────────────────────────────────

    def health_check(self) -> bool:
        try:
            r = self._session.get(f"{self.base_url}/health",
                                  timeout=CONNECT_TIMEOUT)
            return r.status_code == 200
        except requests.RequestException:
            return False

    # ── 上传 session ──────────────────────────────────

    def upload_session_zip(self, zip_path: str, session_name: str,
                           progress_cb: Optional[Callable[[int, int], None]] = None,
                           name: str = "", project_id: str = "",
                           episode_index: int = 0
                           ) -> dict:
        """
        上传一个 session 的 zip 包到服务器。

        Args:
            zip_path: zip 文件路径
            session_name: 会话名称（v1.1.0 池化 = 任务名）
            progress_cb: 进度回调 (uploaded_bytes, total_bytes)
            name: 上传接口的 name 表单字段（会话名；服务器按它解析目标项目，
                  空则回退 session_name）
            project_id: 上传接口的 project_id 表单字段（目标项目 ID；
                  空 = 服务器按名称自动匹配，服务器上有多个同名/近似项目时
                  会返回 409 "Ambiguous project prefix"）
            episode_index: v1.1.0 池化表单字段——值为本 episode 的 file 号
                  （0 基，与 zip 内 file-NNN 完全一致；真实全局序号 N 在
                  parquet 行的 episode_index 列）。旧服务器忽略未知字段，
                  不影响既有上传。

        Returns:
            {"ok": True, "session_id": "...", "response": {...}}
            或 {"ok": False, "error": "..."}
        """
        url = f"{self.base_url}/api/v1/session/upload"
        file_size = os.path.getsize(zip_path)

        # 包装文件对象，在读取时回调进度
        class _ProgressReader:
            def __init__(self, path, cb, total):
                self._f = open(path, "rb")
                self._cb = cb
                self._total = total
                self._read = 0

            def read(self, size=-1):
                data = self._f.read(size)
                if data:
                    self._read += len(data)
                    if self._cb:
                        self._cb(self._read, self._total)
                return data

            def close(self):
                self._f.close()

        reader = _ProgressReader(zip_path, progress_cb, file_size)

        try:
            r = self._session.post(
                url,
                files={"file": (session_name + ".zip", reader, "application/zip")},
                # 显式传 name/project_id：服务器按 name 解析目标项目，
                # 多个同名/近似项目（含 workflow 别名撞名）时按前缀匹配
                # 会 409 歧义，须用 project_id 消歧。
                data={"name": name or session_name,
                      "project_id": project_id or "",
                      "episode_index": str(episode_index)},
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
            if r.status_code in (200, 201):
                data = r.json()
                return {"ok": True,
                        "session_id": data.get("session_id", ""),
                        "response": data}
            if r.status_code == 409:
                # 服务器按名称匹配项目时发现歧义（多项目前缀撞名）
                try:
                    detail = r.json().get("detail", "")
                except ValueError:
                    detail = r.text
                if "project_id" in str(detail) or "prefix" in str(detail).lower():
                    return {"ok": False,
                            "ambiguous_project": True,
                            "error": "项目名有歧义：服务器上有多个同名/近似项目。"
                                     "请在 ☁ 上传对话框的『目标项目』中指定项目后重试"}
                return {"ok": False, "error": f"HTTP 409: {r.text[:300]}"}
            return {"ok": False,
                    "error": f"HTTP {r.status_code}: {r.text[:300]}"}
        except requests.RequestException as e:
            return {"ok": False, "error": str(e)[:300]}
        finally:
            reader.close()

    def get_projects(self, limit: int = 100) -> list[dict]:
        """GET /api/v1/projects 项目列表（需已认证 session；失败/未认证返回 []）。

        用于上传对话框的「目标项目」下拉框。
        """
        try:
            r = self._session.get(
                f"{self.base_url}/api/v1/projects",
                params={"limit": limit},
                timeout=CONNECT_TIMEOUT,
            )
            if r.status_code == 200:
                return r.json().get("projects", [])
        except requests.RequestException:
            pass
        return []

    # ── 查询 ──────────────────────────────────────────

    def get_sessions(self, limit: int = 50) -> list[dict]:
        try:
            r = self._session.get(
                f"{self.base_url}/api/v1/sessions",
                params={"limit": limit},
                timeout=CONNECT_TIMEOUT,
            )
            if r.status_code == 200:
                data = r.json()
                return data.get("sessions", [])
        except requests.RequestException:
            pass
        return []

    def get_session(self, session_id: str) -> Optional[dict]:
        try:
            r = self._session.get(
                f"{self.base_url}/api/v1/session/{session_id}",
                timeout=CONNECT_TIMEOUT,
            )
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        return None

    def delete_session(self, session_id: str) -> bool:
        try:
            r = self._session.delete(
                f"{self.base_url}/api/v1/session/{session_id}",
                timeout=CONNECT_TIMEOUT,
            )
            return r.status_code == 200
        except requests.RequestException:
            return False
