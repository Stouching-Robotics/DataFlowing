"""Analyze a stopped bridge's bounded trace; only stdlib required.

ReadFrame includes kernel dequeue wait and SDK row copies. wall - thread CPU
includes blocking and descheduling: it is NOT by itself CPU scheduler wait.
SDK seq is a software counter, not a hardware lost-frame counter.
"""
import argparse
import collections
import json
import statistics
import struct
from pathlib import Path

HEADER = struct.Struct('<8Q')
EVENT = struct.Struct('<5Q4i2I')
MAGIC = 0x4B53514641595331


def distribution(values):
    if not values:
        return {}
    values = sorted(values)
    def percentile(p):
        x = (len(values) - 1) * p
        lo = int(x)
        return values[lo] + (values[min(lo + 1, len(values)-1)] - values[lo]) * (x-lo)
    return {'n': len(values), 'min': values[0], 'median': statistics.median(values),
            'p95': percentile(.95), 'p99': percentile(.99), 'max': values[-1]}


def analyze(path):
    raw = Path(path).read_bytes()
    if len(raw) < HEADER.size:
        raise ValueError('truncated trace header')
    magic, version, capacity, *_ = HEADER.unpack_from(raw)
    if magic != MAGIC or version != 1:
        raise ValueError('unknown trace ABI')
    if len(raw) != HEADER.size + capacity * EVENT.size:
        raise ValueError('truncated or invalid trace file')
    groups = collections.defaultdict(list)
    count = 0
    for offset in range(HEADER.size, len(raw), EVENT.size):
        fields = EVENT.unpack_from(raw, offset)
        if fields[-1] != 1:
            continue
        start, end, cpu, sensor, obj, tid, fd, seq, status, kind, _ = fields
        if kind not in (1, 2, 3) or end < start:
            raise ValueError('invalid committed event')
        groups[(kind, 0 if kind == 3 else obj, tid, fd)].append(fields)
        count += 1
    result = {'path': str(path), 'events': count, 'capacity': capacity,
              'saturated': count == capacity, 'groups': [],
              'notes': ['SDK seq is not a hardware frame counter.',
                        'Non-CPU time includes I/O wait, locking and scheduling.',
                        'Trace is opt-in and has measurement overhead; compare an untraced run.',
                        'Read only after process exit; an interrupted last event may be absent.']}
    for (kind, obj, tid, fd), events in sorted(groups.items()):
        events.sort(key=lambda e: e[0])
        gaps = [(b[0]-a[0])/1e6 for a, b in zip(events, events[1:])]
        good = [e for e in events if e[8] == 1 and e[3] > 0]
        deltas = [(b[3]-a[3])/1e6 for a, b in zip(good, good[1:])]
        row = {'kind': {1: 'read_frame', 2: 'callback', 3: 'dqbuf'}[kind], 'object': hex(obj),
               'tid': tid, 'fd': fd, 'count': len(events),
               'wall_ms': distribution([(e[1]-e[0])/1e6 for e in events]),
               'thread_cpu_ms': distribution([e[2]/1e6 for e in events]),
               'non_cpu_ms': distribution([max(0, e[1]-e[0]-e[2])/1e6 for e in events]),
               'arrival_interval_ms': distribution(gaps),
               'sensor_interval_ms': distribution(deltas),
               'sensor_non_increasing': sum(d <= 0 for d in deltas),
               'sensor_interval_histogram_ms': dict(sorted(collections.Counter(round(d, 2) for d in deltas).items())),
               'failures': sum(e[8] != 1 for e in events)}
        if kind == 3:
            sequence_deltas = [(b[7]-a[7]) & 0xffffffff for a, b in zip(good, good[1:])]
            row['buffer_sequence_gap_events'] = sum(1 < d < 0x80000000 for d in sequence_deltas)
            row['buffer_sequence_missing'] = sum(d-1 for d in sequence_deltas if 1 < d < 0x80000000)
            row['buffer_error_flag_count'] = sum(bool(e[4] & 0x40) for e in good)
            row['buffer_flags_histogram'] = dict(collections.Counter(hex(e[4]) for e in good))
        result['groups'].append(row)
    # The shipped SDK invokes callbacks synchronously in the ReadFrame thread.
    # Attribute gaps without conflating SDK work and application callback work.
    timelines = collections.defaultdict(list)
    for events in groups.values():
        for e in events:
            if e[9] != 3:
                timelines[e[5]].append(e)
    result['chains'] = []
    for tid, events in sorted(timelines.items()):
        events.sort(key=lambda e: e[0])
        after_read, before_read, gap_frames = [], [], []
        previous_callback = None
        for a, b in zip(events, events[1:]):
            if a[9] == 1 and b[9] == 2 and a[8] == 1 and a[3] == b[3]:
                after_read.append((b[0] - a[1]) / 1e6)
            if a[9] == 2 and b[9] == 1:
                before_read.append((b[0] - a[1]) / 1e6)
        for e in events:
            if e[9] != 2 or e[8] != 1:
                continue
            if previous_callback is not None:
                sensor_gap = (e[3] - previous_callback[3]) / 1e6
                if sensor_gap > 30:
                    gap_frames.append({'host_start_ns': e[0], 'sensor_gap_ms': sensor_gap,
                                       'arrival_gap_ms': (e[0] - previous_callback[0]) / 1e6,
                                       'previous_callback_ms': (previous_callback[1]-previous_callback[0])/1e6})
            previous_callback = e
        if after_read or before_read:
            result['chains'].append({'tid': tid,
                                    'sdk_after_read_ms': distribution(after_read),
                                    'sdk_before_read_ms': distribution(before_read),
                                    'callback_sensor_gap_over_30ms_count': len(gap_frames),
                                    'callback_sensor_gap_examples': gap_frames[:20]})
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('trace', nargs='+')
    args = parser.parse_args()
    print(json.dumps([analyze(p) for p in args.trace], indent=2, ensure_ascii=False))
