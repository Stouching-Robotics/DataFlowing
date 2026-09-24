#!/usr/bin/env python3
"""Run isolated dataset tests without changing production affinity or settings."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

def _find_repo_root(start):
    """向上探测 collector 根（含 ``core/gripper/fays_runtime.py`` 的那一层）。

    这里原来写死 ``parents[4] / 'gripper_version1'`` —— 那是本树还活在
    ``online/`` 布局下的路径。搬进 ``core/gripper/orb_slam_src/`` 之后它
    **静默失效**：sys.path 指向一个不存在的目录，紧接着的 import 直接
    ImportError，本套件整套跑不起来。病根是硬编码深度，所以改成向上探测，
    并留一个环境变量覆盖口（沿用 ``KSQ_GRIPPER_NATIVE_ROOT`` 那套做法）。
    """
    override = os.environ.get('KSQ_COLLECTOR_ROOT')
    if override:
        return Path(override).resolve()
    for candidate in (start, *start.parents):
        if (candidate / 'core' / 'gripper' / 'fays_runtime.py').is_file():
            return candidate
    raise SystemExit(
        '找不到 collector 根（含 core/gripper/fays_runtime.py）；'
        '可用 KSQ_COLLECTOR_ROOT 显式指定')


REPO_ROOT = _find_repo_root(Path(__file__).resolve().parent)
sys.path.insert(0, str(REPO_ROOT))
from core.gripper.runtime.cpu_policy import detect_cpu_policy
from core.gripper.fays_runtime import build_fays_runtime_env, ORB_LIBRARY, ORB_VOCABULARY

CASES = {
    'normal_1': ('normal', 0., 0.),
    'normal_2': ('normal', 0., 0.),
    'normal_3': ('normal', 0., 0.),
    'blank_500ms': ('blank_stereo', 40., .5),
    'imu_gap_100ms': ('imu_gap', 40., .1),
    'imu_gap_500ms': ('imu_gap', 40., .5),
    'baseline_normal': ('normal', 0., 0.),
}


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def environment(library):
    policy = detect_cpu_policy()
    env = build_fays_runtime_env()
    for name in ('GOMP_CPU_AFFINITY', 'OMP_PLACES', 'KMP_AFFINITY',
                 'KMP_HW_SUBSET', 'KMP_PLACE_THREADS'):
        env.pop(name, None)
    env.update(OMP_PROC_BIND='FALSE', OMP_DYNAMIC='FALSE',
               OMP_NUM_THREADS=str(len(policy.fays_process)))
    roles = {'INPUT': 'fays_input', 'PREPARE': 'fays_prepare',
             'TRACK': 'fays_track', 'BACKGROUND': 'fays_background',
             'ORB_LEFT': 'fays_left_orb', 'ORB_RIGHT': 'fays_right_orb'}
    for key, attr in roles.items():
        env[f'KSQ_FAYS_{key}_CPU'] = str(getattr(policy, attr)[0])
    env['LD_LIBRARY_PATH'] = str(library.parent) + os.pathsep + env.get('LD_LIBRARY_PATH', '')
    return env


def process_sample(pid):
    result = {}
    try:
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            key, _, value = line.partition(':')
            if key in ('VmRSS', 'VmHWM', 'Threads'):
                result[key] = int(value.strip().split()[0])
    except (OSError, ValueError):
        pass
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--dataset', type=Path, required=True, help='mav0 directory')
    parser.add_argument('--settings', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--library', type=Path, default=Path(ORB_LIBRARY))
    parser.add_argument('--cases', nargs='+', choices=CASES, default=['normal_1'])
    parser.add_argument('--timeout', type=float, default=360)
    parser.add_argument('--accelerated', action='store_true')
    args = parser.parse_args()
    library = args.library.resolve()
    if library.name != 'libORB_SLAM3.so':
        parser.error('Use an isolated directory containing libORB_SLAM3.so (loader SONAME)')
    env = environment(library)
    args.output.mkdir(parents=True, exist_ok=True)
    loader = subprocess.run(['ldd', str(args.binary.resolve())], env=env,
                            capture_output=True, text=True, check=True)
    if str(library) not in loader.stdout or 'not found' in loader.stdout + loader.stderr:
        raise RuntimeError('Test must resolve exactly the selected ORB library: ' + loader.stdout + loader.stderr)
    metadata = {
        'binary': str(args.binary.resolve()), 'binary_sha256': sha256(args.binary),
        'library': str(library), 'library_sha256': sha256(library),
        'settings': str(args.settings.resolve()), 'settings_sha256': sha256(args.settings),
        'dataset': str(args.dataset.resolve()), 'vocabulary': ORB_VOCABULARY,
        'cpu_environment': {k: v for k, v in env.items() if k.startswith('KSQ_FAYS_') and k.endswith('_CPU')},
        'realtime': not args.accelerated, 'loader': loader.stdout,
    }
    for name in args.cases:
        out = args.output / name
        out.mkdir(exist_ok=False)
        (out / 'provenance.json').write_text(json.dumps(metadata, indent=2))
        mode, start, duration = CASES[name]
        cmd = [str(args.binary.resolve()), ORB_VOCABULARY, str(args.settings.resolve()),
               str(args.dataset.resolve()), str(out.resolve()), mode,
               str(start), str(duration), '0' if args.accelerated else '1']
        print('START', name, flush=True)
        begin = time.monotonic()
        timed_out = False
        with (out / 'native.log').open('w') as log, (out / 'resources.jsonl').open('w') as resources:
            proc = subprocess.Popen(cmd, env=env, cwd=out, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            while proc.poll() is None:
                elapsed = time.monotonic() - begin
                resources.write(json.dumps({'elapsed_s': elapsed, **process_sample(proc.pid)}) + '\n')
                resources.flush()
                if elapsed > args.timeout:
                    timed_out = True
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                    break
                time.sleep(1)
            returncode = proc.wait()
        result = {'case': name, 'returncode': returncode, 'timed_out': timed_out,
                  'wall_s': time.monotonic() - begin,
                  'clean_completion': (out / 'completed.txt').is_file() and returncode == 0,
                  'mode': mode, 'fault_start_s': start, 'fault_duration_s': duration}
        (out / 'process_result.json').write_text(json.dumps(result, indent=2))
        print('FINISH', json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
