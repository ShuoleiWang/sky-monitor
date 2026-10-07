"""The monitor: cameras in, one published answer out, every cycle.

The answer lives in memory for the Alpaca devices and in ``status.json`` for
everything else, each with a time until which it holds: a monitor that stops
or stalls turns into "not safe" by itself.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import numpy as np
from PIL import Image

from .analysis import Assessment, CameraAnalyzer, Verdict, render_preview
from .config import CameraConfig, Config
from .decision import Decision, DecisionMachine
from .ephemeris import SunMoon, moon_events, sun_moon
from .forecast import ForecastService, hour_of, observed_cloud_from_history
from .mail import Attachment, EmailError, send_email
from .messages import describe, email_text, resolve_language
from .night import NightLog, NightState, night_of
from .providers import build_providers
from .sources import (
    FfmpegSource,
    FileSource,
    FrameSource,
    Freshness,
    SnapshotSource,
    SourceError,
    find_ffmpeg,
)

SCHEMA = "sky-monitor.status/1"
# How long a stopping monitor waits for a look that is still running.
STOP_WAIT_SECONDS = 3.0
_LOG = logging.getLogger(__name__)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _replace(temporary: Path, target: Path) -> None:
    for attempt in range(6):
        try:
            os.replace(temporary, target)
            return
        except PermissionError:
            # Windows refuses while a reader holds the target open; readers let go within moments.
            if attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))


def write_text_atomic(path: Path, text: str) -> None:
    """Replace ``path`` in one step, so that a reader never sees half a file."""

    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    _replace(temporary, path)


class SafetyHolder:
    """The latest answer for readers in other threads; past its validity it reads unsafe."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._safe = False
        self._imaging_ok = False
        self._valid_until = 0.0

    def publish(self, safe: bool, imaging_ok: bool, valid_seconds: float) -> None:
        with self._lock:
            self._safe = safe
            self._imaging_ok = imaging_ok
            self._valid_until = self._clock() + valid_seconds

    def roof_may_open(self) -> bool:
        with self._lock:
            return self._safe and self._clock() <= self._valid_until

    def imaging_may_run(self) -> bool:
        with self._lock:
            return self._imaging_ok and self._clock() <= self._valid_until


class _Unavailable:
    """A camera that cannot be read at all; it says why at every look."""

    def __init__(self, error: SourceError) -> None:
        self._error = error

    def grab(self):
        raise SourceError(self._error.code, self._error.detail)


def build_source(camera: CameraConfig, ffmpeg: str = "") -> FrameSource:
    if camera.kind == "stream":
        return FfmpegSource(
            camera.address,
            ffmpeg=[find_ffmpeg(ffmpeg or None)],
            frames=camera.frames,
            seconds=camera.seconds,
            timeout=camera.timeout_seconds or None,
            max_width=camera.max_width,
            input_options=camera.input_options,
        )
    if camera.kind == "snapshot":
        return SnapshotSource(
            camera.address,
            username=camera.username,
            password=camera.password,
            timeout=camera.timeout_seconds or 20.0,
            insecure_tls=camera.insecure_tls,
            max_width=camera.max_width,
        )
    return FileSource(Path(camera.address), max_width=camera.max_width)


@dataclass
class _Camera:
    config: CameraConfig
    source: FrameSource
    analyzer: CameraAnalyzer
    freshness: Freshness
    archived_verdict: Verdict | None = None


