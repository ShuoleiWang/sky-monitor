"""The ``sky-monitor`` command."""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import math
import os
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import __version__
from .alpaca import AlpacaServer, SafetyDevice
from .analysis import AnalysisParams, CameraAnalyzer, SkyRegion, render_preview
from .config import TEMPLATE, Config, ConfigError, load_config
from .ephemeris import sun_moon
from .forecast import Forecast, ForecastService, ProviderError, hour_of
from .mail import EmailError
from .messages import resolve_language
from .night import NightLog
from .providers import build_providers
from .service import Monitor, SafetyHolder, build_source, write_text_atomic
from .sources import IMAGE_SUFFIXES, FfmpegSource, FileSource, SourceError, find_ffmpeg, redact
from .tray import run_tray
from .web import ControlPanel

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# How long a starting monitor waits for its port when a previous instance still holds it.
PORT_WAIT_SECONDS = 30.0


def default_config_path() -> Path:
    """Where the installed product keeps its settings when none are named."""

    named = os.environ.get("SKY_MONITOR_CONFIG")
    if named:
        return Path(named)
    local = Path("sky-monitor.toml")
    if local.is_file():
        return local.resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Sky Monitor"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Sky Monitor"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sky-monitor"
    return base / "sky-monitor.toml"


def _tell(text: str) -> None:
    """Say something to a person who may have no console (the tray on Windows)."""

    print(text, file=sys.stderr)
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, "Sky Monitor", 0x40)
        except (AttributeError, OSError):
            pass


def _init(args: argparse.Namespace) -> int:
    path = Path(args.path)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(TEMPLATE)
    except FileExistsError:
        print(f"sky-monitor: {path} exists; it was not overwritten", file=sys.stderr)
        return 2
    print(f"Wrote {path}. Set the camera address in it, then run: sky-monitor doctor --config {path}")
    return 0


def _port_state(host: str, port: int) -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError:
        return "in use (a running monitor holds it)"
    finally:
        probe.close()
    return "free"


def _doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    report: dict[str, object] = {"version": __version__, "ffmpeg": None, "site": None, "cameras": []}
    healthy = True
    if any(camera.kind == "stream" for camera in config.cameras):
        try:
            report["ffmpeg"] = find_ffmpeg(config.ffmpeg or None)
        except SourceError as error:
            report["ffmpeg"] = f"missing: {error.detail}"
            healthy = False
    site = config.site
    if site.latitude is not None and site.longitude is not None:
        sky = sun_moon(datetime.now(UTC), site.latitude, site.longitude)
        report["site"] = {
            "sunAltitudeDeg": round(sky.sun.altitude_deg, 1),
            "moonAltitudeDeg": round(sky.moon.altitude_deg, 1),
            "moonIllumination": round(sky.moon_illumination, 2),
            "darkEnoughForRoof": sky.sun.altitude_deg <= config.decision.sun_max_altitude_deg,
        }
    cameras: list[dict[str, object]] = []
    for camera in config.cameras:
        entry: dict[str, object] = {"name": camera.name, "type": camera.kind}
        started = time.monotonic()
        try:
            burst = build_source(camera, config.ffmpeg).grab()
            params = replace(camera.analysis, require_mature=False)
            look = CameraAnalyzer(params, region=camera.region).assess(burst)
            height, width = burst.images[-1].shape
            entry.update(
                ok=True,
                seconds=round(time.monotonic() - started, 1),
                width=width,
                height=height,
                frames=burst.frames,
                compactSources=look.star_count,
                background=look.background,
            )
        except SourceError as error:
            entry.update(ok=False, error=error.code, detail=error.detail)
            healthy = False
        cameras.append(entry)
    report["cameras"] = cameras
    if config.alpaca.enabled:
        report["alpacaPort"] = f"{config.alpaca.port}: {_port_state(config.alpaca.host, config.alpaca.port)}"

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if healthy else 1
    print(f"sky-monitor {__version__}")
    if report["ffmpeg"] is not None:
        print(f"ffmpeg: {report['ffmpeg']}")
    if report["site"] is None:
        print("site: no latitude/longitude; the Sun and Moon are not used")
    else:
        facts = report["site"]
        print(
            f"site: Sun {facts['sunAltitudeDeg']:+.1f} deg, Moon {facts['moonAltitudeDeg']:+.1f} deg "
            f"({facts['moonIllumination']:.0%} lit); dark enough for the roof: {facts['darkEnoughForRoof']}"
        )
    for entry in cameras:
        if entry["ok"]:
            print(
                f"camera {entry['name']}: OK, {entry['width']}x{entry['height']}, {entry['frames']} frame(s) "
                f"in {entry['seconds']} s, {entry['compactSources']} compact sources (stars, at night), "
                f"background {entry['background']}"
            )
        else:
            print(f"camera {entry['name']}: FAILED, {entry['error']} {entry['detail']}")
    if "alpacaPort" in report:
        print(f"Alpaca port {report['alpacaPort']}")
    print("Result: every camera delivers frames." if healthy else "Result: NOT ready, see above.")
    return 0 if healthy else 1


