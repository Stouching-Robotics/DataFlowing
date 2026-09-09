"""Linux 原生线程 affinity 的唯一写入服务。

锁顺序固定为 ``_state_lock`` → 无其他内部锁。外部回调（日志与 fatal
dispatcher）始终在释放锁后调用，避免关机路径与审计线程交叉死锁。
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import threading
from typing import Callable, Iterable, Optional, Tuple

from .cpu_policy import (
    DEDICATED_THREAD_ROLES,
    CpuPolicySnapshot,
)


AUDIT_INTERVAL_SECONDS = 1.0
AUDITOR_JOIN_TIMEOUT_SECONDS = 1.5

_FAYS_STAGE_ROLES = {
    "fays-input": "fays_input",
    "fays-prep": "fays_prepare",
    "fays-orb-left": "fays_left_orb",
    "fays-orb-right": "fays_right_orb",
    "fays-track": "fays_track",
    "fays-output": "fays_track",
}

# These long-lived ORB workers must be able to use the complete ORB child
# domain. Treating them as a fixed background stage pins both to CPU9 and
# recreates the LocalMapping/TrackStereo contention seen in dual-device runs.
_FAYS_CHILD_DOMAIN_THREADS = {
    "fays-local-map",
    "fays-loop-close",
}


@dataclass(frozen=True)
class ThreadBindingSnapshot:
    tid: int
    role: str
    label: str
    cpus: Tuple[int, ...]


@dataclass(frozen=True)
class ChildDomainSnapshot:
    name: str
    pid: int
    cpus: Tuple[int, ...]
    allow_fays_stage_threads: bool
    stage_cpus: Tuple[Tuple[str, Tuple[int, ...]], ...] = ()


@dataclass(frozen=True)
class ThreadAffinitySnapshot:
    installed: bool
    auditor_running: bool
    faulted: bool
    bindings: Tuple[ThreadBindingSnapshot, ...]
    children: Tuple[ChildDomainSnapshot, ...]


class ThreadAffinityService:
    """应用固定 CPU 角色、登记专用线程并持续审计边界。"""

    def __init__(
        self,
        policy: CpuPolicySnapshot,
        *,
        set_affinity: Optional[Callable[[int, Iterable[int]], None]] = None,
        get_affinity: Optional[Callable[[int], Iterable[int]]] = None,
        list_task_ids: Optional[Callable[[int], Iterable[int]]] = None,
        read_task_name: Optional[Callable[[int, int], str]] = None,
        native_id: Callable[[], int] = threading.get_native_id,
        process_id: Callable[[], int] = os.getpid,
        logger: Callable[[str], None] = print,
        fatal_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        if policy._relaxed:
            policy.validate_relaxed()
        else:
            policy.validate_strict()
        self._policy = policy
        self._set_affinity = (
            set_affinity
            if set_affinity is not None
            else getattr(os, "sched_setaffinity", None)
        )
        self._get_affinity = (
            get_affinity
            if get_affinity is not None
            else getattr(os, "sched_getaffinity", None)
        )
        self._list_task_ids = list_task_ids or self._default_list_task_ids
        self._read_task_name = (
            read_task_name or self._default_read_task_name)
        self._native_id = native_id
        self._process_id = process_id
        self._logger = logger
        self._fatal_callback = fatal_callback

        self._state_lock = threading.RLock()
        self._bindings: dict[int, ThreadBindingSnapshot] = {}
        self._children: dict[str, ChildDomainSnapshot] = {}
        self._reported: set[tuple[int, str]] = set()
        self._installed = False
        self._faulted = False
        self._stop = threading.Event()
        self._auditor_thread: Optional[threading.Thread] = None

    @property
    def policy(self) -> CpuPolicySnapshot:
        return self._policy

    @staticmethod
    def _default_list_task_ids(pid: int) -> Tuple[int, ...]:
        try:
            names = os.listdir(f"/proc/{int(pid)}/task")
        except FileNotFoundError:
            return ()
        return tuple(sorted(
            int(name) for name in names if str(name).isdigit()))

    @staticmethod
    def _default_read_task_name(pid: int, tid: int) -> str:
        try:
            with open(
                f"/proc/{int(pid)}/task/{int(tid)}/comm",
                encoding="utf-8",
            ) as stream:
                return stream.read().strip()
        except (FileNotFoundError, ProcessLookupError):
            return ""

    def _require_api(self) -> None:
        if self._set_affinity is None or self._get_affinity is None:
            raise RuntimeError("Linux thread affinity API is unavailable")

    def snapshot(self) -> ThreadAffinitySnapshot:
        with self._state_lock:
            thread = self._auditor_thread
            return ThreadAffinitySnapshot(
                installed=self._installed,
                auditor_running=bool(
                    thread is not None and thread.is_alive()),
                faulted=self._faulted,
                bindings=tuple(sorted(
                    self._bindings.values(), key=lambda item: item.tid)),
                children=tuple(sorted(
                    self._children.values(), key=lambda item: item.name)),
            )

    def _apply_exact(self, tid: int, cpus: Iterable[int]) -> Tuple[int, ...]:
        self._require_api()
        target = {int(cpu) for cpu in cpus}
        if not target:
            raise RuntimeError(f"empty CPU affinity for tid={int(tid)}")
        assert self._set_affinity is not None
        assert self._get_affinity is not None
        self._set_affinity(int(tid), target)
        actual = tuple(sorted(int(cpu) for cpu in self._get_affinity(int(tid))))
        expected = tuple(sorted(target))
        if actual != expected:
            raise RuntimeError(
                f"tid={int(tid)} affinity verification failed: "
                f"expected={list(expected)} actual={list(actual)}"
            )
        return actual

    def install_strict_boundary(self) -> None:
        """在任何应用 worker 创建前把已有原生线程收进普通线程域。"""

        self._require_api()
        general = self._policy.general
        pid = self._process_id()
        try:
            tids = tuple(int(tid) for tid in self._list_task_ids(pid))
        except (OSError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"cannot enumerate startup threads: {exc}") from exc
        if not tids:
            raise RuntimeError("cannot enumerate startup threads: empty task set")

        for tid in tids:
            try:
                self._apply_exact(tid, general)
            except ProcessLookupError:
                continue
            except (OSError, ValueError) as exc:
                raise RuntimeError(
                    f"cannot isolate startup thread tid={tid}: {exc}"
                ) from exc

        with self._state_lock:
            self._installed = True
        self._logger(
            "[CPU-ISOLATION] default boundary verified: "
            f"reserved-only={self._policy.reserved} "
            f"general-only={self._policy.general}"
        )

    def set_xhci_irq_affinity_to_general(self) -> bool:
        """Pin xHCI interrupt handling to the shared general CPU domain."""
        general_text = ",".join(str(cpu) for cpu in self._policy.general)
        changed = 0
        try:
            interrupt_lines = Path("/proc/interrupts").read_text(
                encoding="utf-8").splitlines()
            for line in interrupt_lines:
                irq_text, separator, _description = line.partition(":")
                if not separator or "xhci_hcd" not in line:
                    continue
                try:
                    irq = int(irq_text.strip())
                except ValueError:
                    continue
                try:
                    Path(f"/proc/irq/{irq}/smp_affinity_list").write_text(
                        f"{general_text}\n", encoding="ascii")
                    changed += 1
                except (OSError, PermissionError) as exc:
                    self._logger(
                        "[CPU-ISOLATION] xHCI IRQ affinity FAILED: "
                        f"irq={irq} cpus={general_text}: {exc}"
                    )
        except OSError as exc:
            self._logger(
                "[CPU-ISOLATION] xHCI IRQ scan failed: "
                f"{exc}"
            )
            return False
        self._logger(
            "[CPU-ISOLATION] xHCI IRQ affinity set: "
            f"irqs={changed} cpus={general_text}"
        )
        return changed > 0

    def bind_general_thread(self, label: str) -> bool:
        """把当前普通线程严格绑定到当前策略的普通线程域。"""

        tid = int(self._native_id())
        try:
            self._apply_exact(tid, self._policy.general)
            return True
        except (OSError, RuntimeError, ValueError) as exc:
            self._logger(
                "[CPU-ISOLATION] general bind FAILED: "
                f"thread={label} cpus={self._policy.general}: {exc}"
            )
            return False

    def bind_general_thread_for(
        self, policy: CpuPolicySnapshot, label: str,
    ) -> bool:
        """Bind a thread using a device slot's immutable policy snapshot."""
        tid = int(self._native_id())
        try:
            self._apply_exact(tid, policy.general)
            return True
        except (OSError, RuntimeError, ValueError) as exc:
            self._logger(
                "[CPU-ISOLATION] slot general bind FAILED: "
                f"thread={label} cpus={policy.general}: {exc}"
            )
            return False

    def bind_gui_main_thread(self, cpu: int, label: str = "gui-main") -> bool:
        """绑定并登记 Tk 主线程到指定核；auditor 不再纠回 general。

        后续未登记的工作线程若继承该核，会被 auditor 按 general 域纠回，
        因此 GUI 主线程可以独占该核而不被轻量线程挤占。
        """
        self._require_api()
        tid = int(self._native_id())
        cpus = (int(cpu),)
        binding = ThreadBindingSnapshot(
            tid=tid, role="gui_main", label=label, cpus=cpus)
        with self._state_lock:
            self._bindings[tid] = binding
        try:
            actual = self._apply_exact(tid, cpus)
        except (OSError, RuntimeError, ValueError) as exc:
            with self._state_lock:
                self._bindings.pop(tid, None)
            self._logger(
                "[CPU-ISOLATION] GUI main bind FAILED: "
                f"cpu={cpu}: {exc}"
            )
            return False
        self._logger(
            f"[CPU-ISOLATION] GUI main thread cpu={cpu} verified "
            f"actual={actual}")
        return True

    def bind_dedicated_thread(self, role: str, label: str = "") -> bool:
        """绑定并登记当前专用线程；失败时撤销登记。"""

        role = str(role)
        if role not in DEDICATED_THREAD_ROLES:
            return False
        cpus = self._policy.cpus_for(role)
        tid = int(self._native_id())
        binding = ThreadBindingSnapshot(
            tid=tid,
            role=role,
            label=str(label or threading.current_thread().name),
            cpus=cpus,
        )
        with self._state_lock:
            self._bindings[tid] = binding
        try:
            actual = self._apply_exact(tid, cpus)
        except (OSError, RuntimeError, ValueError) as exc:
            with self._state_lock:
                self._bindings.pop(tid, None)
            self._logger(
                "[CPU-ISOLATION] dedicated bind FAILED: "
                f"role={role} cpus={cpus}: {exc}"
            )
            return False

        report_key = (tid, role)
        with self._state_lock:
            should_report = report_key not in self._reported
            self._reported.add(report_key)
        if should_report:
            scope = "Fays" if role == "fays_input" else "Tactile"
            self._logger(
                f"[{scope}] CPU bound: thread={binding.label} "
                f"role={role} cpus={actual}"
            )
        return True

    def bind_dedicated_thread_for(
        self,
        policy: CpuPolicySnapshot,
        role: str,
        label: str = "",
    ) -> bool:
        """Bind/register a dedicated thread without changing global policy."""
        role = str(role)
        if role not in DEDICATED_THREAD_ROLES:
            return False
        cpus = policy.cpus_for(role)
        tid = int(self._native_id())
        binding = ThreadBindingSnapshot(
            tid=tid,
            role=role,
            label=str(label or threading.current_thread().name),
            cpus=cpus,
        )
        with self._state_lock:
            self._bindings[tid] = binding
        try:
            actual = self._apply_exact(tid, cpus)
        except (OSError, RuntimeError, ValueError) as exc:
            with self._state_lock:
                self._bindings.pop(tid, None)
            self._logger(
                "[CPU-ISOLATION] slot dedicated bind FAILED: "
                f"role={role} cpus={cpus}: {exc}"
            )
            return False
        report_key = (tid, role)
        with self._state_lock:
            should_report = report_key not in self._reported
            self._reported.add(report_key)
        if should_report:
            self._logger(
                f"[CPU-ISOLATION] slot thread bound: "
                f"thread={binding.label} role={role} cpus={actual}"
            )
        return True

    def unbind_current_thread(self) -> None:
        tid = int(self._native_id())
        with self._state_lock:
            self._bindings.pop(tid, None)

    def register_child(
        self,
        name: str,
        pid: int,
        role: str,
        *,
        allow_fays_stage_threads: bool = False,
        cpus: Optional[Iterable[int]] = None,
        stage_policy: Optional[CpuPolicySnapshot] = None,
    ) -> None:
        """登记一个子进程的 CPU 域供审计器维护。

        Fays 子进程的 ``role`` 仍然记录为 ``fays_background``，但其实际
        启动域是全部 Fays stage CPU 的并集。已命名的 native
        输入、预处理、特征提取和跟踪线程由 ``_FAYS_STAGE_ROLES`` 收窄到
        对应 CPU。LocalMapping/LoopClosing 及其他未识别线程保留在子进程
        启动域，避免长期后台计算被压到单个逻辑 CPU。
        """

        child_pid = int(pid)
        if child_pid <= 0:
            raise ValueError(f"invalid child pid: {pid!r}")
        child_cpus = tuple(sorted({
            int(cpu) for cpu in (
                self._policy.cpus_for(role) if cpus is None else cpus
            )
        }))
        if not child_cpus:
            raise ValueError(f"empty child CPU domain for {name!r}")
        if role not in {
            "left_process", "right_process", "fays_background"
        }:
            raise ValueError(f"unsupported child CPU role: {role!r}")
        if allow_fays_stage_threads and role != "fays_background":
            raise ValueError(
                "Fays stage thread routing requires fays_background role")
        registration = ChildDomainSnapshot(
            name=str(name),
            pid=child_pid,
            cpus=child_cpus,
            allow_fays_stage_threads=bool(allow_fays_stage_threads),
            stage_cpus=(
                tuple(
                    (role, tuple(stage_policy.cpus_for(role)))
                    for role in sorted(set(_FAYS_STAGE_ROLES.values()))
                )
                if stage_policy is not None else ()
            ),
        )
        with self._state_lock:
            self._children[registration.name] = registration

    def bind_child_process(
        self,
        name: str,
        pid: int,
        role: str,
        *,
        allow_fays_stage_threads: bool = False,
    ) -> Tuple[int, ...]:
        """Apply and register one child CPU domain through the sole owner."""

        child_pid = int(pid)
        cpus = self._policy.cpus_for(role)
        actual = self._apply_exact(child_pid, cpus)
        self.register_child(
            name,
            child_pid,
            role,
            allow_fays_stage_threads=allow_fays_stage_threads,
        )
        return actual

    def bind_fays_orb_process(
        self, name: str, pid: int,
        policy: Optional[CpuPolicySnapshot] = None,
    ) -> Tuple[int, ...]:
        """Apply the full Fays-stage cpuset to the ORB process.

        The child is registered with the ``fays_background`` role for lifecycle
        bookkeeping, while its audited domain is the union of every Fays
        stage role. Otherwise a worker created by TrackStereo cannot bind to
        its ORB CPU. Native LocalMapping/LoopClosing threads are restored to
        this complete child domain by the affinity auditor.
        """
        child_pid = int(pid)
        selected_policy = policy or self._policy
        cpus = selected_policy.fays_process
        actual = self._apply_exact(child_pid, cpus)
        self.register_child(
            str(name),
            child_pid,
            "fays_background",
            allow_fays_stage_threads=True,
            cpus=cpus,
            stage_policy=selected_policy,
        )
        return actual

    def bind_child_process_for(
        self,
        policy: CpuPolicySnapshot,
        name: str,
        pid: int,
        role: str,
    ) -> Tuple[int, ...]:
        """Apply/register a non-Fays child with one slot's CPU policy."""
        cpus = policy.cpus_for(role)
        actual = self._apply_exact(int(pid), cpus)
        self.register_child(name, int(pid), role, cpus=cpus)
        return actual

    def enable_dual_slam(self, track_cpu: int = 6, input_cpu: int = 5) -> None:
        """双设备模式：第二套 input/fays_input=5、track/output=6；
        其余角色与单设备严格分区一致（general=10/11）。"""
        with self._state_lock:
            self._policy = CpuPolicySnapshot.dual_slam(
                track_cpu=track_cpu,
                input_cpu=input_cpu,
            )

    def enable_balanced_dual_slam(self, *, secondary: bool) -> None:
        """切换到现场验证后的双设备正式性能分区。"""
        with self._state_lock:
            self._policy = CpuPolicySnapshot.balanced_dual_slam(
                secondary=secondary,
            )

    def enable_exclusive_dual_slam(self, *, secondary: bool) -> None:
        """切换到两套 ORB 子进程互不共享物理核的实验分区。"""
        with self._state_lock:
            self._policy = CpuPolicySnapshot.exclusive_dual_slam(
                secondary=secondary,
            )

    def child_affinity(self, pid: int) -> Tuple[int, ...]:
        """Return a child affinity for controller-side post-bind checks."""

        self._require_api()
        assert self._get_affinity is not None
        return tuple(sorted(
            int(cpu) for cpu in self._get_affinity(int(pid))
        ))

    def unregister_child(self, name: str) -> None:
        with self._state_lock:
            self._children.pop(str(name), None)

    def audit_task_group(
        self,
        pid: int,
        *,
        allowed_cpus: Optional[Iterable[int]] = None,
        allow_fays_stage_threads: bool = False,
        stage_cpus: Optional[
            Iterable[Tuple[str, Iterable[int]]]
        ] = None,
    ) -> int:
        """纠正父进程或指定子进程中越过职责边界的线程。"""

        self._require_api()
        process_pid = int(pid)
        own_process = process_pid == int(self._process_id())
        with self._state_lock:
            bindings = dict(self._bindings) if own_process else {}

        general = set(self._policy.general)
        child_domain = {int(cpu) for cpu in (allowed_cpus or ())}
        child_stage_cpus = {
            str(role): {int(cpu) for cpu in cpus}
            for role, cpus in (stage_cpus or ())
        }
        if not own_process and not child_domain:
            raise RuntimeError(
                f"missing CPU domain for child process pid={process_pid}")

        tids = {
            int(tid) for tid in self._list_task_ids(process_pid)
        }
        corrected = 0
        assert self._get_affinity is not None
        for tid in tids:
            expected_binding = bindings.get(tid)
            try:
                actual = {
                    int(cpu) for cpu in self._get_affinity(tid)
                }
            except ProcessLookupError:
                continue

            if expected_binding is not None:
                expected = set(expected_binding.cpus)
                target = expected if actual != expected else None
            elif own_process:
                target = None if actual and actual <= general else general
            elif allow_fays_stage_threads:
                task_name = self._read_task_name(process_pid, tid)
                stage_role = _FAYS_STAGE_ROLES.get(task_name)
                if task_name in _FAYS_CHILD_DOMAIN_THREADS:
                    expected = child_domain
                elif stage_role is not None:
                    expected = child_stage_cpus.get(
                        stage_role,
                        set(self._policy.cpus_for(stage_role)),
                    )
                else:
                    expected = child_domain
                target = None if actual == expected else expected
            else:
                target = None if actual == child_domain else child_domain

            if target is None:
                continue
            try:
                self._apply_exact(tid, target)
            except ProcessLookupError:
                continue
            corrected += 1

        if own_process:
            with self._state_lock:
                for stale_tid in set(self._bindings) - tids:
                    self._bindings.pop(stale_tid, None)
        return corrected

    def audit_once(self) -> int:
        corrected = self.audit_task_group(self._process_id())
        with self._state_lock:
            children = tuple(self._children.values())
        for child in children:
            corrected += self.audit_task_group(
                child.pid,
                allowed_cpus=child.cpus,
                allow_fays_stage_threads=child.allow_fays_stage_threads,
                stage_cpus=child.stage_cpus,
            )
        return corrected

    def _fail(self, reason: str) -> None:
        with self._state_lock:
            if self._faulted:
                return
            self._faulted = True
        message = str(reason)
        self._logger(f"[CPU-ISOLATION] FATAL: {message}")
        if self._fatal_callback is not None:
            self._fatal_callback(message)

    def _audit_worker(self) -> None:
        if not self.bind_general_thread("cpu-boundary-audit"):
            self._fail("cannot bind boundary auditor to general CPUs")
            return
        while not self._stop.is_set():
            try:
                corrected = self.audit_once()
                if corrected:
                    self._logger(
                        "[CPU-ISOLATION] corrected "
                        f"{corrected} thread affinity violation(s)"
                    )
            except (OSError, RuntimeError, ValueError) as exc:
                self._fail(str(exc))
                return
            self._stop.wait(AUDIT_INTERVAL_SECONDS)

    def start_auditor(self) -> None:
        with self._state_lock:
            thread = self._auditor_thread
            if thread is not None and thread.is_alive():
                return
            self._stop.clear()
            thread = threading.Thread(
                target=self._audit_worker,
                daemon=True,
                name="cpu-boundary-audit",
            )
            self._auditor_thread = thread
        thread.start()

    def stop_auditor(
        self,
        timeout: float = AUDITOR_JOIN_TIMEOUT_SECONDS,
    ) -> None:
        self._stop.set()
        with self._state_lock:
            thread = self._auditor_thread
            self._auditor_thread = None
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=float(timeout))
