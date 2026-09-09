"""Bounded pipe-message reads for a single parent consumer on Linux/CPython.

multiprocessing.Queue.get(timeout) times out before recv_bytes, not during it.
An exiting producer can leave a partial frame. This adapter preserves partial
frames across timeouts and never enters Connection's blocking recv_bytes.

Own this queue's read end exclusively: do not mix with Queue.get or another
reader. Queue capacity is returned exactly once after receiving a whole frame.
The CPython-private queue access is deliberately isolated in this module.
"""
import os
import queue
import select
import struct
import time
from multiprocessing.reduction import ForkingPickler


class InterruptibleQueueReader:
    MAX_MESSAGE_BYTES = 16 * 1024 * 1024

    def __init__(self, channel):
        self.channel = channel
        self.fd = channel._reader.fileno()
        os.set_blocking(self.fd, False)
        self.buffer = bytearray()
        self.header_size = 4
        self.payload_size = None

    def get(self, timeout=0.05, *, cancelled=lambda: False):
        deadline = time.monotonic() + max(0.0, timeout)
        if not self.channel._rlock.acquire(True, max(0.0, timeout)):
            raise queue.Empty
        try:
            while True:
                if cancelled():
                    raise queue.Empty
                if self.payload_size is None and len(self.buffer) >= self.header_size:
                    if self.header_size == 4:
                        size, = struct.unpack('!i', self.buffer[:4])
                        if size == -1:
                            self.header_size = 12
                            continue
                    else:
                        size, = struct.unpack('!Q', self.buffer[4:12])
                    if size < 0 or size > self.MAX_MESSAGE_BYTES:
                        raise ValueError(f'Invalid IPC message length: {size}')
                    self.payload_size = size
                target = self.header_size + (self.payload_size or 0)
                if self.payload_size is not None and len(self.buffer) == target:
                    payload = self.buffer[self.header_size:]
                    self.buffer = bytearray()
                    self.header_size = 4
                    self.payload_size = None
                    self.channel._sem.release()
                    return ForkingPickler.loads(payload)
                remaining = max(0.0, deadline - time.monotonic())
                ready, _, _ = select.select([self.fd], [], [], min(.02, remaining))
                if ready:
                    try:
                        chunk = os.read(self.fd, min(65536, target - len(self.buffer)))
                    except BlockingIOError:
                        chunk = None
                    if chunk == b'':
                        raise EOFError('IPC producer closed during message read')
                    if chunk:
                        self.buffer.extend(chunk)
                        # Decode complete messages even if this read used the
                        # final instant of the deadline; otherwise retain bytes.
                        if len(self.buffer) == target:
                            continue
                if time.monotonic() >= deadline:
                    raise queue.Empty
        finally:
            self.channel._rlock.release()