def _analyze(args: argparse.Namespace) -> int:
    detection = replace(AnalysisParams().detection, threshold_sigma=args.threshold)
    params = AnalysisParams(detection=detection, min_stars=args.min_stars, require_mature=False)
    region = None
    if args.rectangle or args.circle:
        region = SkyRegion(
            rectangle=tuple(args.rectangle) if args.rectangle else None,
            circle=tuple(args.circle) if args.circle else None,
        )
    path = Path(args.input)
    local = path.is_file()
    out = Path(args.out) if args.out else None
    if out is not None and out.exists():
        print(f"sky-monitor: {out} exists; choose a new name for --out", file=sys.stderr)
        return 2
    try:
        if local and path.suffix.lower() in IMAGE_SUFFIXES:
            bursts = [FileSource(path, max_width=args.max_width).grab()]
        else:
            ffmpeg = [find_ffmpeg(args.ffmpeg or None)]

            def look(seek: float):
                source = FfmpegSource(
                    args.input,
                    ffmpeg=ffmpeg,
                    frames=args.frames,
                    seconds=args.seconds,
                    max_width=args.max_width,
                    seek=seek,
                )
                return source.grab()

            bursts = [look(args.start)]
            if args.delay > 0:
                later = args.seconds + args.delay
                try:
                    if local:
                        second = look(args.start + later)
                        # A file is read at once; its second look was filmed ``later`` seconds on.
                        moment = bursts[0].captured_at + timedelta(seconds=later)
                        bursts.append(replace(second, captured_at=moment))
                    else:
                        time.sleep(args.delay)
                        bursts.append(look(0.0))
                except SourceError as error:
                    print(f"sky-monitor: no second look ({error.code}); one look only", file=sys.stderr)
    except SourceError as error:
        print(f"sky-monitor: {error}", file=sys.stderr)
        return 2

    analyzer = CameraAnalyzer(params, region=region, interval_seconds=args.seconds + args.delay)
    for burst in bursts:
        assessment = analyzer.assess(burst)
    height, width = bursts[-1].images[-1].shape
    result = {
        "input": path.name if local else redact(args.input),
        "width": width,
        "height": height,
        "looks": len(bursts),
        # One-off: hot pixels and lamps are not learned, so the monitor itself may count fewer stars.
        "provisional": True,
        **assessment.to_json(),
    }
    if out is not None and assessment.overlay is not None:
        caption = (
            f"{assessment.verdict.value}  sky with stars {assessment.coverage or 0:.0%}  stars {assessment.star_count}"
        )
        render_preview(assessment.overlay, caption).save(out)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    try:
        text = (config.output.directory / "status.json").read_text(encoding="utf-8")
        status = json.loads(text)
        valid_until = datetime.fromisoformat(status["validUntil"])
    except (OSError, ValueError, KeyError, TypeError):
        print("sky-monitor: no readable status; is the monitor running?", file=sys.stderr)
        return 2
    if datetime.now(UTC) > valid_until:
        print(f"sky-monitor: the status ran out at {status['validUntil']}; treat it as not safe", file=sys.stderr)
        return 2
    print(json.dumps(status, ensure_ascii=False, indent=2) if args.json else status.get("message", ""))
    return 0 if status.get("imagingOk" if args.imaging else "safe") is True else 1


