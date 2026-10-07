"""Frame acquisition.

Every source returns stacked luminance within a hard time limit or raises a
SourceError.  A frame that cannot be obtained is never a clear sky, and no
text that leaves this module carries a camera password.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import numpy as np
from PIL import Image

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"})
_DEEP_MODES = frozenset({"I;16", "I;16L", "I;16B", "I;16N", "I"})
_MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
_PGM_HEADER = re.compile(rb"P5\s+(\d+)\s+(\d+)\s+(\d+)\s")
_URL_PASSWORD = re.compile(r"(?P<head>[A-Za-z][A-Za-z0-9+.\-]*://[^/@:\s]*):[^/@\s]*@")
_QUERY_SECRET = re.compile(r"(?i)\b(password|passwd|pwd|token|secret|key|auth)=[^&\s]+")


def redact(text: str) -> str:
    """``text`` without URL passwords and secret-looking query values."""

    return _QUERY_SECRET.sub(r"\1=***", _URL_PASSWORD.sub(r"\g<head>:***@", text))


class SourceError(Exception):
    """A frame could not be obtained; ``code`` is stable and ``detail`` is safe to publish."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = redact(detail).strip()
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


@dataclass(frozen=True)
class FrameBurst:
    # One stack, or two stacks of the same view taken ``seconds_apart`` apart;
    # float32 luminance on a 0..1 scale.
    images: tuple[np.ndarray, ...]
    seconds_apart: float
    frames: int
    # Changes whenever the camera delivered something new.
    content_id: str
    captured_at: datetime


class FrameSource(Protocol):
    def grab(self) -> FrameBurst: ...


