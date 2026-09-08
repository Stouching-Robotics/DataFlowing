"""Browser-compatible video preview cache.

The collector and LeRobot dataset may use HEVC (including 12-bit depth
streams), but the Review page must not depend on the browser being able to
decode those codecs. This module creates a local, derived H.264 preview on
first use and reuses it for subsequent range requests.

The source file is never changed. The preview is a disposable local cache;
the authoritative recording and the lossless metric-depth asset remain in
the configured storage directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from app.config import settings


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

_FASTSTART_CACHE: dict[str, bool] = {}
_FASTSTART_CACHE_LOCK = threading.Lock()


def _cache_root() -> Path:
    root = settings.upload_staging_root / "browser-preview"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cache_key(source: Path) -> str:
    stat = source.stat()
    fingerprint = f"{source.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def _lock_for(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


# ── Local source mirror (depth streams) ──────────────────────────────
# Depth videos live on the SSHFS mount, where random reads for seek/decode
# cost seconds.  A bit-identical local copy turns every later decode into a
# local-disk read.  This is a disposable transport cache of the SOURCE file
# (never a derived/colorized asset); the authoritative stream stays in the
# configured storage directory.

_MIRROR_MAX_BYTES = 8 * 1024 ** 3
_MIRROR_INFLIGHT: set[str] = set()
_MIRROR_INFLIGHT_GUARD = threading.Lock()


def _mirror_root() -> Path:
    root = settings.upload_staging_root / "depth-mirror"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _prune_mirror(keep: Path) -> None:
    """Remove oldest mirror files beyond the byte budget (LRU by mtime)."""
    try:
        files = [p for p in _mirror_root().iterdir()
                 if p.is_file() and p.suffix == ".mp4"]
    except OSError:
        return
    total = sum(p.stat().st_size for p in files if p != keep) + (
        keep.stat().st_size if keep.is_file() else 0)
    if total <= _MIRROR_MAX_BYTES:
        return
    for path in sorted(files, key=lambda p: p.stat().st_mtime_ns):
        if path == keep or total <= _MIRROR_MAX_BYTES:
            continue
        try:
            total -= path.stat().st_size
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _copy_mirror(source: Path, destination: Path, key: str) -> None:
    """Background copy to a temp file, then atomic rename."""
    temporary = destination.with_suffix(".part")
    temporary.unlink(missing_ok=True)
    try:
        shutil.copy2(source, temporary)
        if not temporary.is_file() or temporary.stat().st_size <= 0:
            raise RuntimeError("mirror copy produced an empty file")
        os.replace(temporary, destination)
        _prune_mirror(destination)
    except (OSError, RuntimeError):
        temporary.unlink(missing_ok=True)
    finally:
        with _MIRROR_INFLIGHT_GUARD:
            _MIRROR_INFLIGHT.discard(key)


def _write_turbo_cube(path: Path) -> None:
    """Write the static 4096-entry 1D Turbo cube used by the transcode.

    4096 entries keep the range mapping at 12-bit precision: a 256-entry LUT
    evaluated after an 8-bit intermediate caused visible banding (only ~58
    distinct color levels survived the chain instead of ~173).
    """
    if path.is_file():
        return
    from app.lerobot_v21 import turbo_lut

    lut = turbo_lut(4096)
    lines = ["TITLE Turbo", "LUT_1D_SIZE 4096", "",
             "DOMAIN_MIN 0 0 0", "DOMAIN_MAX 1 1 1", ""]
    for r, g, b in lut:
        lines.append(f"{r / 255.0:.6f} {g / 255.0:.6f} {b / 255.0:.6f}")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def _depth_stats_path(source: Path) -> Path:
    key = _cache_key(source)
    return _cache_root() / f"depth-{key}.stats.json"


def read_depth_stats(source: Path) -> dict | None:
    """Disk-persisted q01/q99 bounds for ``source`` (same fingerprint key).

    Written once by :func:`_ensure_depth_stats` during preview generation;
    survives restarts so neither the stats endpoint nor a re-transcode ever
    pays a second full-stream decode for the same source file.
    """
    try:
        path = _depth_stats_path(source)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if (isinstance(data, dict)
                and isinstance(data.get("q01"), (int, float))
                and isinstance(data.get("q99"), (int, float))
                and float(data["q99"]) > float(data["q01"])):
            return data
    except (OSError, ValueError):
        pass
    return None


def _histogram_depth_stats(source: Path) -> dict | None:
    """Single-decode q01/q99 code bounds via a streaming 4096-bin histogram.

    The stream is read as raw gray12le codes and histogrammed in O(1)
    memory; percentiles are exact over every decoded pixel.  The previous
    ``select=`` sampling pass decoded the whole stream anyway while keeping
    only a fraction of the frames, so nothing is lost.
    """
    import numpy as np

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    hist = np.zeros(4096, dtype=np.int64)
    proc = subprocess.Popen(
        [ffmpeg, "-v", "error", "-i", str(source), "-map", "0:v:0",
         "-f", "rawvideo", "-pix_fmt", "gray12le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while True:
            chunk = proc.stdout.read(8 * 1024 * 1024)
            if not chunk:
                break
            values = np.frombuffer(chunk, dtype="<u2")
            hist += np.bincount(values.astype(np.int64), minlength=4096)[:4096]
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    total = int(hist.sum())
    if total < 2:
        return None
    cumulative = np.cumsum(hist)
    q01 = float(np.searchsorted(cumulative, total * 0.01, side="right"))
    q99 = float(np.searchsorted(cumulative, total * 0.99, side="right"))
    return {"q01": q01, "q99": q99, "total_pixels": total}


def _ensure_depth_stats(source: Path, local: Path | None = None) -> dict | None:
    """Return q01/q99 for ``source``, computed once and persisted to disk.

    ``source`` is the authoritative path used for the cache fingerprint;
    ``local`` (the transport mirror) is decoded when it is ready so the pass
    never competes with an SSHFS random read.
    """
    cached = read_depth_stats(source)
    if cached is not None:
        return cached
    with _lock_for(f"stats:{_cache_key(source)}"):
        cached = read_depth_stats(source)
        if cached is not None:
            return cached
        stats = _histogram_depth_stats(local or source)
        if stats is None:
            return None
        try:
            _depth_stats_path(source).write_text(
                json.dumps(stats), encoding="utf-8")
        except OSError:
            pass
        return stats


def depth_preview_exists(source: Path) -> bool:
    """Cheap cache-hit check for prewarm scheduling (no transcode)."""
    try:
        destination = _cache_root() / f"depth-{_cache_key(Path(source))}.mp4"
        return destination.is_file() and destination.stat().st_size > 0
    except OSError:
        return False


def ensure_depth_h264_preview(source: Path, q01: float = 0.0,
                              q99: float = 4095.0) -> Path:
    """Generate (once) an 8-bit Turbo-colored H.264 preview of a depth stream.

    The browser display pipeline already quantizes codes into an 8-bit Turbo
    ramp; this bakes the identical mapping (same clamp/scale math, same
    colormap) into a disposable local MP4 so the review page plays it through
    a native ``<video>`` element — instant open, native seek, no per-open
    12-bit decode or 229MB raw-code transfer.  The preview never enters the
    dataset; the canonical gray12le stream stays authoritative.
    """
    source = Path(source)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to build depth previews")
    key = _cache_key(source)
    destination = _cache_root() / f"depth-{key}.mp4"
    if destination.is_file() and destination.stat().st_size > 0:
        return destination
    with _lock_for(key):
        if destination.is_file() and destination.stat().st_size > 0:
            return destination
        q01 = float(q01 if q01 is not None else 0.0)
        q99 = float(q99 if q99 is not None else 4095.0)
        span = max(q99 - q01, 1.0)
        # Decode from the local mirror when it is ready; SSHFS random reads
        # during the stats/transcode passes otherwise dominate generation.
        # Wait briefly for the background copy — a ~6s copy is cheaper than
        # decoding the whole stream over the mount.
        original = source
        try:
            source = ensure_local_mirror(source, wait_seconds=45)
        except Exception:
            pass
        if q01 <= 0 and q99 >= 4095:
            # No cached adaptive bounds: compute them once from a single
            # decode (persisted next to the preview, keyed on the
            # authoritative source path) so the ramp matches the former
            # q01/q99 colorization and never re-decodes for the same file.
            stats = _ensure_depth_stats(original, source)
            if stats and float(stats.get("q99") or 0) > float(stats.get("q01") or 0):
                q01 = float(stats["q01"])
                q99 = float(stats["q99"])
            span = max(q99 - q01, 1.0)
        # Map the range in the native 12-bit domain (the lut filter evaluates
        # ``val`` at the input depth), then apply a 4096-entry Turbo cube:
        # quantizing through an 8-bit intermediate produced visible banding.
        cube = _cache_root() / "turbo.cube"
        _write_turbo_cube(cube)
        temporary = destination.with_name(
            f".{destination.stem}.{os.getpid()}.part.mp4")
        temporary.unlink(missing_ok=True)
        try:
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", str(source),
                 "-vf", (f"lut=y='clip((val-{q01:.2f})*4095/{span:.2f},0,4095)',"
                         f"lut1d=file={cube},format=yuv420p"),
                 "-an", "-c:v", "libx264", "-preset", "veryfast",
                 "-crf", "18", "-g", "25", "-keyint_min", "25",
                 "-sc_threshold", "0", "-movflags", "+faststart",
                 str(temporary)],
                check=True, capture_output=True, timeout=3600,
            )
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise RuntimeError("ffmpeg produced an empty depth preview")
            os.replace(temporary, destination)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("depth preview transcode timed out") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode("utf-8", errors="replace")[-500:]
            raise RuntimeError(f"depth preview transcode failed: {detail}") from exc
        finally:
            temporary.unlink(missing_ok=True)
    return destination


def ensure_local_mirror(source: Path, wait_seconds: float = 0.0) -> Path:
    """Return a local copy of ``source`` when ready, else ``source``.

    The first request for a video schedules an asynchronous copy and keeps
    serving from the original path; once the mirror lands (atomic rename),
    every later call — including the next frame of the same request stream —
    returns the local path.  The key includes size+mtime, so a reprocessed
    MP4 silently invalidates the old mirror.

    ``wait_seconds > 0`` polls for the copy to land before falling back to
    the remote path (used by preview generation, whose decode pass is much
    faster from local disk than over SSHFS).
    """
    source = Path(source)
    key = _cache_key(source)
    destination = _mirror_root() / f"{key}.mp4"
    if destination.is_file() and destination.stat().st_size > 0:
        return destination
    with _MIRROR_INFLIGHT_GUARD:
        if key not in _MIRROR_INFLIGHT:
            _MIRROR_INFLIGHT.add(key)
            threading.Thread(
                target=_copy_mirror, args=(source, destination, key),
                daemon=True,
            ).start()
    if wait_seconds > 0:
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if destination.is_file() and destination.stat().st_size > 0:
                return destination
            time.sleep(0.25)
        return destination if destination.is_file() else source
    return source


def _is_h264_yuv420(source: Path) -> bool:
    """Avoid re-encoding an already browser-compatible source."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return False
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,pix_fmt",
             "-of", "csv=p=0", str(source)],
            check=True, capture_output=True, text=True, timeout=15,
        )
        codec, _, pix_fmt = result.stdout.strip().partition(",")
        return codec.lower() == "h264" and pix_fmt in {"yuv420p", "yuvj420p"}
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def _probe_faststart(source: Path) -> bool:
    """moov 在 mdat 之前 = faststart(浏览器可流式播放,否则播放会早停)。

    只读前几个顶层 box 的头部,moov/mdat 先后顺序即可判定;探测失败
    保守返回 True(透传原文件,维持旧行为)。结果按源文件指纹记忆。
    """
    key = _cache_key(source)
    with _FASTSTART_CACHE_LOCK:
        cached = _FASTSTART_CACHE.get(key)
        if cached is not None:
            return cached
    result = True
    try:
        with open(source, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(0)
            for _ in range(8):
                if handle.tell() > size - 8:
                    break
                pos = handle.tell()
                header = handle.read(8)
                if len(header) < 8:
                    break
                length = int.from_bytes(header[:4], "big")
                box_type = header[4:8]
                if length == 1:
                    ext = handle.read(8)
                    length = int.from_bytes(ext, "big") if len(ext) == 8 else 0
                if length == 0:
                    length = size - pos
                if length < 8:
                    break
                if box_type == b"moov":
                    result = True
                    break
                if box_type == b"mdat":
                    result = False
                    break
                handle.seek(pos + length)
    except OSError:
        result = True
    with _FASTSTART_CACHE_LOCK:
        _FASTSTART_CACHE[key] = result
    return result


def _remux_faststart(source: Path, destination: Path) -> None:
    """``-c copy -movflags +faststart`` 容器重排(不重编码,字节级无损)。

    moov 索引移到文件头,浏览器无需跳到文件尾即可开始流式播放。
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for faststart remux")
    temporary = destination.with_name(
        f".{destination.stem}.{os.getpid()}.part.mp4")
    temporary.unlink(missing_ok=True)
    try:
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(source),
             "-map", "0:v:0", "-an", "-c:v", "copy",
             "-movflags", "+faststart", str(temporary)],
            check=True, capture_output=True, timeout=600,
        )
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError("faststart remux produced an empty file")
        os.replace(temporary, destination)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("faststart remux timed out") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", errors="replace")[-300:]
        raise RuntimeError(f"faststart remux failed: {detail}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def ensure_h264_preview(source: Path) -> Path:
    """Return an H.264/yuv420p MP4 suitable for HTML5 playback.

    Browser-decodable sources are passed through directly when their moov
    index sits at the front (faststart); non-faststart files are remuxed
    once into the local cache (``-c copy``, byte-lossless, only the
    container box order changes) so HTML5 streaming does not stall a few
    seconds into playback.  Remux failure falls back to the original file.

    Callers should run this function via ``asyncio.to_thread`` so a cold
    transcode never blocks FastAPI's event loop.
    """
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(f"Video source not found: {source}")

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to build browser video previews")

    key = _cache_key(source)
    destination = _cache_root() / f"{key}.mp4"
    if destination.is_file() and destination.stat().st_size > 0:
        return destination
    if _is_h264_yuv420(source) and _probe_faststart(source):
        return source

    with _lock_for(key):
        if destination.is_file() and destination.stat().st_size > 0:
            return destination
        if _is_h264_yuv420(source):
            # 非 faststart 的 h264:一次性无损重排 moov 到文件头。
            # remux 失败回退原文件,绝不阻断播放。
            try:
                _remux_faststart(source, destination)
                if destination.is_file() and destination.stat().st_size > 0:
                    return destination
            except Exception:
                pass
            return source

        # Keep the final .mp4 suffix so ffmpeg can infer the output muxer.
        temporary = destination.with_name(
            f".{destination.stem}.{os.getpid()}.part.mp4")
        temporary.unlink(missing_ok=True)
        try:
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", str(source),
                 "-map", "0:v:0", "-an",
                 "-c:v", "libx264", "-preset", "veryfast",
                 "-pix_fmt", "yuv420p",
                 # Short GOP keeps frame stepping and range seeks responsive.
                 "-g", "25", "-keyint_min", "25", "-sc_threshold", "0",
                 "-movflags", "+faststart", str(temporary)],
                check=True, capture_output=True, timeout=3600,
            )
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise RuntimeError("ffmpeg produced an empty browser preview")
            os.replace(temporary, destination)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("H.264 preview transcode timed out") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode("utf-8", errors="replace")[-500:]
            raise RuntimeError(f"H.264 preview transcode failed: {detail}") from exc
        finally:
            temporary.unlink(missing_ok=True)
    return destination