def _calibrate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    now = datetime.now(UTC)
    cutoff = now - timedelta(minutes=args.minutes)
    counts: dict[str, list[int]] = {}
    moonlit = False
    for path in sorted((config.output.directory / "history").glob("????-??-??.jsonl"))[-3:]:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
                if datetime.fromisoformat(entry["updatedAt"]) < cutoff:
                    continue
                moon = entry.get("moon") or {}
                moonlit = moonlit or (moon.get("altitudeDeg", -90) > 0 and moon.get("illumination", 0) > 0.3)
                for camera in entry["cameras"]:
                    if camera["verdict"] != "UNKNOWN":
                        counts.setdefault(camera["name"], []).append(int(camera["starCount"]))
            except (ValueError, KeyError, TypeError):
                continue

    reference_path = config.output.directory / "reference.json"
    try:
        stored = json.loads(reference_path.read_text(encoding="utf-8"))
        references = dict(stored["cameras"])
    except (OSError, ValueError, KeyError, TypeError):
        references = {}
    complete = True
    for camera in config.cameras:
        values = sorted(counts.get(camera.name, []))
        if len(values) < 5:
            print(f"{camera.name}: only {len(values)} look(s) in the last {args.minutes:g} min; not calibrated")
            complete = False
            continue
        median = statistics.median(values)
        spread = (values[int(0.9 * (len(values) - 1))] - values[int(0.1 * (len(values) - 1))]) / max(median, 1)
        if spread > 0.5 and not args.force:
            print(
                f"{camera.name}: the star count varied by {spread:.0%} around {median:g}; a clear sky is steadier. "
                "Not calibrated (--force to accept)."
            )
            complete = False
            continue
        references[camera.name] = {
            "starCount": int(median),
            "looks": len(values),
            "calibratedAt": now.isoformat(timespec="seconds"),
        }
        needed = max(5, math.ceil(camera.analysis.min_star_ratio * median))
        print(f"{camera.name}: a clear sky shows {int(median)} stars; from now on {needed} or more count as clear")
    if references:
        write_text_atomic(reference_path, json.dumps({"cameras": references}, indent=2) + "\n")
    if moonlit:
        print("Note: the Moon was up; a reference taken under moonlight is low. Repeat on a moonless clear night.")
    return 0 if complete else 1


class _Timed:
    """A forecast source that notes how long it took and how it ended."""

    def __init__(self, provider, log: list[dict[str, object]]) -> None:
        self.name = provider.name
        self._provider = provider
        self._log = log

    def fetch(self, latitude: float, longitude: float) -> list[Forecast]:
        entry: dict[str, object] = {"provider": self.name, "seconds": None, "ok": False, "forecasts": 0, "error": None}
        self._log.append(entry)
        started = time.monotonic()
        try:
            forecasts = self._provider.fetch(latitude, longitude)
        except Exception as error:
            entry["error"] = str(error) if isinstance(error, ProviderError) else f"{type(error).__name__}: {error}"
            raise
        finally:
            entry["seconds"] = round(time.monotonic() - started, 2)
        entry.update(ok=True, forecasts=len(forecasts))
        return forecasts


def _night_hours(config: Config, now: datetime) -> list[datetime]:
    """The hours of darkness ahead (24 h at most), as the monitor shows them."""

    start = hour_of(now)
    hours = [start + timedelta(hours=index) for index in range(24)]
    site = config.site
    if site.latitude is None or site.longitude is None:
        return hours[:12]
    gate = config.decision.sun_max_altitude_deg
    dark = [
        h for h in hours if sun_moon(h + timedelta(minutes=30), site.latitude, site.longitude).sun.altitude_deg <= gate
    ]
    return dark or hours[:12]


