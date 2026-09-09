"""Cross-process device-operation guards; no SDK calls or affinity changes."""
from contextlib import contextmanager
import fcntl
import os
import stat
import time


@contextmanager
def device_access_guard(path, *, timeout=30.0, cancelled=None):
    # Read-only flock also works for a lock created by a sudo-launched process.
    # Do not truncate, unlink or follow symlinks: every caller must lock the
    # same inode for the whole operation/lease lifetime.
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            fd = os.open(path, flags)
    acquired = False
    owner_pid = os.getpid()
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError(f"设备锁不是普通文件: {path}")
        deadline = time.monotonic() + timeout
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
        yield
    finally:
        try:
            # O_CLOEXEC does not prevent inheritance by fork-only workers.
            # close(fd) alone leaves flock held by their duplicate descriptors.
            # Only the acquiring process may release the shared lock explicitly.
            if acquired and os.getpid() == owner_pid:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


FAYS_SDK_INITIALIZATION_LOCK = "/tmp/ksq-fays-sdk-serial-probe.lock"


def fays_device_guard(stereo_port, *, timeout=0.0):
    import re
    if not re.fullmatch(r"/dev/video[0-9]+", str(stereo_port)):
        raise RuntimeError("Fays stereo 节点无效，拒绝访问 SDK")
    return device_access_guard(
        f"/tmp/ksq-fays-sdk-device-{os.path.basename(stereo_port)}.lock",
        timeout=timeout,
    )


def fays_config_device_guard(config_path):
    from core.gripper.fays_runtime import read_fays_config_ports
    return fays_device_guard(read_fays_config_ports(config_path)["stereo_dev_port"])