def find_ffmpeg(configured: str | None = None) -> str:
    """The ffmpeg executable: configured, ``SKY_MONITOR_FFMPEG``, bundled, PATH, then imageio-ffmpeg."""

    for candidate in (configured, os.environ.get("SKY_MONITOR_FFMPEG")):
        if candidate:
            resolved = shutil.which(candidate)
            if resolved is None:
                raise SourceError("NO_FFMPEG", f"ffmpeg not found at {candidate}")
            return resolved
    if getattr(sys, "frozen", False):
        # The installed product carries its own copy next to the program.
        bundled = Path(sys.executable).with_name("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if bundled.is_file():
            return str(bundled)
    resolved = shutil.which("ffmpeg")
    if resolved is not None:
        return resolved
    try:
        import imageio_ffmpeg

        return str(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:  # not installed, or its binary is missing
        pass
    raise SourceError("NO_FFMPEG", "install ffmpeg and put it on PATH, or pip install sky-monitor[ffmpeg]")


def _digest(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _shrink(values: np.ndarray, max_width: int) -> np.ndarray:
    factor = -(-values.shape[1] // max_width) if max_width > 0 else 1
    if factor <= 1:
        return values
    height = values.shape[0] // factor * factor
    width = values.shape[1] // factor * factor
    blocks = values[:height, :width].reshape(height // factor, factor, width // factor, factor)
    return blocks.mean(axis=(1, 3), dtype=np.float32)


def decode_image(data: bytes, max_width: int = 0) -> np.ndarray:
    """A still image as float32 luminance in 0..1."""

    try:
        with Image.open(io.BytesIO(data)) as picture:
            if picture.mode in _DEEP_MODES:
                values = np.asarray(picture, dtype=np.float32) / 65535.0
            else:
                values = np.asarray(picture.convert("L"), dtype=np.float32) / 255.0
    except Exception as error:  # Pillow raises unrelated types for damaged files
        raise SourceError("DECODE", f"{type(error).__name__}: {error}") from error
    if values.ndim != 2 or min(values.shape) < 32:
        raise SourceError("DECODE", "the image is too small to analyse")
    return _shrink(np.clip(values, 0.0, 1.0), max_width)


def _parse_pgm(data: bytes) -> list[np.ndarray]:
    """The complete 8-bit frames of a concatenated binary PGM stream."""

    frames: list[np.ndarray] = []
    position = 0
    while True:
        header = _PGM_HEADER.match(data, position)
        if header is None:
            break
        width, height, maximum = (int(value) for value in header.groups())
        start, size = header.end(), width * height
        if maximum != 255 or size == 0 or start + size > len(data):
            break
        frame = np.frombuffer(data, dtype=np.uint8, count=size, offset=start).reshape(height, width)
        if frames and frame.shape != frames[0].shape:
            break
        frames.append(frame)
        position = start + size
    return frames


def _mean(frames: Sequence[np.ndarray]) -> np.ndarray:
    return np.mean(np.stack(frames), axis=0, dtype=np.float32) / np.float32(255.0)


class FfmpegSource:
    """Anything ffmpeg reads: RTSP, HTTP, HLS or FLV streams, a video file, a screen region.

    One capture of ``frames`` frames spread over ``seconds`` becomes two stacks,
    its first and its second half, so that a star can be told from noise inside
    a single capture.  Repeated pictures count once.
    """

    def __init__(
        self,
        url: str,
        *,
        ffmpeg: Sequence[str],
        frames: int = 16,
        seconds: float = 8.0,
        timeout: float | None = None,
        max_width: int = 1920,
        input_options: Sequence[str] = (),
        seek: float = 0.0,
    ) -> None:
        self._url = url
        self._ffmpeg = tuple(ffmpeg)
        self._frames = max(1, int(frames))
        self._seconds = max(0.1, float(seconds))
        self._timeout = float(timeout) if timeout else self._seconds + 25.0
        self._max_width = int(max_width)
        self._input_options = tuple(input_options)
        self._seek = float(seek)
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._cancelled = False

    def command(self) -> list[str]:
        options = list(self._input_options)
        if self._url.lower().startswith(("rtsp://", "rtsps://")) and "-rtsp_transport" not in options:
            options = ["-rtsp_transport", "tcp", *options]
        if self._seek > 0:
            options = ["-ss", f"{self._seek:.3f}", *options]
        filters = [f"fps={self._frames / self._seconds:.6g}"]
        if self._max_width > 0:
            filters.append(f"scale='min(iw,{self._max_width})':-2:flags=area")
        return [
            *self._ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            *options,
            "-i",
            self._url,
            "-an",
            "-sn",
            "-dn",
            "-vf",
            ",".join(filters),
            "-frames:v",
            str(self._frames),
            "-pix_fmt",
            "gray",
            "-c:v",
            "pgm",
            "-f",
            "image2pipe",
            "pipe:1",
        ]

    def cancel(self) -> None:
        """Stop a capture in flight, for good: the monitor is shutting down."""

        with self._lock:
            self._cancelled = True
            process = self._process
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    def grab(self) -> FrameBurst:
        try:
            process = subprocess.Popen(
                self.command(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=_NO_WINDOW,
            )
        except OSError as error:
            raise SourceError("NO_FFMPEG", str(error)) from error
        with self._lock:
            self._process = process
            if self._cancelled:
                process.kill()
        timed_out = False
        try:
            try:
                data, errors = process.communicate(timeout=self._timeout)
            except subprocess.TimeoutExpired:
                # Whatever ffmpeg delivered in time is still usable.
                timed_out = True
                process.kill()
                data, errors = process.communicate()
        finally:
            with self._lock:
                self._process = None
        if self._cancelled:
            raise SourceError("CANCELLED", "the monitor is stopping")
        status = process.returncode
        frames = _parse_pgm(data)
        if not frames:
            if timed_out:
                raise SourceError("TIMEOUT", f"no frame within {self._timeout:.0f} s")
            message = errors.decode("utf-8", "replace").strip()[-400:]
            raise SourceError("NO_FRAMES", message or f"ffmpeg exited with status {status}")
        # A camera slower than the capture rate repeats its picture; a repeat is no second look.
        distinct = [frames[0]]
        distinct += [
            frame for earlier, frame in zip(frames, frames[1:], strict=False) if not np.array_equal(earlier, frame)
        ]
        half = len(distinct) // 2
        if half >= 2:
            images: tuple[np.ndarray, ...] = (_mean(distinct[:half]), _mean(distinct[half:]))
            seconds_apart = self._seconds * len(frames) / self._frames / 2.0
        else:
            # Too few pictures to tell a star from noise inside one capture: the newest one
            # alone, to be confirmed at the next look.
            images, seconds_apart = (_mean(distinct[-1:]),), 0.0
        content_id = _digest(b"".join(image.tobytes() for image in images))
        return FrameBurst(images, seconds_apart, len(distinct), content_id, _now())


class SnapshotSource:
    """An HTTP(S) address that returns the current picture, such as an all-sky ``image.jpg``."""

    def __init__(
        self,
        url: str,
        *,
        username: str = "",
        password: str = "",
        timeout: float = 20.0,
        insecure_tls: bool = False,
        max_width: int = 0,
    ) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise SourceError("BAD_URL", "a snapshot address starts with http:// or https://")
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        if parts.port is not None:
            host = f"{host}:{parts.port}"
        self._url = urlunsplit((parts.scheme, host, parts.path, parts.query, ""))
        self._timeout = float(timeout)
        self._max_width = int(max_width)
        manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        user, secret = username or (parts.username or ""), password or (parts.password or "")
        if user:
            manager.add_password(None, self._url, user, secret)
        context = ssl.create_default_context()
        if insecure_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPBasicAuthHandler(manager),
            urllib.request.HTTPDigestAuthHandler(manager),
            urllib.request.HTTPSHandler(context=context),
        )

    def grab(self) -> FrameBurst:
        request = urllib.request.Request(self._url, headers={"User-Agent": "sky-monitor", "Cache-Control": "no-cache"})
        deadline = time.monotonic() + self._timeout
        chunks: list[bytes] = []
        size = 0
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                if "multipart" in (response.headers.get("Content-Type") or "").lower():
                    raise SourceError("NOT_A_SNAPSHOT", 'this address streams; use type = "stream"')
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > _MAX_SNAPSHOT_BYTES:
                        raise SourceError("TOO_LARGE", "the response is larger than 64 MB")
                    if time.monotonic() > deadline:
                        raise SourceError("TIMEOUT", f"no picture within {self._timeout:.0f} s")
                    chunks.append(chunk)
        except urllib.error.HTTPError as error:
            raise SourceError(f"HTTP_{error.code}", str(error.reason)) from error
        except (urllib.error.URLError, OSError, ValueError) as error:
            reason = getattr(error, "reason", error)
            code = "TIMEOUT" if isinstance(reason, TimeoutError) else "UNREACHABLE"
            raise SourceError(code, str(reason)) from error
        data = b"".join(chunks)
        return FrameBurst((decode_image(data, self._max_width),), 0.0, 1, _digest(data), _now())


class FileSource:
    """An image another program keeps up to date, or the newest image in a directory."""

    def __init__(self, path: Path, *, max_width: int = 0) -> None:
        self._path = Path(path)
        self._max_width = int(max_width)

    def _target(self) -> Path:
        if not self._path.is_dir():
            return self._path
        try:
            images = [
                entry for entry in self._path.iterdir() if entry.suffix.lower() in IMAGE_SUFFIXES and entry.is_file()
            ]
            if images:
                return max(images, key=lambda entry: entry.stat().st_mtime)
        except OSError as error:
            raise SourceError("UNREADABLE", f"{type(error).__name__}: {error.strerror}") from error
        raise SourceError("NO_FILES", "the directory holds no image")

    def grab(self) -> FrameBurst:
        target = self._target()
        for attempt in (0, 1):
            try:
                data = target.read_bytes()
                image = decode_image(data, self._max_width)
                break
            except OSError as error:
                raise SourceError("UNREADABLE", f"{type(error).__name__}: {error.strerror}") from error
            except SourceError:
                # The writer may be half way through the file: look once more.
                if attempt:
                    raise
                time.sleep(0.5)
        return FrameBurst((image,), 0.0, 1, _digest(data), _now())


class Freshness:
    """Fails a source whose picture stopped changing: a frozen stream is not a quiet sky."""

    def __init__(self, stale_after_seconds: float) -> None:
        self._stale_after = float(stale_after_seconds)
        self._content_id: str | None = None
        self._changed_at = 0.0

    def check(self, burst: FrameBurst, now: float) -> None:
        """``now`` is a monotonic time in seconds."""

        if burst.content_id != self._content_id:
            self._content_id = burst.content_id
            self._changed_at = now
            return
        age = now - self._changed_at
        if age > self._stale_after:
            raise SourceError("STALE", f"the picture has not changed for {age:.0f} s")