def _local(text: object, layout: str = "%H:%M") -> str:
    if not text:
        return "-"
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone().strftime(layout)


def _percent(value: object) -> str:
    return "-" if value is None else f"{float(value):.0%}"  # type: ignore[arg-type]


def _figure(value: object, digits: int = 1) -> str:
    return "-" if value is None else f"{float(value):.{digits}f}"  # type: ignore[arg-type]


def _print_forecast(summary: dict[str, object], site: str, asked: list[dict[str, object]]) -> None:
    providers: list[dict[str, object]] = summary["providers"]  # type: ignore[assignment]
    if asked:
        print("Asked the sources:")
        for entry in asked:
            outcome = f"OK, {entry['forecasts']} forecast(s)" if entry["ok"] else f"FAILED: {entry['error']}"
            print(f"  {entry['provider']:<11} {float(entry['seconds'] or 0.0):5.1f} s  {outcome}")
        print()
    answered = sum(1 for provider in providers if provider["model"])
    print(
        f"Tonight's forecast for {site}: {answered} source(s), refreshed {_local(summary['refreshedAt'], '%Y-%m-%d %H:%M')}"
    )
    columns = ("hour", "cloud", "low", "mid", "high", "rain", "mm", "wind", "gust", "humid", "seeing", "transp")
    print("  ".join(f"{name:>6}" for name in columns))
    for hour in summary["night"]:  # type: ignore[union-attr]
        row = (
            _local(hour["t"]),
            _percent(hour["cloud"]),
            _percent(hour["cloudLow"]),
            _percent(hour["cloudMid"]),
            _percent(hour["cloudHigh"]),
            _percent(hour["rainProbability"]),
            _figure(hour["rainMm"]),
            _figure(hour["windMs"]),
            _figure(hour["gustMs"]),
            _percent(hour["humidity"]),
            _figure(hour["seeing"], 0),
            _figure(hour["transparency"], 0),
        )
        print("  ".join(f"{value:>6}" for value in row))
    if float(summary["rainWindowHours"] or 0.0) > 0:  # type: ignore[arg-type]
        print(f"Rain forecast within {float(summary['rainWindowHours']):g} h: {'yes' if summary['rainSoon'] else 'no'}")  # type: ignore[arg-type]
    print()
    width = max([len("source")] + [len(str(provider["key"])) for provider in providers])
    print(
        f"{'source':<{width}}  {'issued':>11}  {'fetched':>7}  {'error %':>7}  {'hours':>5}  {'right':>5}  {'weight':>6}  problem"
    )
    for provider in providers:
        skill = provider["skill"] or {}
        print(
            f"{provider['key']!s:<{width}}  {_local(provider['issuedAt'], '%m-%d %H:%M'):>11}  "
            f"{_local(provider['fetchedAt']):>7}  {_percent(skill.get('mae')):>7}  {skill.get('n', '-')!s:>5}  "
            f"{_percent(skill.get('hitRate')):>5}  {_figure(provider['weight'], 2):>6}  {provider['error'] or ''}"
        )
    if not providers:
        print("(no source has answered yet)")
    print(
        "error %: mean error of the cloud fraction against the cameras; right: clear/cloudy called right; both over the last 30 nights"
    )


