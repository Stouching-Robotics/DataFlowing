#!/usr/bin/env python3
"""Hardware-free Fays codec roundtrip / SLAM comparison with recorded timestamps.

Only NVIDIA's codec engines are exercised; no camera/SDK/serial access. Input
stream.bin and IMU are never modified. All artifacts go to an explicit output.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import sys
import time

import cv2
import numpy as np
import yaml

PROJECT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT / 'gripper_version1'))
from runtime.cpu_policy import detect_cpu_policy
from run_suite import environment
from fays_runtime import ORB_LIBRARY, ORB_VOCABULARY

HEADER = struct.Struct('<6I3Q4i2hi2I')
IMU = struct.Struct('<6d')
W, H = 640, 400
CASES = {'soft_h264': ('libx264', 'h264', False),
         'hard_h264': ('h264_nvenc', 'h264', True),
         'soft_h265': ('libx265', 'hevc', False),
         'hard_h265': ('hevc_nvenc', 'hevc', True)}


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False))
    os.replace(temporary, path)


def compare_pixels(a, b, frames):
    shape = (frames, H, W)
    expected = int(np.prod(shape))
    if Path(a).stat().st_size != expected or Path(b).stat().st_size != expected:
        raise ValueError('decoded frame count / byte size mismatch')
    left, right = np.memmap(a, dtype=np.uint8, mode='r', shape=shape), np.memmap(b, dtype=np.uint8, mode='r', shape=shape)
    unequal_frames = unequal_pixels = absolute = squared = maximum = 0
    for start in range(0, frames, 8):
        d = left[start:start+8].astype(np.int16) - right[start:start+8].astype(np.int16)
        unequal_frames += int(np.any(d != 0, axis=(1, 2)).sum())
        unequal_pixels += int(np.count_nonzero(d))
        absolute += int(np.abs(d).sum())
        squared += int(np.square(d.astype(np.int32)).sum())
        maximum = max(maximum, int(np.abs(d).max()))
    mse = squared / expected
    return {'frames': frames, 'exact_equal': unequal_pixels == 0,
            'unequal_frames': unequal_frames, 'unequal_pixels': unequal_pixels,
            'unequal_pixel_ratio': unequal_pixels / expected,
            'mae': absolute / expected, 'mse': mse, 'max_abs_difference': maximum,
            'psnr_db': 10 * math.log10(255**2 / mse) if mse else None,
            'psnr_infinite': mse == 0, 'sha256_a': sha(a), 'sha256_b': sha(b)}


def alignment(estimated, reference, scale_correct):
    x, y = estimated - estimated.mean(axis=0), reference - reference.mean(axis=0)
    u, sv, vt = np.linalg.svd(x.T @ y)
    d = np.ones(3)
    d[-1] = np.linalg.det(vt.T @ u.T)
    rotation = vt.T @ np.diag(d) @ u.T
    variance = np.square(x).sum()
    if variance < 1e-12:
        raise ValueError('trajectory has too little movement for meaningful scale estimation')
    scale = float((sv * d).sum() / variance)
    aligned = (scale if scale_correct else 1.) * x @ rotation.T + reference.mean(axis=0)
    error = np.linalg.norm(aligned - reference, axis=1)
    return {'scale': scale, 'APE_Mean': float(error.mean()),
            'APE_RMSE': float(np.sqrt(np.mean(error**2))), 'APE_Max': float(error.max()),
            'matched_poses': len(error), 'alignment': 'Sim3' if scale_correct else 'SE3'}


def replay_metrics(destination, expected_frames):
    """Keep full-replay evidence separate from clean process termination.

    A shutdown failure must not be marked successful. Flushed online poses may
    still be evaluated when all original frames were processed and logged.
    """
    log = (destination / 'native.log').read_text(errors='replace')
    path = destination / 'frames.csv'
    if not path.exists():
        return {'replay_complete': False}
    metrics = np.genfromtxt(path, delimiter=',', names=True, ndmin=1)
    complete = (len(metrics) == expected_frames
                and np.array_equal(metrics['index'], np.arange(expected_frames))
                and np.all(np.diff(metrics['timestamp_ns']) > 0)
                and f'total={expected_frames} injected_frames=0' in log
                and 'REPLAY_FINISHED' in log)
    result = {'replay_complete': bool(complete), 'processed_frames': len(metrics),
              'tracking_ok_frames': int(np.sum(metrics['state'] == 2)),
              'tracking_ok_ratio': float(np.mean(metrics['state'] == 2)) if len(metrics) else 0.,
              'map_reset_messages': log.count('Creation of new map') + log.count('Reset map')}
    if len(metrics):
        result['max_rss_mib'] = float(np.max(metrics['max_rss_kib'])) / 1024
    return result


class Benchmark:
    def __init__(self, args):
        self.args = args
        self.out = args.output.resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.policy = detect_cpu_policy()
        # Resolve the existing role policy before constraining this helper to
        # the general cores; rediscovery from a restricted mask would fail.
        self.slam_env = environment(Path(ORB_LIBRARY))
        os.sched_setaffinity(0, self.policy.general)
        cv2.setNumThreads(1)
        self.report = {'reference': 'raw-image SLAM, not ground truth',
                       'quality': args.quality, 'cases': {}, 'pixel_comparisons': {}}
        self.commands = self.out / 'commands.jsonl'

    def status(self, stage, **kw):
        value = {'time': time.time(), 'stage': stage, **kw}
        save(self.out / 'status.json', value)
        print(json.dumps(value, ensure_ascii=False), flush=True)

    def run(self, command, log, timeout=600, env=None):
        with self.commands.open('a') as f:
            f.write(json.dumps({'time': time.time(), 'command': [str(x) for x in command], 'log': str(log)}) + '\n')
        start = time.monotonic()
        with log.open('w') as stream:
            proc = subprocess.Popen([str(x) for x in command], stdout=stream, stderr=subprocess.STDOUT,
                                    env=env, start_new_session=True)
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                raise RuntimeError(f'timeout: {log}')
        if rc:
            raise RuntimeError(f'exit={rc}: {log}; {log.read_text(errors="replace")[-1200:]}')
        return time.monotonic() - start

    def prepare(self):
        self.status('extract_raw_and_validate_calibration')
        raw = self.out / 'raw'
        raw.mkdir(exist_ok=True)
        timestamps, imu, counts = [], [], {1: 0, 2: 0}
        with (self.args.input / 'stream.bin').open('rb') as source, \
             (raw / 'left.gray').open('wb') as lf, (raw / 'right.gray').open('wb') as rf:
            while h := source.read(HEADER.size):
                if len(h) != HEADER.size:
                    raise ValueError('truncated raw header')
                v = HEADER.unpack(h)
                if v[:2] != (0x53544F55, 1) or v[3] != 80 or v[2] not in counts:
                    raise ValueError('unknown raw packet schema')
                payload = source.read(v[4])
                if len(payload) != v[4]:
                    raise ValueError('truncated payload')
                counts[v[2]] += 1
                if v[2] == 1:
                    if (v[10], v[11], v[12], v[15]) != (W, 2*H, 1, W):
                        raise ValueError(f'unexpected stereo shape/stride: {v}')
                    timestamps.append(v[6])
                    lf.write(payload[:W*H]); rf.write(payload[W*H:])
                else:
                    imu.append((v[6], *IMU.unpack(payload)))
        self.timestamps = np.array(timestamps, dtype=np.int64)
        self.frames = len(timestamps)
        if np.any(np.diff(self.timestamps) <= 0):
            raise ValueError('non-monotonic stereo timestamps')
        calib = yaml.safe_load(self.args.calibration.read_text())
        cams = [calib['cam0'], calib['cam1']]
        ks = [np.array([[c['intrinsics'][0], 0, c['intrinsics'][2]],
                        [0, c['intrinsics'][1], c['intrinsics'][3]], [0, 0, 1]], dtype=np.float64) for c in cams]
        ds = [np.array(c['distortion_coeffs'], dtype=np.float64) for c in cams]
        transform = np.array(cams[1]['T_cn_cnm1'])
        r1, r2, p1, p2, q = cv2.fisheye.stereoRectify(ks[0], ds[0], ks[1], ds[1], (W, H),
                                                     transform[:3, :3], transform[:3, 3], flags=cv2.CALIB_ZERO_DISPARITY)
        self.maps = [cv2.fisheye.initUndistortRectifyMap(k, d, r, p, (W, H), cv2.CV_32FC1)
                     for k, d, r, p in zip(ks, ds, (r1, r2), (p1, p2))]
        recorded = json.loads((self.args.input / 'meta.json').read_text())
        self.settings = self.out / 'recorded_settings.yaml'
        shutil.copy2(recorded['fays_yaml'], self.settings)
        fs = cv2.FileStorage(str(self.settings), cv2.FILE_STORAGE_READ)
        values = [fs.getNode(k).real() for k in ('Camera1.fx', 'Camera1.fy', 'Camera1.cx', 'Camera1.cy')]
        expected = [p1[0, 0], p1[1, 1], p1[0, 2], p1[1, 2]]
        if not np.allclose(values, expected, atol=1e-4):
            raise ValueError(f'recorded intrinsics do not match rectification: {values} vs {expected}')
        rt = np.eye(4); rt[:3, :3] = r1
        tbc = np.linalg.inv(np.array(cams[0]['T_cam_imu'])) @ np.linalg.inv(rt)
        if not np.allclose(fs.getNode('IMU.T_b_c1').mat(), tbc, atol=1e-5):
            raise ValueError('recorded IMU extrinsics do not match rectification')
        fs.release()
        shift = round(cams[0]['timeshift_cam_imu'] * 1e9)
        inertial = self.out / 'imu.csv'
        with inertial.open('w') as f:
            f.write('#timestamp [ns],w_x,w_y,w_z,a_x,a_y,a_z\n')
            for ns, ax, ay, az, gx, gy, gz in imu:
                f.write(f'{ns-shift},{gx:.17g},{gy:.17g},{gz:.17g},{ax:.17g},{ay:.17g},{az:.17g}\n')
        its = np.array([r[0]-shift for r in imu])
        if np.any(np.diff(its) <= 0) or its[0] > timestamps[0] or its[-1] < timestamps[-1]:
            raise ValueError('IMU does not cover all images')
        self.report['input'] = {'frames': self.frames, 'imu_samples': len(imu),
                                'duration_sensor_s': (timestamps[-1]-timestamps[0])/1e9,
                                'imu_timeshift_ns_subtracted': shift, 'width': W, 'height_per_eye': H,
                                'ground_truth_available': False}
        self.report['provenance'] = {'stream_sha256': sha(self.args.input / 'stream.bin'),
                                     'calibration_sha256': sha(self.args.calibration),
                                     'settings_sha256': sha(self.settings), 'binary_sha256': sha(self.args.binary),
                                     'orb_library': str(ORB_LIBRARY), 'orb_library_sha256': sha(ORB_LIBRARY),
                                     'cpu_policy': repr(self.policy), 'clahe': False,
                                     'note': 'compression before rectification; original image/IMU sensor timestamps; no second frame selection'}
        save(self.out / 'provenance.json', self.report)
        self.materialize('raw', raw / 'left.gray', raw / 'right.gray')

    def materialize(self, name, left, right):
        self.status('rectify_decoded_images', case=name)
        root = self.out / name / 'mav0'
        (root / 'imu0').mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.out / 'imu.csv', root / 'imu0/data.csv')
        for side, path in enumerate((left, right)):
            if path.stat().st_size != self.frames * W * H:
                raise ValueError('frame count changed during codec roundtrip')
            target = root / f'cam{side}' / 'data'
            target.mkdir(parents=True, exist_ok=True)
            images = np.memmap(path, dtype=np.uint8, mode='r', shape=(self.frames, H, W))
            with (target.parent / 'data.csv').open('w') as f:
                f.write('#timestamp [ns],filename\n')
                for i, ns in enumerate(self.timestamps):
                    filename = f'{i:06d}.png'
                    image = cv2.remap(images[i], *self.maps[side], interpolation=cv2.INTER_LINEAR)
                    if not cv2.imwrite(str(target / filename), image, [cv2.IMWRITE_PNG_COMPRESSION, 1]):
                        raise RuntimeError('failed to write replay image')
                    f.write(f'{ns},{filename}\n')
        return root

    def slam(self, name, data_name=None):
        self.status('slam_running', case=name)
        destination = self.out / name / 'slam'
        destination.mkdir(parents=True, exist_ok=True)
        env = dict(self.slam_env)
        env['KSQ_OFFLINE_NO_CLAHE'] = '1'
        command = [self.args.binary, ORB_VOCABULARY, self.settings,
                   self.out / (data_name or name) / 'mav0', destination, 'normal', '0', '0', '1']
        case = self.report['cases'].setdefault(name, {})
        try:
            case['slam_wall_s'] = self.run(command, destination / 'native.log', timeout=240, env=env)
            if not (destination / 'completed.txt').exists():
                raise RuntimeError('missing clean completion marker')
            metrics = np.genfromtxt(destination / 'frames.csv', delimiter=',', names=True)
            case['slam_success'] = True
            case['processed_frames'] = len(metrics)
            case['tracking_ok_frames'] = int(np.sum(metrics['state'] == 2))
            case['tracking_ok_ratio'] = float(np.mean(metrics['state'] == 2))
            case['max_rss_mib'] = float(np.max(metrics['max_rss_kib'])) / 1024
            log = (destination / 'native.log').read_text(errors='replace')
            case['map_reset_messages'] = log.count('Creation of new map') + log.count('Reset map')
        except Exception as exc:
            case['slam_success'] = False
            case['error'] = str(exc)
        case.update(replay_metrics(destination, self.frames))
        save(self.out / 'results.json', self.report)

    def codec(self, name):
        encoder, codec, hardware = CASES[name]
        root = self.out / name
        root.mkdir(exist_ok=True)
        q = self.args.quality
        self.report['cases'][name] = {'encoder': encoder, 'quality_parameter': 'CQ' if hardware else 'CRF',
                                      'quality_value': q, 'decoder_for_slam': 'NVDEC' if hardware else 'software'}
        total_video_bytes = 0
        for eye in ('left', 'right'):
            original = self.out / 'raw' / f'{eye}.gray'
            video = root / f'{eye}.mp4'
            self.status('encoding', case=name, eye=eye, quality=q)
            options = (['-preset', 'p4', '-rc', 'vbr', '-cq', str(q), '-b:v', '0'] if hardware else
                       ['-preset', 'medium', '-crf', str(q)])
            if encoder == 'libx265':
                options += ['-x265-params', 'pools=2:frame-threads=2:log-level=error']
            cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'info', '-threads', '2',
                   '-f', 'rawvideo', '-pixel_format', 'gray', '-video_size', f'{W}x{H}',
                   '-framerate', '30', '-i', original, '-an', '-c:v', encoder,
                   *options, '-threads', '2', '-g', '60', '-pix_fmt', 'yuv420p', '-color_range', 'tv', '-y', video]
            encoding_s = self.run(cmd, root / f'{eye}.encode.log')
            total_video_bytes += video.stat().st_size
            decode_times = {}
            for mode in ('software', 'hardware'):
                self.status('decoding', case=name, eye=eye, decoder=mode)
                if mode == 'hardware':
                    before = ['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda', '-c:v', codec + '_cuvid']
                    filters = 'hwdownload,format=nv12,format=gray'
                else:
                    before = ['-c:v', codec]
                    filters = 'format=nv12,format=gray'
                command = ['ffmpeg', '-hide_banner', '-loglevel', 'verbose', '-threads', '2', *before,
                           '-i', video, '-an', '-vf', filters, '-vsync', '0', '-pix_fmt', 'gray',
                           '-f', 'rawvideo', '-y', root / f'{eye}.{mode}.gray']
                decode_times[mode] = self.run(command, root / f'{eye}.decode_{mode}.log')
            sw, hw = root / f'{eye}.software.gray', root / f'{eye}.hardware.gray'
            self.report['pixel_comparisons'][f'{name}/{eye}/same_bitstream_decoders'] = compare_pixels(sw, hw, self.frames)
            selected = hw if hardware else sw
            self.report['pixel_comparisons'][f'{name}/{eye}/vs_raw'] = compare_pixels(original, selected, self.frames)
            self.report['cases'][name][eye] = {'encoding_s': encoding_s, 'decoding_s': decode_times,
                                             'video_bytes': video.stat().st_size, 'video_sha256': sha(video)}
            save(self.out / 'results.json', self.report)
        self.report['cases'][name]['video_total_bytes'] = total_video_bytes
        mode = 'hardware' if hardware else 'software'
        self.materialize(name, root / f'left.{mode}.gray', root / f'right.{mode}.gray')

    def evaluate(self):
        self.status('evaluate_trajectories')
        for codec in ('h264', 'h265'):
            for eye in ('left', 'right'):
                a = self.out / ('soft_' + codec) / f'{eye}.software.gray'
                b = self.out / ('hard_' + codec) / f'{eye}.hardware.gray'
                if a.exists() and b.exists():
                    self.report['pixel_comparisons'][f'{codec}/{eye}/software_vs_hardware_roundtrip'] = compare_pixels(a, b, self.frames)
        for leaf, timestamp_scale in [('optimized_body_ns.txt', 1e9), ('online_body.txt', 1.)]:
            trajectories = {}
            for name, case in self.report['cases'].items():
                path = self.out / name / 'slam' / leaf
                eligible = case.get('slam_success') or (leaf == 'online_body.txt' and case.get('replay_complete'))
                if not eligible or not path.exists() or path.stat().st_size == 0:
                    continue
                t = np.loadtxt(path, ndmin=2)
                if leaf == 'online_body.txt' and len(t) != case.get('tracking_ok_frames'):
                    continue
                if len(t) < 3 or not np.all(np.isfinite(t)):
                    continue
                keys = np.rint(t[:, 0] / timestamp_scale * 1e9).astype(np.int64)
                trajectories[name] = {int(k): row[1:4] for k, row in zip(keys, t)}
            if 'raw' not in trajectories:
                self.report.setdefault('evaluation_errors', []).append('raw baseline unavailable: ' + leaf)
                continue
            common = set(trajectories['raw'])
            for name in CASES:
                if name in trajectories:
                    common.intersection_update(trajectories[name])
            if 'raw_repeat' in trajectories:
                common.intersection_update(trajectories['raw_repeat'])
            common = sorted(common)
            self.report.setdefault('comparison_protocol', {})[leaf] = {
                'common_matched_poses': len(common), 'input_frames': self.frames,
                'coverage_ratio': len(common) / self.frames, 'exact_timestamp_match': True,
                'reference': 'raw SLAM body trajectory; not physical ground truth',
                'APE_units': 'm', 'scale_direction': 'multiply compressed trajectory by scale to align to raw'}
            for name, traj in trajectories.items():
                keys = common
                if len(keys) < 3:
                    continue
                est = np.array([traj[k] for k in keys])
                ref = np.array([trajectories['raw'][k] for k in keys])
                try:
                    self.report['cases'][name].setdefault('evaluation', {})[leaf] = {
                        'sim3': alignment(est, ref, True), 'se3': alignment(est, ref, False)}
                except ValueError as exc:
                    self.report['cases'][name].setdefault('evaluation_errors', []).append(str(exc))
        save(self.out / 'results.json', self.report)
        self.status('complete', results=str(self.out / 'results.json'))

    def execute(self):
        self.prepare()
        self.slam('raw')
        for name in CASES:
            try:
                self.codec(name)
                self.slam(name)
            except Exception as exc:
                self.report['cases'].setdefault(name, {})['error'] = str(exc)
                self.report['cases'][name]['slam_success'] = False
                save(self.out / 'results.json', self.report)
                self.status('case_failed', case=name, error=str(exc))
        self.slam('raw_repeat', data_name='raw')
        self.evaluate()


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--input', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--calibration', type=Path, required=True)
    p.add_argument('--binary', type=Path, required=True)
    p.add_argument('--quality', type=int, default=40)
    a = p.parse_args()
    if (a.output / 'provenance.json').exists():
        raise SystemExit('Refusing to overwrite an existing benchmark; use a new output directory')
    Benchmark(a).execute()
