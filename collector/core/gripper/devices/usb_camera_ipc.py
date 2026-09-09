"""Minimal Python consumer for the camera-service Unix-socket prototype.

The public shape intentionally follows ``cv2.VideoCapture``:

    cam = Camera("sightac_left")
    cam.open()
    ok, frame = cam.read()

The service sends a fixed-size header followed by one MJPEG payload over a
Unix byte stream. This module reconstructs one complete frame and does not
open a V4L2 node.
"""

from __future__ import annotations

import socket
import struct
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np


IPC_MAGIC = 0x4B535146  # "KSQF"
IPC_VERSION = 1
# The C producer uses the native-aligned struct: uint64_t is aligned to
# 8 bytes, so there are four padding bytes before timestamp_ns.
HEADER = struct.Struct("<IHHIIIII4xQ")
MAX_PAYLOAD = 16 * 1024 * 1024
MAX_DECODE_ATTEMPTS = 5
IPC_RECONNECT_ATTEMPTS = 3

DEFAULT_SOCKETS = {
    "sightac_left": "/tmp/ksq-camera-service-sightac-left.sock",
    "sightac_right": "/tmp/ksq-camera-service-sightac-right.sock",
    "decxin": "/tmp/ksq-camera-service-decxin.sock",
    "unit_b_sightac_left": "/tmp/ksq-camera-service-unit-b-sightac-left.sock",
    "unit_b_sightac_right": "/tmp/ksq-camera-service-unit-b-sightac-right.sock",
    "unit_b_decxin": "/tmp/ksq-camera-service-unit-b-decxin.sock",
}