def _forecast(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    site = config.site
    settings = config.forecast
    if not settings.enabled or site.latitude is None or site.longitude is None:
        print(
            "sky-monitor: the forecast is off; it needs [site] latitude and longitude and [forecast] enabled = true",
            file=sys.stderr,
        )
        return 2
    directory = config.output.directory / "forecast"
    if not args.refresh and not (directory / "latest.json").is_file():
        print("sky-monitor: no forecast yet; start the monitor, or ask the sources now with --refresh", file=sys.stderr)
        return 2
    asked: list[dict[str, object]] = []
    try:
        service = ForecastService(
            [_Timed(provider, asked) for provider in build_providers(settings)] if args.refresh else [],
            site.latitude,
            site.longitude,
            directory,
            refresh_seconds=settings.refresh_minutes * 60.0,
            coordinate_precision=settings.coordinate_precision,
        )
    except OSError as error:
        print(f"sky-monitor: cannot use {directory}: {error}", file=sys.stderr)
        return 2
    if args.refresh:
        service.refresh()
    summary = service.summary(
        _night_hours(config, datetime.now(UTC)),
        rain_window_hours=settings.rain_veto_hours,
        rain_probability=settings.rain_veto_probability,
        rain_mm=settings.rain_veto_mm,
    )
    if args.json:
        print(json.dumps({**summary, "asked": asked} if args.refresh else summary, ensure_ascii=False, indent=2))
    else:
        _print_forecast(summary, site.name, asked)
    return 0 if any(provider["model"] for provider in summary["providers"]) else 1  # type: ignore[union-attr]


def _devices(holder: SafetyHolder, config: Config) -> list[SafetyDevice]:
    def identity(role: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sky-monitor://{socket.gethostname()}/{config.path}/{role}"))

    site = config.site.name
    return [
        SafetyDevice(
            name="Sky Monitor - roof may open",
            description=f"{site}: the sky has stayed clear long enough for the roof to be open",
            unique_id=identity("roof"),
            is_safe=holder.roof_may_open,
        ),
        SafetyDevice(
            name="Sky Monitor - imaging may run",
            description=f"{site}: the roof may be open, the sky is clear now and it is dark",
            unique_id=identity("imaging"),
            is_safe=holder.imaging_may_run,
        ),
    ]


def _lan_address() -> str | None:
    """This computer's address on its network (no packet is sent)."""

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        return str(probe.getsockname()[0])
    except OSError:
        return None
    finally:
        probe.close()


def _serving(config: Config) -> tuple[str, bool]:
    """Where the server listens, and whether other computers may use the safety devices."""

    host = config.alpaca.host
    devices_shared = host not in ("127.0.0.1", "localhost", "::1")
    if config.web.share == "lan" and not devices_shared:
        # The page goes to the network; the devices stay for this computer (checked per request).
        return "0.0.0.0", False
    return host, devices_shared


def _page_url(config: Config) -> str:
    host = "127.0.0.1" if config.alpaca.host in ("0.0.0.0", "") else config.alpaca.host
    return f"http://{host}:{config.alpaca.port}/" if config.alpaca.enabled else ""


def _start(config: Config, *, verbose: bool, console: bool) -> tuple[Monitor, AlpacaServer | None] | None:
    """The monitor with its server, logging set up; None (after telling why) when it cannot start."""

    holder = SafetyHolder()
    try:
        monitor = Monitor(config, holder=holder, panel_url=_page_url(config))
    except OSError as error:
        _tell(f"sky-monitor: cannot use {config.output.directory}: {error}")
        return None
    handlers: list[logging.Handler] = [logging.StreamHandler()] if console else []
    handlers.append(
        logging.handlers.RotatingFileHandler(
            config.output.directory / "sky-monitor.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
    )
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )
    server = None
    if config.alpaca.enabled:
        panel = ControlPanel(
            monitor,
            language=resolve_language(config.language),
            config_path=config.path,
            preview_dir=config.output.directory / "preview",
            site=config.site.name,
            email_enabled=config.email.enabled,
            share=config.web.share,
            control_code=config.web.control_code,
            lan_url=f"http://{address}:{config.alpaca.port}/" if (address := _lan_address()) else "",
        )
        bind_host, devices_shared = _serving(config)
        # The port may still be held by an instance that is shutting down: wait for it a while.
        deadline = time.monotonic() + PORT_WAIT_SECONDS
        while True:
            try:
                server = AlpacaServer(
                    _devices(holder, config),
                    host=bind_host,
                    port=config.alpaca.port,
                    discovery=config.alpaca.discovery,
                    location=config.site.name,
                    panel=panel,
                    alpaca_remote=devices_shared,
                )
                break
            except OSError as error:
                if time.monotonic() < deadline:
                    logging.warning(
                        "port %s:%d is busy (%s); trying again", config.alpaca.host, config.alpaca.port, error
                    )
                    time.sleep(2.0)
                    continue
                monitor.close()
                reason = (
                    f"sky-monitor: cannot serve on {bind_host}:{config.alpaca.port}: {error} (is it already running?)"
                )
                logging.error(reason)
                _tell(reason)
                return None
        server.start()
        monitor.set_automation_probe(server.any_connected)
        logging.info(
            "status page on %s:%d (%s), safety devices for %s",
            bind_host,
            server.port,
            config.web.share,
            "the network" if devices_shared else "this computer",
        )
    logging.info(
        "watching %d camera(s) every %g s; data in %s",
        len(config.cameras),
        config.interval_seconds,
        config.output.directory.name,
    )
    return monitor, server


def _run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    started = _start(config, verbose=args.verbose, console=True)
    if started is None:
        return 2
    monitor, server = started
    stop = threading.Event()
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: stop.set())
    try:
        monitor.run(stop)
    finally:
        if server is not None:
            server.stop()
        monitor.close()
        print("sky-monitor: stopped; the published status now reads not safe", file=sys.stderr)
    return 0


def _tray(args: argparse.Namespace) -> int:
    path = Path(args.config) if args.config else default_config_path()
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TEMPLATE, encoding="utf-8")
        _tell(f"Sky Monitor wrote its settings file: {path}\nFill in the camera and start it again.")
        if sys.platform == "win32":
            os.startfile(path.parent)  # type: ignore[attr-defined]
        return 2
    try:
        config = load_config(path)
    except ConfigError as error:
        _tell(f"sky-monitor: settings: {error}")
        return 2
    started = _start(config, verbose=args.verbose, console=not getattr(sys, "frozen", False))
    if started is None:
        return 2
    monitor, server = started
    stop = threading.Event()
    try:
        return run_tray(monitor, stop, language=resolve_language(config.language), url=_page_url(config))
    finally:
        stop.set()
        if server is not None:
            server.stop()
        monitor.close()


