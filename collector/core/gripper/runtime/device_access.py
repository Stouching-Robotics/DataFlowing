"""Cross-process device-operation guards; no SDK calls or affinity changes."""
from contextlib import contextmanager
import fcntl
import os
import stat
import threading
import time


_HELD_LOCK = threading.RLock()
_HELD_BY_PROCESS = {}       # 锁文件路径 -> (持有者线程 ident, 深度)
_PATH_LOCKS = {}            # 锁文件路径 -> 进程内互斥（同进程不同线程排队）


def _path_lock(path):
    with _HELD_LOCK:
        lock = _PATH_LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[path] = lock
        return lock


def device_guard_held_by_process(path):
    """本进程是否正持有 ``path`` 上的 guard。

    flock 绑在 **open file description** 上，不是进程上：同一进程再 open
    一次同一文件，非阻塞加锁照样 EWOULDBLOCK。所以「加锁失败」推不出
    「被别的会话占用」——也可能是自己。要区分归属只能看这份进程内登记
    表，否则自我探测会把本机唯一那台设备判成「别人正在用」而跳过。
    """
    with _HELD_LOCK:
        entry = _HELD_BY_PROCESS.get(str(path))
    return entry is not None and entry[1] > 0


def _open_lock_file(path, flags):
    try:
        return os.open(path, flags)
    except FileNotFoundError:
        try:
            return os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return os.open(path, flags)


@contextmanager
def device_access_guard(path, *, timeout=30.0, cancelled=None):
    # Read-only flock also works for a lock created by a sudo-launched process.
    # Do not truncate, unlink or follow symlinks: every caller must lock the
    # same inode for the whole operation/lease lifetime.
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    hold_key = str(path)
    ident = threading.get_ident()
    deadline = time.monotonic() + timeout

    # 同线程重入：本线程已经持有这把锁 → 只加深度，绝不再开 fd 加第二次。
    # flock 认的是 open file description，认不出「这是我自己」，再开一个 fd
    # 必然 EWOULDBLOCK —— 同一个操作在内部再套一层就会把自己挡死。
    with _HELD_LOCK:
        entry = _HELD_BY_PROCESS.get(hold_key)
        nested = entry is not None and entry[0] == ident
        if nested:
            _HELD_BY_PROCESS[hold_key] = (ident, entry[1] + 1)

    if not nested:
        # 同进程不同线程：排队等，而不是互撞。flock 只挡得住别的进程，挡不住
        # 自己人；进程内串行化由这把互斥负责。等待上限与 flock 共用同一个
        # deadline，所以 timeout=0 的调用点语义不变（立刻失败，用来探测）。
        path_lock = _path_lock(hold_key)
        if not path_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise RuntimeError(f"设备正在使用或初始化，请稍后重试: {path}")

    fd = None
    acquired = False
    owner_pid = os.getpid()
    try:
        if not nested:
            fd = _open_lock_file(path, flags)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RuntimeError(f"设备锁不是普通文件: {path}")
            while True:
                if cancelled is not None and cancelled():
                    raise RuntimeError("设备操作已取消")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"设备正在使用或初始化，请稍后重试: {path}")
                    time.sleep(0.05)
            with _HELD_LOCK:
                _HELD_BY_PROCESS[hold_key] = (ident, 1)
        yield
    finally:
        with _HELD_LOCK:
            current = _HELD_BY_PROCESS.get(hold_key)
            if current is not None:
                if current[1] > 1:
                    _HELD_BY_PROCESS[hold_key] = (current[0], current[1] - 1)
                else:
                    _HELD_BY_PROCESS.pop(hold_key, None)
        if not nested:
            try:
                # O_CLOEXEC does not prevent inheritance by fork-only workers.
                # close(fd) alone leaves flock held by their duplicate descriptors.
                # Only the acquiring process may release the shared lock explicitly.
                if acquired and os.getpid() == owner_pid:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                if fd is not None:
                    os.close(fd)
                path_lock.release()


FAYS_SDK_INITIALIZATION_LOCK = "/tmp/ksq-fays-sdk-serial-probe.lock"


def fays_device_guard_path(stereo_port):
    """Fays 设备 guard 的锁文件路径（调用方用它查本进程是否已持有）。"""
    import re
    if not re.fullmatch(r"/dev/video[0-9]+", str(stereo_port)):
        raise RuntimeError("Fays stereo 节点无效，拒绝访问 SDK")
    return f"/tmp/ksq-fays-sdk-device-{os.path.basename(stereo_port)}.lock"


def fays_device_guard(stereo_port, *, timeout=0.0):
    return device_access_guard(
        fays_device_guard_path(stereo_port),
        timeout=timeout,
    )


def fays_config_device_guard(config_path):
    from core.gripper.fays_runtime import read_fays_config_ports
    return fays_device_guard(read_fays_config_ports(config_path)["stereo_dev_port"])