class Camera:
    """Receive MJPEG frames from one camera-service Unix socket."""

    def __init__(self, name_or_path: str, timeout: Optional[float] = 2.0):
        self.name = name_or_path
        self.socket_path = DEFAULT_SOCKETS.get(name_or_path, name_or_path)
        self.timeout = timeout
        self._socket: Optional[socket.socket] = None
        self.last_metadata = {}
        self.last_decode_failures = 0
        self.last_decode_diagnostics = ()
        self.reconnect_count = 0
        self.last_transport_error = ""
        self._released = False

    def open(self) -> bool:
        if self._socket is not None:
            return True
        if self._released:
            return False
        if not Path(self.socket_path).exists():
            raise FileNotFoundError(self.socket_path)
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if self.timeout is not None:
            connection.settimeout(self.timeout)
        try:
            connection.connect(self.socket_path)
        except BaseException:
            connection.close()
            raise
        if self._released:
            connection.close()
            return False
        self._socket = connection
        return True

    def isOpened(self) -> bool:
        """Match the small ``cv2.VideoCapture`` status API used by the app."""
        return self._socket is not None

    def reconnect(self) -> bool:
        """Close and recreate one client lease after a transport failure."""
        self.close_socket()
        for _attempt in range(IPC_RECONNECT_ATTEMPTS):
            if self._released:
                return False
            try:
                if not self.open() or self._released:
                    self.close_socket()
                    return False
                self.reconnect_count += 1
                self.last_transport_error = ""
                return True
            except (OSError, FileNotFoundError) as exc:
                self.last_transport_error = str(exc)
                self.close_socket()
                time.sleep(0.05)
        return False

    def getBackendName(self) -> str:
        return "libuvc-ipc"

    def set(self, _property, _value) -> bool:
        """The UVC mode is negotiated by camera-service, not V4L2."""
        return False

    def get(self, property_id):
        """Return negotiated metadata where the IPC protocol can provide it."""
        if property_id == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self.last_metadata.get("width", 0))
        if property_id == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self.last_metadata.get("height", 0))
        if property_id == cv2.CAP_PROP_FPS:
            return 30.0
        if property_id == cv2.CAP_PROP_FOURCC:
            return float(cv2.VideoWriter_fourcc(*"MJPG"))
        return 0.0

    def _recv_exact(self, size: int):
        # Capture the socket once: release() may clear the owner's reference
        # while a blocking read is in progress.  recv_into avoids accumulating
        # and then copying each individual TCP/Unix stream fragment.
        connection = self._socket
        if connection is None:
            raise ConnectionResetError("camera IPC socket is closed")
        buffer = bytearray(size)
        view = memoryview(buffer)
        offset = 0
        while offset < size:
            count = connection.recv_into(view[offset:])
            if not count:
                raise ConnectionResetError("camera-service closed an incomplete packet")
            offset += count
        return buffer

    def close_socket(self) -> None:
        socket_obj = self._socket
        self._socket = None
        if socket_obj is not None:
            try:
                socket_obj.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                socket_obj.close()
            except OSError:
                pass

    def read_packet(self):
        if self._socket is None:
            if self._released or not self.reconnect():
                return False, None
        try:
            header = self._recv_exact(HEADER.size)
            values = HEADER.unpack(header)
            (magic, version, header_bytes, sequence, width, height,
             fourcc, size, timestamp_ns) = values
            if (magic != IPC_MAGIC or version != IPC_VERSION
                    or header_bytes != HEADER.size):
                raise ValueError("invalid camera-service IPC header")
            if fourcc != int.from_bytes(b"MJPG", "little"):
                raise ValueError("camera-service did not negotiate MJPG")
            if not (0 < size <= MAX_PAYLOAD and 0 < width <= 8192
                    and 0 < height <= 8192 and timestamp_ns > 0):
                raise ValueError("invalid camera-service frame dimensions/size/timestamp")
            payload = self._recv_exact(size)
        except (OSError, ValueError, struct.error) as exc:
            # A timeout can occur in the middle of either header or payload.
            # Continuing on that socket would interpret JPEG bytes as a header.
            # Report this failed read; only a later read may create a new lease.
            self.last_transport_error = str(exc)
            self.close_socket()
            self.last_metadata = {}
            return False, None
        if not payload.startswith(b"\xff\xd8") or payload.rfind(b"\xff\xd9") < 2:
            self.last_transport_error = "incomplete MJPEG payload"
            self.last_metadata = {}
            return False, None
        self.last_transport_error = ""
        self.last_metadata = {
            "sequence": sequence,
            "width": width,
            "height": height,
            "fourcc": "MJPG",
            "timestamp_ns": timestamp_ns,
            "payload_bytes": size,
        }
        return True, payload

    def read(self):
        """Return ``(ret, BGR frame)`` like ``cv2.VideoCapture.read()``."""
        # USB startup can deliver an occasional incomplete JPEG before the
        # source settles.  Match V4L2 semantics: report read failure instead
        # of killing initialization on the first undecodable packet.  A hard
        # error remains if several consecutive payloads cannot be decoded.
        failures = []
        for _attempt in range(MAX_DECODE_ATTEMPTS):
            ok, payload = self.read_packet()
            if not ok:
                return False, None
            encoded = np.frombuffer(payload, dtype=np.uint8)
            try:
                frame = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
            except cv2.error:
                frame = None
            if (frame is not None and frame.size > 0
                    and frame.shape[:2] == (self.last_metadata["height"],
                                            self.last_metadata["width"])):
                self.last_decode_failures = len(failures)
                self.last_decode_diagnostics = tuple(failures)
                return True, frame
            metadata = dict(self.last_metadata)
            failures.append({
                "sequence": metadata.get("sequence"),
                "bytes": len(payload),
                "head": bytes(payload[:4]).hex(),
                "tail": bytes(payload[-4:]).hex(),
            })
            print(
                "[IpcCamera] partial/truncated MJPEG payload "
                f"socket={self.socket_path} attempt={_attempt + 1} "
                f"sequence={metadata.get('sequence')} bytes={len(payload)} "
                f"head={failures[-1]['head']} tail={failures[-1]['tail']} "
                f"transport_error={self.last_transport_error!r}",
                flush=True,
            )
        self.last_decode_failures = len(failures)
        self.last_decode_diagnostics = tuple(failures)
        if failures:
            print(
                "[IpcCamera] MJPEG decode failed "
                f"socket={self.socket_path} "
                f"failures={len(failures)} "
                f"transport_error={self.last_transport_error!r} "
                f"details={failures}",
                flush=True,
            )
        return False, None

    def close(self) -> None:
        self._released = True
        self.close_socket()

    release = close

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