def _roof(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    log = NightLog(config.output.directory / "state" / "night.json", config.site.longitude)
    now = datetime.now(UTC)
    if args.mark == "status":
        state = log.current(now)
    elif args.mark == "read":
        state = log.set_acknowledged(now, True)
    else:
        state = log.set_roof_opened(now, args.mark == "opened")
    print(json.dumps(state.to_json(), ensure_ascii=False))
    return 0


def _test_email(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if not config.email.enabled:
        print("sky-monitor: [email] enabled = false in the settings", file=sys.stderr)
        return 2
    try:
        Monitor(config, panel_url=_page_url(config)).send_test_email()
    except EmailError as error:
        print(f"sky-monitor: mail failed: {error}", file=sys.stderr)
        return 1
    print(f"sky-monitor: test mail sent to {', '.join(config.email.recipients)}")
    return 0


def _autostart(args: argparse.Namespace) -> int:
    """A shortcut in the Windows Startup folder that opens the tray at log-on."""

    if sys.platform != "win32":
        print("sky-monitor: autostart is for Windows", file=sys.stderr)
        return 2
    folder = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    link = folder / "Sky Monitor.lnk"
    if args.disable:
        link.unlink(missing_ok=True)
        print(f"sky-monitor: removed {link}")
        return 0
    config = Path(args.config).resolve() if args.config else default_config_path()
    if getattr(sys, "frozen", False):
        target = Path(sys.executable).with_name("sky-monitor-tray.exe")
        if not target.is_file():
            target = Path(sys.executable)
        arguments = f'--config "{config}"'
    else:
        target = Path(sys.executable).with_name("pythonw.exe")
        arguments = f'-m skymonitor tray --config "{config}"'

    def quoted(value: object) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    script = (
        f"$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut({quoted(link)}); "
        f"$shortcut.TargetPath = {quoted(target)}; $shortcut.Arguments = {quoted(arguments)}; "
        f"$shortcut.WorkingDirectory = {quoted(config.parent)}; $shortcut.Save()"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            check=True,
            timeout=60,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as error:
        print(f"sky-monitor: could not create the shortcut: {error}", file=sys.stderr)
        return 1
    print(f"sky-monitor: {link} starts the tray at log-on")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            # A console that cannot show a character must not stop the monitor.
            stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(
        prog="sky-monitor",
        description="Decide from sky cameras whether the observatory roof may open and imaging may run.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="write a settings file to fill in")
    init.add_argument("path", nargs="?", default="sky-monitor.toml")
    init.set_defaults(handler=_init)

    def with_config(name: str, handler, summary: str) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=summary)
        command.add_argument("--config", default="sky-monitor.toml", help="settings file (default: sky-monitor.toml)")
        command.set_defaults(handler=handler)
        return command

    doctor = with_config("doctor", _doctor, "check ffmpeg, the site and every camera once")
    doctor.add_argument("--json", action="store_true")

    run = with_config("run", _run, "watch the sky until stopped")
    run.add_argument("--verbose", action="store_true")

    tray = commands.add_parser("tray", help="watch the sky behind a tray icon (the installed product)")
    tray.add_argument("--config", default="", help="settings file (default: the product's own location)")
    tray.add_argument("--verbose", action="store_true")
    tray.set_defaults(handler=_tray)

    roof = with_config(
        "roof", _roof, "mark the roof opened tonight, cancel the mark, mark the reminder read, or show the night"
    )
    roof.add_argument("mark", choices=("opened", "cancel", "read", "status"))

    with_config("test-email", _test_email, "send a test mail with the [email] settings")

    autostart = commands.add_parser("autostart", help="start the tray at Windows log-on")
    autostart.add_argument("--config", default="")
    autostart.add_argument("--disable", action="store_true")
    autostart.set_defaults(handler=_autostart)

    status = with_config(
        "status", _status, "print the running monitor's answer; exit 0 safe, 1 not safe, 2 no fresh answer"
    )
    status.add_argument("--imaging", action="store_true", help="answer for imaging instead of the roof")
    status.add_argument("--json", action="store_true")

    forecast = with_config(
        "forecast",
        _forecast,
        "print tonight's forecast: the sources' consensus hour by hour and each source's record here",
    )
    forecast.add_argument(
        "--refresh", action="store_true", help="ask every source now (and keep the answers) before printing"
    )
    forecast.add_argument("--json", action="store_true")

    calibrate = with_config(
        "calibrate", _calibrate, "on a clear moonless night: keep the current star count as the reference"
    )
    calibrate.add_argument("--minutes", type=float, default=30.0, help="how far back to read the history (default 30)")
    calibrate.add_argument("--force", action="store_true", help="accept an unsteady star count")

    analyze = commands.add_parser("analyze", help="analyse a picture, a video file or a stream address once")
    analyze.add_argument("input")
    analyze.add_argument("--out", help="write the marked-up frame to this new file")
    analyze.add_argument("--frames", type=int, default=16)
    analyze.add_argument("--seconds", type=float, default=8.0)
    analyze.add_argument("--delay", type=float, default=15.0, help="seconds until the second look (0: one look)")
    analyze.add_argument("--start", type=float, default=0.0, help="where to start in a video file, in seconds")
    analyze.add_argument("--max-width", type=int, default=1920)
    analyze.add_argument("--threshold", type=float, default=AnalysisParams().detection.threshold_sigma)
    analyze.add_argument("--min-stars", type=int, default=AnalysisParams().min_stars)
    analyze.add_argument("--rectangle", type=float, nargs=4, metavar=("X0", "Y0", "X1", "Y1"))
    analyze.add_argument("--circle", type=float, nargs=3, metavar=("X", "Y", "R"))
    analyze.add_argument("--ffmpeg", default="")
    analyze.set_defaults(handler=_analyze)

    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except ConfigError as error:
        print(f"sky-monitor: settings: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