class Monitor:
    def __init__(
        self,
        config: Config,
        *,
        sources: Mapping[str, FrameSource] | None = None,
        clock: Callable[[], datetime] | None = None,
        holder: SafetyHolder | None = None,
        mailer: Callable[..., None] = send_email,
        panel_url: str = "",
        forecast: ForecastService | None = None,
    ) -> None:
        self._config = config
        self._clock = clock or (lambda: datetime.now(UTC))
        self._holder = holder or SafetyHolder()
        self._mailer = mailer
        self._panel_url = panel_url
        self._forecast = forecast
        self._language = resolve_language(config.language)
        self._machine = DecisionMachine(config.decision)
        self._output = config.output.directory
        for folder in ("history", "preview", "state"):
            (self._output / folder).mkdir(parents=True, exist_ok=True)
        self._cameras: list[_Camera] = []
        for camera in config.cameras:
            if sources is not None and camera.name in sources:
                source = sources[camera.name]
            else:
                try:
                    source = build_source(camera, config.ffmpeg)
                except SourceError as error:
                    source = _Unavailable(error)
            analyzer = CameraAnalyzer(
                camera.analysis,
                region=camera.region,
                state_path=self._output / "state" / f"{camera.name}.fixed.npz",
                interval_seconds=config.interval_seconds,
            )
            self._cameras.append(_Camera(camera, source, analyzer, Freshness(camera.stale_after_seconds)))
        self._pool = ThreadPoolExecutor(max_workers=len(self._cameras), thread_name_prefix="camera")
        self._valid_seconds = max(3.0 * config.interval_seconds, config.interval_seconds + 90.0)
        self._references: dict[str, int] = {}
        self._references_stamp: int | None = None
        self._announced: tuple[bool, bool] | None = None
        self._pruned_on: date | None = None
        self._archived_at: datetime | None = None
        self._cycle_lock = threading.Lock()
        self._publish_lock = threading.Lock()
        self._closed = False
        self._night_lock = threading.Lock()
        self._night = NightLog(self._output / "state" / "night.json", config.site.longitude)
        site = config.site
        if (
            self._forecast is None
            and config.forecast.enabled
            and site.latitude is not None
            and site.longitude is not None
        ):
            try:
                self._forecast = ForecastService(
                    build_providers(config.forecast),
                    site.latitude,
                    site.longitude,
                    self._output / "forecast",
                    refresh_seconds=config.forecast.refresh_minutes * 60.0,
                    coordinate_precision=config.forecast.coordinate_precision,
                    clock=self._clock,
                )
            except OSError as error:
                _LOG.warning("forecast is off: %s", error)
        self._scored_marker = self._output / "state" / "forecast-scored.txt"
        try:
            self._scored_night: str | None = self._scored_marker.read_text(encoding="utf-8").strip() or None
        except OSError:
            self._scored_night = None
        self._latest: dict[str, object] | None = None
        self._last_email: dict[str, object] | None = None
        self._idle = False
        # Whether automation reads the safety devices; set once the Alpaca server runs.
        self._automation: Callable[[], bool] = lambda: False

    def set_automation_probe(self, probe: Callable[[], bool]) -> None:
        """Tell the monitor how to see whether software reads its safety devices.

        While it does, the operator's "roof opened" mark only ends the mail: pausing would
        make the devices read unsafe and the software would close the roof.
        """

        self._automation = probe

    # What the control page, the tray and the commands read and change.

    def latest_status(self) -> dict[str, object] | None:
        with self._publish_lock:
            return self._latest

    def night_state(self) -> NightState:
        with self._night_lock:
            return self._night.current(self._clock())

    def set_roof_opened(self, opened: bool) -> NightState:
        """The operator's mark: the roof is open tonight (True) or that is cancelled (False)."""

        with self._night_lock:
            return self._night.set_roof_opened(self._clock(), opened)

    def set_acknowledged(self, acknowledged: bool) -> NightState:
        """The operator has read the reminder: no more repeats for this clear spell."""

        with self._night_lock:
            return self._night.set_acknowledged(self._clock(), acknowledged)

    def last_email(self) -> dict[str, object] | None:
        with self._publish_lock:
            return self._last_email

    def history(self, hours: float) -> list[dict[str, object]]:
        """The night's course from the history files: time, coverage and the answer, one point a minute at most."""

        now = self._clock()
        cutoff = now - timedelta(seconds=hours * 3600.0)
        points: list[dict[str, object]] = []
        last_minute = None
        files = sorted((self._output / "history").glob("????-??-??.jsonl"))[-3:]
        for path in files:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    entry = json.loads(line)
                    moment = datetime.fromisoformat(entry["updatedAt"])
                except (ValueError, KeyError, TypeError):
                    continue
                if moment < cutoff:
                    continue
                minute = moment.replace(second=0, microsecond=0)
                if minute == last_minute:
                    continue
                last_minute = minute
                points.append(
                    {
                        "t": entry["updatedAt"],
                        "coverage": entry.get("coverage"),
                        "safe": entry.get("safe") is True,
                        "reason": entry.get("reason"),
                    }
                )
        return points

    def send_test_email(self) -> None:
        subject, text = email_text("test", {"site": self._config.site.name}, self._language, self._panel_url)
        self._mailer(self._config.email, subject, text, ())

    def cycle(self) -> dict[str, object]:
        """Look at the sky once and publish the answer."""

        with self._cycle_lock:
            return self._cycle()

    def _cycle(self) -> dict[str, object]:
        now = self._clock()
        with self._night_lock:
            night = self._night.current(now)
        paused = night.roof_opened and self._config.operator.pause_when_opened and not self._automation()
        site = self._config.site
        ephemeris = None
        if site.latitude is not None and site.longitude is not None:
            ephemeris = sun_moon(now, site.latitude, site.longitude)
        sun = None if ephemeris is None else ephemeris.sun.altitude_deg
        dark = sun is None or sun <= self._config.decision.sun_max_altitude_deg
        if paused:
            assessments = [Assessment(Verdict.UNKNOWN, "ROOF_OPENED") for _ in self._cameras]
            self._machine.reset()
        elif dark:
            self._load_references()
            moon = 0.0 if ephemeris is None else ephemeris.moon_strength
            assessments = list(self._pool.map(lambda camera: self._observe(camera, now, moon), self._cameras))
        else:
            assessments = [Assessment(Verdict.UNKNOWN, "DAYLIGHT") for _ in self._cameras]
        forecast_block: dict[str, object] | None = None
        veto = None
        if self._forecast is not None:
            settings = self._config.forecast
            forecast_block = self._forecast.summary(
                self._night_hours(now, ephemeris),
                rain_window_hours=settings.rain_veto_hours,
                rain_probability=settings.rain_veto_probability,
                rain_mm=settings.rain_veto_mm,
            )
            if forecast_block.get("rainSoon"):
                veto = "RAIN_FORECAST"
        decision = self._machine.update(
            now,
            [a.verdict for a in assessments],
            [a.coverage for a in assessments],
            sun,
            veto=veto,
            moon_altitude_deg=None if ephemeris is None else ephemeris.moon.altitude_deg,
            moon_illumination=None if ephemeris is None else ephemeris.moon_illumination,
        )
        status = self._status(now, decision, ephemeris, assessments, night, paused, forecast_block)
        # Nothing to watch: look less often.
        self._idle = paused or not dark or all(a.reason == "TOO_BRIGHT" for a in assessments)
        self._publish(status, assessments)
        return status

    def _night_hours(self, now: datetime, ephemeris: SunMoon | None) -> list[datetime]:
        """The hours of darkness ahead (24 h at most); the next 12 hours without a site position."""

        start = hour_of(now)
        hours = [start + timedelta(hours=index) for index in range(24)]
        site = self._config.site
        if ephemeris is None or site.latitude is None or site.longitude is None:
            return hours[:12]
        gate = self._config.decision.sun_max_altitude_deg
        dark = [
            hour
            for hour in hours
            if sun_moon(hour + timedelta(minutes=30), site.latitude, site.longitude).sun.altitude_deg <= gate
        ]
        return dark or hours[:12]

    def _observe(self, camera: _Camera, now: datetime, moon_strength: float) -> Assessment:
        try:
            burst = camera.source.grab()
            camera.freshness.check(burst, now.timestamp())
            return camera.analyzer.assess(
                burst,
                moon_strength=moon_strength,
                reference_star_count=self._references.get(camera.config.name, 0),
            )
        except SourceError as error:
            return Assessment(Verdict.UNKNOWN, error.code, detail=error.detail)
        except Exception as error:  # a fault in the analysis must never read as a clear sky
            _LOG.exception("camera %s: the analysis failed", camera.config.name)
            return Assessment(Verdict.UNKNOWN, "ANALYSIS_ERROR", detail=type(error).__name__)

    def _load_references(self) -> None:
        """Clear-sky star counts written by ``sky-monitor calibrate``."""

        path = self._output / "reference.json"
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            self._references, self._references_stamp = {}, None
            return
        if stamp == self._references_stamp:
            return
        self._references_stamp = stamp
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            self._references = {str(name): int(entry["starCount"]) for name, entry in stored["cameras"].items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            _LOG.warning("reference.json cannot be read; cameras fall back to min_stars")
            self._references = {}

    def _status(
        self,
        now: datetime,
        decision: Decision,
        ephemeris: SunMoon | None,
        assessments: Sequence[Assessment],
        night: NightState,
        paused: bool,
        forecast: dict[str, object] | None = None,
    ) -> dict[str, object]:
        params = self._config.decision
        status: dict[str, object] = {
            "schema": SCHEMA,
            "site": self._config.site.name,
            "updatedAt": _iso(now),
            "validUntil": _iso(now + timedelta(seconds=self._valid_seconds)),
            "safe": decision.safe,
            "imagingOk": decision.imaging_ok,
            "verdict": decision.verdict.value,
            "reason": "ROOF_OPENED" if paused else decision.reason,
            "night": night.to_json(),
            "paused": paused,
            "lastEmail": self.last_email(),
            "since": _iso(decision.since),
            "coverage": None if decision.coverage is None else round(decision.coverage, 3),
            "heldSeconds": round(decision.held_seconds),
            "heldCoverage": None if decision.held_coverage is None else round(decision.held_coverage, 3),
            "openAfterSeconds": round(params.open_after_seconds),
            "openCoverage": params.open_coverage,
            "sunGateDeg": params.sun_max_altitude_deg,
            "sun": None,
            "moon": None,
            "cameras": [
                {"name": camera.config.name, **assessment.to_json()}
                for camera, assessment in zip(self._cameras, assessments, strict=True)
            ],
        }
        if ephemeris is not None:
            status["sun"] = {
                "altitudeDeg": round(ephemeris.sun.altitude_deg, 2),
                "azimuthDeg": round(ephemeris.sun.azimuth_deg, 1),
            }
            site = self._config.site
            assert site.latitude is not None and site.longitude is not None
            events = moon_events(now, site.latitude, site.longitude)
            status["moon"] = {
                "altitudeDeg": round(ephemeris.moon.altitude_deg, 2),
                "illumination": round(ephemeris.moon_illumination, 3),
                "risesAt": None if events["rise"] is None else _iso(events["rise"]),
                "setsAt": None if events["set"] is None else _iso(events["set"]),
            }
        status["forecast"] = forecast
        warm_up = max(camera.config.analysis.fixed_mature_minutes for camera in self._cameras)
        status["message"] = describe(status, self._language, warm_up)
        return status

    def fail(self, reason: str, *, final: bool = False) -> dict[str, object]:
        """Publish "not safe" outside a normal cycle: the monitor stopped or broke."""

        now = self._clock()
        status: dict[str, object] = {
            "schema": SCHEMA,
            "site": self._config.site.name,
            "updatedAt": _iso(now),
            "validUntil": _iso(now),
            "safe": False,
            "imagingOk": False,
            "verdict": Verdict.UNKNOWN.value,
            "reason": reason,
            "since": _iso(now),
            "cameras": [],
        }
        status["message"] = describe(status, self._language)
        self._publish(status, (), final=final)
        return status

    def _publish(self, status: dict[str, object], assessments: Sequence[Assessment], *, final: bool = False) -> None:
        with self._publish_lock:
            if self._closed and not final:
                # A look that outlived the monitor: "stopped" stays the last word.
                return
            self._closed = self._closed or final
            self._latest = status
            self._publish_locked(status, assessments)

    def _publish_locked(self, status: dict[str, object], assessments: Sequence[Assessment]) -> None:
        state = (status["safe"] is True, status["imagingOk"] is True)
        self._holder.publish(*state, self._valid_seconds)
        try:
            write_text_atomic(self._output / "status.json", json.dumps(status, ensure_ascii=False, indent=2) + "\n")
            self._record(status)
            self.score_last_night()
            # A stopped or failed monitor publishes no looks: nothing to draw or keep.
            if self._config.output.preview and assessments:
                self._write_previews(status, assessments)
            if self._config.output.archive_minutes > 0 and assessments:
                self._archive(assessments)
        except OSError as error:
            # Readers of status.json then see it pass its validity, which reads as unsafe.
            _LOG.error("cannot write to %s: %s", self._output.name, error)
        self._mail(status)
        if self._announced is not None and state != self._announced:
            self._notify(status)
        self._announced = state

    def _record(self, status: Mapping[str, object]) -> None:
        today = self._clock().astimezone().date()
        history = self._output / "history"
        # The forecast block is large and kept by the forecast service itself.
        line = json.dumps(
            {k: v for k, v in status.items() if k != "forecast"}, ensure_ascii=False, separators=(",", ":")
        )
        with (history / f"{today.isoformat()}.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
        if self._pruned_on == today:
            return
        self._pruned_on = today
        output = self._config.output
        oldest = (today - timedelta(days=output.history_days)).isoformat()
        for entry in history.glob("????-??-??.jsonl"):
            if entry.stem < oldest:
                entry.unlink(missing_ok=True)
        oldest = (today - timedelta(days=output.archive_days)).strftime("%Y%m%d")
        for entry in (self._output / "archive").glob("*/????????-??????-*.png"):
            if entry.name[:8] < oldest:
                entry.unlink(missing_ok=True)
        if self._forecast is not None:
            self._forecast.prune(today)

    def score_last_night(self) -> int:
        """Once per night: score every forecast source against what the cameras measured."""

        if self._forecast is None:
            return 0
        now = self._clock()
        longitude = self._config.site.longitude
        tonight = night_of(now, longitude)
        if self._scored_night == tonight:
            return 0
        previous = (date.fromisoformat(tonight) - timedelta(days=1)).isoformat()
        entries = []
        for day in (previous, tonight):
            try:
                lines = (self._output / "history" / f"{day}.jsonl").read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    entry = json.loads(line)
                    if night_of(datetime.fromisoformat(str(entry["updatedAt"])), longitude) == previous:
                        entries.append(entry)
                except (ValueError, KeyError, TypeError):
                    continue
        added = self._forecast.score_night(observed_cloud_from_history(entries))
        self._scored_night = tonight
        try:
            write_text_atomic(self._scored_marker, tonight + "\n")
        except OSError:
            pass
        if added:
            _LOG.info("forecast: scored %d source-hours of the night of %s", added, previous)
        return added

    def _archive(self, assessments: Sequence[Assessment]) -> None:
        """Keep the frames the verdicts were read from, to tune thresholds on real nights."""

        now = self._clock()
        period = self._config.output.archive_minutes * 60.0
        due = self._archived_at is None or (now - self._archived_at).total_seconds() >= period - 1.0
        stamp = now.astimezone().strftime("%Y%m%d-%H%M%S")
        for camera, assessment in zip(self._cameras, assessments, strict=True):
            if assessment.overlay is None:
                continue
            if not due and assessment.verdict is camera.archived_verdict:
                continue
            camera.archived_verdict = assessment.verdict
            folder = self._output / "archive" / camera.config.name
            folder.mkdir(parents=True, exist_ok=True)
            frame = np.round(np.clip(assessment.overlay.image, 0.0, 1.0) * 65535.0).astype(np.uint16)
            Image.fromarray(frame).save(folder / f"{stamp}-{assessment.verdict.value}.png")
        if due:
            self._archived_at = now

    def _write_previews(self, status: Mapping[str, object], assessments: Sequence[Assessment]) -> None:
        for camera, assessment in zip(self._cameras, assessments, strict=True):
            if assessment.overlay is None:
                continue
            caption = (
                f"{camera.config.name}  {assessment.verdict.value}  "
                f"sky with stars {assessment.coverage or 0:.0%}  "
                f"stars {assessment.star_count}/{assessment.required_stars}  {status['updatedAt']}"
            )
            target = self._output / "preview" / f"{camera.config.name}.jpg"
            temporary = target.with_name(target.name + ".tmp")
            render_preview(assessment.overlay, caption).save(temporary, "JPEG", quality=85)
            _replace(temporary, target)

    def _mail(self, status: Mapping[str, object]) -> None:
        """Mail when the roof may open, and when that is no longer so; nothing once it is marked open."""

        settings = self._config.email
        if not settings.enabled or status.get("reason") in ("STOPPED", "MONITOR_ERROR"):
            return
        now = self._clock()
        with self._night_lock:
            night = self._night.current(now)
            if night.roof_opened:
                return
            if status.get("safe") is True:
                if not settings.on_clear:
                    return
                if night.notified_at is not None:
                    since = (now - datetime.fromisoformat(night.notified_at)).total_seconds()
                    if night.acknowledged or settings.repeat_minutes <= 0 or since < settings.repeat_minutes * 60.0:
                        return
                self._night.set_notified(now, True)
                kind = "clear"
            elif night.notified_at is not None and settings.on_clear_lost:
                self._night.set_notified(now, False)
                kind = "lost"
            else:
                return
        subject, text = email_text(kind, status, self._language, self._panel_url)
        attachments: list[Attachment] = []
        if kind == "clear":
            for camera in self._cameras:
                try:
                    data = (self._output / "preview" / f"{camera.config.name}.jpg").read_bytes()
                except OSError:
                    continue
                attachments.append(Attachment(f"{camera.config.name}.jpg", data, "image/jpeg"))

        def deliver() -> None:
            record: dict[str, object] = {"at": _iso(self._clock()), "kind": kind, "subject": subject}
            try:
                self._mailer(settings, subject, text, attachments)
                record["ok"] = True
                _LOG.info("mail sent: %s", subject)
            except EmailError as error:
                record.update(ok=False, error=str(error))
                _LOG.warning("mail not sent (%s): %s", subject, error)
                if kind == "clear":
                    # Not delivered: the next look tries again.
                    with self._night_lock:
                        self._night.set_notified(now, False)
            with self._publish_lock:
                self._last_email = record

        threading.Thread(target=deliver, name="mail", daemon=True).start()

    def _notify(self, status: Mapping[str, object]) -> None:
        command = self._config.notify_command
        if not command:
            return
        message = str(status["message"])
        state = "IMAGING" if status["imagingOk"] else ("OPEN" if status["safe"] else "CLOSED")
        arguments = [
            part.replace("{state}", state)
            .replace("{message_url}", quote(message, safe=""))
            .replace("{message}", message)
            for part in command
        ]
        environment = {**os.environ, "SKY_MONITOR_STATE": state, "SKY_MONITOR_MESSAGE": message}

        def run() -> None:
            try:
                subprocess.run(
                    arguments,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                    env=environment,
                    creationflags=_NO_WINDOW,
                )
            except (OSError, subprocess.SubprocessError) as error:
                _LOG.warning("the notify command failed: %s", type(error).__name__)

        threading.Thread(target=run, name="notify", daemon=True).start()

    def _attempt_cycle(self) -> None:
        try:
            status = self.cycle()
            label = "IMAGING" if status["imagingOk"] else ("OPEN" if status["safe"] else "CLOSED")
            _LOG.info("%s | %s", label, status["message"])
        except Exception:
            _LOG.exception("the cycle failed")
            try:
                self.fail("MONITOR_ERROR")
            except Exception:
                _LOG.exception("the failure could not be published")

    def cancel(self) -> None:
        """Stop the captures in flight; their cameras read as no data."""

        for camera in self._cameras:
            cancel = getattr(camera.source, "cancel", None)
            if cancel is not None:
                cancel()

    def run(self, stop: threading.Event) -> None:
        """Cycle until ``stop`` is set."""

        if self._forecast is not None:
            self._forecast.start()
        while not stop.is_set():
            started = time.monotonic()
            look = threading.Thread(target=self._attempt_cycle, name="cycle", daemon=True)
            look.start()
            # Short waits throughout: on Windows a long one would hold back Ctrl+C until it ends.
            while look.is_alive() and not stop.is_set():
                look.join(0.5)
            interval = self._config.idle_interval_seconds if self._idle else self._config.interval_seconds
            until = max(started + interval, time.monotonic() + 1.0)
            while not stop.is_set() and time.monotonic() < until:
                stop.wait(0.5)

    def close(self) -> None:
        """Leave "not safe" behind, at once, and keep what the cameras learned."""

        self.cancel()
        if self._forecast is not None:
            self._forecast.stop()
        idle = self._cycle_lock.acquire(timeout=STOP_WAIT_SECONDS)
        try:
            self.fail("STOPPED", final=True)
            if not idle:
                _LOG.warning("a look was still running; what it learned since the last save is not kept")
                return
            for camera in self._cameras:
                try:
                    camera.analyzer.save()
                except OSError as error:
                    _LOG.warning("camera %s: its fixed sources were not saved: %s", camera.config.name, error)
        finally:
            if idle:
                self._cycle_lock.release()
            self._pool.shutdown(wait=False, cancel_futures=True)
