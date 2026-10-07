"""What the sources share: one GET within a time limit, its JSON answer, every failure as ProviderError."""

from __future__ import annotations

import gzip
import http.client
import json
import math
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta, timezone

from ..forecast import ProviderError

TIMEOUT_SECONDS = 20.0
_USER_AGENT = "sky-monitor"
_MAX_BYTES = 8_000_000


def redact(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def coordinate(value: float) -> str:
    """A coordinate as plain decimals, without an exponent or trailing zeros."""

    return f"{float(value):.6f}".rstrip("0").rstrip(".")


def get_json(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    secrets: Sequence[str] = (),
    timeout: float = TIMEOUT_SECONDS,
) -> object:
    """GET ``url`` and decode its JSON answer; no text in ``secrets`` reaches an error message."""

    request = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json", "Accept-Encoding": "gzip", **(headers or {})},
    )
    deadline = time.monotonic() + timeout
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            chunks: list[bytes] = []
            size = 0
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError
                chunk = response.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_BYTES:
                    raise ProviderError("the answer is too large")
                chunks.append(chunk)
            body = _decoded(b"".join(chunks), response.headers)
    except urllib.error.HTTPError as error:
        raise ProviderError(redact(f"HTTP {error.code}{_detail(error)}", secrets)) from None
    except urllib.error.URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise ProviderError(f"no answer within {timeout:g} s") from None
        raise ProviderError(redact(f"cannot reach the server: {error.reason}", secrets)) from None
    except TimeoutError:
        raise ProviderError(f"no answer within {timeout:g} s") from None
    except (OSError, EOFError, ValueError, zlib.error, http.client.HTTPException) as error:
        raise ProviderError(redact(f"{type(error).__name__}: {error}", secrets)) from None
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        raise ProviderError("the answer is not JSON") from None


def _decoded(body: bytes, headers: Mapping[str, str] | None) -> bytes:
    encoding = (headers.get("Content-Encoding", "") if headers is not None else "") or ""
    if "gzip" in encoding.lower() or body[:2] == b"\x1f\x8b":
        return gzip.decompress(body)
    return body


def _detail(error: urllib.error.HTTPError) -> str:
    """The reason a service gives in the body of an HTTP error, as ": text"; empty without one."""

    try:
        body = _decoded(error.read(65536), error.headers)
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    parts: list[str] = []
    for key in ("reason", "error", "message", "detail"):
        value = payload.get(key)
        if isinstance(value, dict):
            parts.extend(str(value[name]) for name in ("title", "detail") if value.get(name))
        elif isinstance(value, str) and value:
            parts.append(value)
    text = ": ".join(parts)[:240]
    return f": {text}" if text else ""


def number(value: object) -> float | None:
    """A finite number from a JSON value (numbers may come as text); None for anything else."""

    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def scaled(value: object, factor: float) -> float | None:
    result = number(value)
    return None if result is None else result * factor


def parse_time(text: str, *, offset_seconds: float = 0.0) -> datetime:
    """An ISO 8601 time; one without a zone is taken at ``offset_seconds`` from UTC."""

    moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone(timedelta(seconds=offset_seconds)))
    return moment.astimezone(UTC)


def now_utc() -> datetime:
    return datetime.now(UTC)
