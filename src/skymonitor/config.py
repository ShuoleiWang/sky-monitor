"""The monitor's settings file (TOML), read strictly: a typo must not loosen a threshold."""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from .analysis import AnalysisParams, SkyRegion
from .decision import DecisionParams
from .mail import EmailSettings
from .providers import KNOWN as KNOWN_PROVIDERS

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
CONTROL_CODE = re.compile(r"^[A-Za-z0-9]{4,32}$")
# Share of the sky cells with stars that counts as clear, and below which it is cloudy.
CLOUD_TOLERANCE = {"lenient": (0.55, 0.30), "standard": (0.70, 0.40), "strict": (0.85, 0.60)}
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_MISSING = object()
_KIND_NAMES = {str: "text", int: "a whole number", float: "a number", bool: "true or false", list: "a list"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SiteConfig:
    name: str = "Observatory"
    # Degrees, north and east positive; without them the Sun and Moon are not used.
    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True)
class CameraConfig:
    name: str
    # "stream" (anything ffmpeg reads), "snapshot" (an HTTP picture) or "file".
    kind: str
    address: str
    frames: int = 16
    seconds: float = 8.0
    timeout_seconds: float = 0.0
    max_width: int = 1920
    input_options: tuple[str, ...] = ()
    username: str = ""
    password: str = ""
    insecure_tls: bool = False
    stale_after_seconds: float = 300.0
    region: SkyRegion | None = None
    analysis: AnalysisParams = AnalysisParams()


@dataclass(frozen=True)
class OutputConfig:
    directory: Path
    preview: bool = True
    history_days: int = 60
    # Keep the stacked frame of every camera this often (and when its verdict changes); 0: never.
    archive_minutes: float = 0.0
    archive_days: int = 7


@dataclass(frozen=True)
class AlpacaConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 11112
    discovery: bool = True


@dataclass(frozen=True)
class WebConfig:
    # "local": the status page opens on this computer only; "lan": other computers on the
    # network may view it, read-only.
    share: str = "local"
    # Lets the page's buttons work from other computers; empty: on this computer only.
    control_code: str = ""


@dataclass(frozen=True)
class ForecastConfig:
    enabled: bool = True
    providers: tuple[str, ...] = ("open-meteo", "7timer")
    open_meteo_models: tuple[str, ...] = (
        "best_match",
        "ecmwf_ifs025",
        "gfs_global",
        "icon_global",
        "cma_grapes_global",
    )
    refresh_minutes: float = 60.0
    caiyun_token: str = ""
    qweather_key: str = ""
    qweather_host: str = "devapi.qweather.com"
    # > 0: the roof does not open while rain is forecast within this many hours.
    rain_veto_hours: float = 0.0
    rain_veto_probability: float = 0.5
    rain_veto_mm: float = 0.2
    # Decimals of the coordinates sent to the sources (2 is about a kilometre).
    coordinate_precision: int = 2


@dataclass(frozen=True)
class OperatorConfig:
    # Once the operator marks the roof open, stop watching for the rest of the night.
    pause_when_opened: bool = True


@dataclass(frozen=True)
class Config:
    path: Path
    language: str
    interval_seconds: float
    # Between looks while there is nothing to watch: daylight without site coordinates,
    # or the roof marked open.
    idle_interval_seconds: float
    ffmpeg: str
    site: SiteConfig
    decision: DecisionParams
    cameras: tuple[CameraConfig, ...]
    output: OutputConfig
    alpaca: AlpacaConfig
    notify_command: tuple[str, ...]
    email: EmailSettings = EmailSettings()
    operator: OperatorConfig = OperatorConfig()
    forecast: ForecastConfig = ForecastConfig()
    web: WebConfig = WebConfig()


class _Table:
    """A TOML table whose settings are taken one by one; what is left over is an error."""

    def __init__(self, data: object, where: str) -> None:
        if not isinstance(data, dict):
            raise ConfigError(f"{where}: expected a table")
        self._data = dict(data)
        self.where = where

    def _path(self, key: str) -> str:
        return f"{self.where}.{key}" if self.where else key

    def take(
        self,
        key: str,
        kind: type,
        default: object = _MISSING,
        *,
        low: float | None = None,
        high: float | None = None,
        choices: tuple[str, ...] = (),
    ):
        if key not in self._data:
            if default is _MISSING:
                raise ConfigError(f"{self._path(key)}: required")
            return default
        value = self._data.pop(key)
        if kind is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if not isinstance(value, kind) or (kind is not bool and isinstance(value, bool)):
            raise ConfigError(f"{self._path(key)}: expected {_KIND_NAMES[kind]}")
        if (low is not None and value < low) or (high is not None and value > high):
            raise ConfigError(f"{self._path(key)}: {value} is outside {low} .. {high}")
        if choices and value not in choices:
            raise ConfigError(f"{self._path(key)}: expected one of {', '.join(choices)}")
        return value

    def fractions(self, key: str, count: int) -> tuple[float, ...] | None:
        """``count`` numbers between 0 and 1: a place in the frame."""

        values = self.take(key, list, None)
        return None if values is None else _fractions(values, count, self._path(key))

    def table(self, key: str) -> _Table:
        return _Table(self._data.pop(key, {}), self._path(key))

    def finish(self) -> None:
        if self._data:
            names = ", ".join(sorted(self._data))
            raise ConfigError(f"{self.where or 'top level'}: unknown setting(s): {names}")


def _fractions(values: object, count: int, where: str) -> tuple[float, ...]:
    if (
        not isinstance(values, list)
        or len(values) != count
        or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in values)
        or any(not 0.0 <= v <= 1.0 for v in values)
    ):
        raise ConfigError(f"{where}: expected {count} numbers between 0 and 1")
    return tuple(float(v) for v in values)


def _expand(text: str, where: str) -> str:
    """Replace ``${NAME}`` by the environment variable, so that passwords can stay out of the file."""

    def substitute(match: re.Match[str]) -> str:
        value = os.environ.get(match.group(1))
        if value is None:
            raise ConfigError(f"{where}: the environment variable {match.group(1)} is not set")
        return value

    return _VARIABLE.sub(substitute, text)


def _analysis(table: _Table, base: AnalysisParams) -> AnalysisParams:
    detection = replace(
        base.detection,
        threshold_sigma=table.take("threshold_sigma", float, base.detection.threshold_sigma, low=3.0, high=20.0),
        psf_sigma=table.take("psf_sigma", float, base.detection.psf_sigma, low=0.6, high=4.0),
        point_ratio_min=table.take("point_ratio_min", float, base.detection.point_ratio_min, low=1.5, high=6.0),
    )
    params = replace(
        base,
        detection=detection,
        clear_coverage=table.take("clear_coverage", float, base.clear_coverage, low=0.3, high=1.0),
        cloudy_coverage=table.take("cloudy_coverage", float, base.cloudy_coverage, low=0.1, high=0.9),
        min_stars=table.take("min_stars", int, base.min_stars, low=5),
        min_star_ratio=table.take("min_star_ratio", float, base.min_star_ratio, low=0.1, high=1.0),
        reference_star_count=table.take("reference_star_count", int, base.reference_star_count, low=0),
        grid_cells=table.take("grid_cells", int, base.grid_cells, low=0, high=400),
        drift_px_per_minute=table.take("drift_px_per_minute", float, base.drift_px_per_minute, low=0.0, high=60.0),
        fixed_window_minutes=table.take("fixed_window_minutes", float, base.fixed_window_minutes, low=10.0, high=240.0),
    )
    table.finish()
    if params.cloudy_coverage >= params.clear_coverage:
        raise ConfigError(f"{table.where}: cloudy_coverage must be below clear_coverage")
    # Half the window, as in the defaults: the fixed sources are known before a verdict is given.
    return replace(params, fixed_mature_minutes=params.fixed_window_minutes / 2.0)


def _camera(table: _Table, base: AnalysisParams, root: Path) -> CameraConfig:
    where = table.where
    name = table.take("name", str)
    if not _NAME.match(name):
        raise ConfigError(f"{where}.name: use letters, digits, '-' and '_' (it names files)")
    kind = table.take("type", str, choices=("stream", "snapshot", "file"))
    if kind == "file":
        address = str((root / table.take("path", str)).resolve())
    else:
        address = _expand(table.take("url", str), f"{where}.url")
        if kind == "snapshot" and not address.lower().startswith(("http://", "https://")):
            raise ConfigError(f"{where}.url: a snapshot address starts with http:// or https://")
    options = table.take("input_options", list, [])
    if any(not isinstance(option, str) for option in options):
        raise ConfigError(f"{where}.input_options: expected a list of text")
    exclude = table.take("exclude", list, [])
    mask = table.take("mask", str, "")
    region = SkyRegion(
        rectangle=table.fractions("rectangle", 4),
        circle=table.fractions("circle", 3),
        exclude=tuple(_fractions(box, 4, f"{where}.exclude") for box in exclude),
        mask_path=(root / mask).resolve() if mask else None,
    )
    if region.mask_path is not None and not region.mask_path.is_file():
        raise ConfigError(f"{where}.mask: no such file")
    camera = CameraConfig(
        name=name,
        kind=kind,
        address=address,
        frames=table.take("frames", int, 16, low=1, high=120),
        seconds=table.take("seconds", float, 8.0, low=0.5, high=60.0),
        timeout_seconds=table.take("timeout_seconds", float, 0.0, low=0.0, high=300.0),
        max_width=table.take("max_width", int, 1920, low=0, high=8192),
        input_options=tuple(_expand(option, f"{where}.input_options") for option in options),
        username=table.take("username", str, ""),
        password=_expand(table.take("password", str, ""), f"{where}.password"),
        insecure_tls=table.take("insecure_tls", bool, False),
        stale_after_seconds=table.take("stale_after_seconds", float, 300.0, low=30.0),
        region=region if (region.rectangle or region.circle or region.exclude or mask) else None,
        analysis=_analysis(table.table("analysis"), base),
    )
    table.finish()
    return camera


def parse_config(data: dict, path: Path) -> Config:
    root = path.parent
    top = _Table(data, "")
    site_table = top.table("site")
    site = SiteConfig(
        name=site_table.take("name", str, "Observatory"),
        latitude=site_table.take("latitude", float, None, low=-90.0, high=90.0),
        longitude=site_table.take("longitude", float, None, low=-180.0, high=360.0),
    )
    site_table.finish()
    if (site.latitude is None) != (site.longitude is None):
        raise ConfigError("site: give both latitude and longitude, or neither")

    decision_table = top.table("decision")
    interval = decision_table.take("interval_seconds", float, 60.0, low=10.0, high=600.0)
    idle_interval = decision_table.take("idle_interval_seconds", float, 300.0, low=60.0, high=3600.0)
    tolerance = decision_table.take("cloud_tolerance", str, "standard", choices=tuple(CLOUD_TOLERANCE))
    clear_coverage, cloudy_coverage = CLOUD_TOLERANCE[tolerance]
    base = _analysis(
        top.table("analysis"),
        replace(AnalysisParams(), clear_coverage=clear_coverage, cloudy_coverage=cloudy_coverage),
    )
    decision = DecisionParams(
        open_after_seconds=60.0 * decision_table.take("open_after_minutes", float, 10.0, low=1.0, high=120.0),
        open_coverage=base.clear_coverage,
        hold_coverage=base.cloudy_coverage,
        close_after_cycles=decision_table.take("close_after_cycles", int, 2, low=1, high=10),
        partly_close_seconds=60.0 * decision_table.take("partly_close_minutes", float, 30.0, low=0.0, high=600.0),
        unknown_grace_seconds=decision_table.take("unknown_grace_seconds", float, 120.0, low=0.0, high=900.0),
        max_gap_seconds=max(300.0, 4.0 * interval),
        min_cameras=decision_table.take("min_cameras", int, 1, low=1),
        sun_max_altitude_deg=decision_table.take("sun_max_altitude_deg", float, -8.0, low=-18.0, high=0.0),
        imaging_sun_altitude_deg=decision_table.take("imaging_sun_altitude_deg", float, -12.0, low=-18.0, high=0.0),
        imaging_moon_max_illumination=decision_table.take(
            "imaging_moon_max_illumination", float, 1.0, low=0.0, high=1.0
        ),
    )
    decision_table.finish()
    if decision.imaging_sun_altitude_deg > decision.sun_max_altitude_deg:
        raise ConfigError("decision: imaging_sun_altitude_deg must not be above sun_max_altitude_deg")

    entries = top.take("camera", list, [])
    cameras = tuple(_camera(_Table(entry, f"camera[{index}]"), base, root) for index, entry in enumerate(entries))
    if not cameras:
        raise ConfigError("camera: at least one [[camera]] is required")
    if len({camera.name for camera in cameras}) != len(cameras):
        raise ConfigError("camera: every camera needs its own name")
    if decision.min_cameras > len(cameras):
        raise ConfigError("decision.min_cameras: more than the cameras that are configured")

    output_table = top.table("output")
    output = OutputConfig(
        directory=(root / output_table.take("directory", str, "sky-monitor-data")).resolve(),
        preview=output_table.take("preview", bool, True),
        history_days=output_table.take("history_days", int, 60, low=1),
        archive_minutes=output_table.take("archive_minutes", float, 0.0, low=0.0, high=1440.0),
        archive_days=output_table.take("archive_days", int, 7, low=1),
    )
    output_table.finish()

    alpaca_table = top.table("alpaca")
    alpaca = AlpacaConfig(
        enabled=alpaca_table.take("enabled", bool, True),
        host=alpaca_table.take("host", str, "127.0.0.1"),
        port=alpaca_table.take("port", int, 11112, low=1, high=65535),
        discovery=alpaca_table.take("discovery", bool, True),
    )
    alpaca_table.finish()

    web_table = top.table("web")
    web = WebConfig(
        share=web_table.take("share", str, "local", choices=("local", "lan")),
        control_code=_expand(web_table.take("control_code", str, ""), "web.control_code"),
    )
    web_table.finish()
    if web.control_code and not CONTROL_CODE.match(web.control_code):
        raise ConfigError("web.control_code: 4 to 32 letters or digits")

    notify_table = top.table("notify")
    command = notify_table.take("command", list, [])
    if any(not isinstance(part, str) for part in command):
        raise ConfigError("notify.command: expected a list of text")
    notify_table.finish()

    email_table = top.table("email")
    recipients = email_table.take("to", list, [])
    if any(not isinstance(address, str) or "@" not in address for address in recipients):
        raise ConfigError("email.to: expected a list of addresses")
    email = EmailSettings(
        enabled=email_table.take("enabled", bool, False),
        host=email_table.take("host", str, ""),
        port=email_table.take("port", int, 465, low=1, high=65535),
        security=email_table.take("security", str, "ssl", choices=("ssl", "starttls", "none")),
        username=email_table.take("username", str, ""),
        password=_expand(email_table.take("password", str, ""), "email.password"),
        sender=email_table.take("from", str, ""),
        recipients=tuple(recipients),
        subject_prefix=email_table.take("subject_prefix", str, "[Sky Monitor]"),
        on_clear=email_table.take("on_clear", bool, True),
        on_clear_lost=email_table.take("on_clear_lost", bool, True),
        repeat_minutes=email_table.take("repeat_minutes", float, 30.0, low=0.0, high=1440.0),
    )
    email_table.finish()
    if email.enabled and (not email.host or not email.recipients):
        raise ConfigError("email: host and to are required when enabled")

    operator_table = top.table("operator")
    operator = OperatorConfig(pause_when_opened=operator_table.take("pause_when_opened", bool, True))
    operator_table.finish()

    forecast_table = top.table("forecast")
    providers = forecast_table.take("providers", list, list(ForecastConfig().providers))
    if any(not isinstance(name, str) or name not in KNOWN_PROVIDERS for name in providers):
        raise ConfigError(f"forecast.providers: expected names from {', '.join(KNOWN_PROVIDERS)}")
    models = forecast_table.take("open_meteo_models", list, list(ForecastConfig().open_meteo_models))
    if any(not isinstance(model, str) or not model for model in models):
        raise ConfigError("forecast.open_meteo_models: expected a list of model names")
    forecast = ForecastConfig(
        enabled=forecast_table.take("enabled", bool, True),
        providers=tuple(providers),
        open_meteo_models=tuple(models),
        refresh_minutes=forecast_table.take("refresh_minutes", float, 60.0, low=15.0, high=720.0),
        caiyun_token=_expand(forecast_table.take("caiyun_token", str, ""), "forecast.caiyun_token"),
        qweather_key=_expand(forecast_table.take("qweather_key", str, ""), "forecast.qweather_key"),
        qweather_host=forecast_table.take("qweather_host", str, "devapi.qweather.com"),
        rain_veto_hours=forecast_table.take("rain_veto_hours", float, 0.0, low=0.0, high=24.0),
        rain_veto_probability=forecast_table.take("rain_veto_probability", float, 0.5, low=0.05, high=1.0),
        rain_veto_mm=forecast_table.take("rain_veto_mm", float, 0.2, low=0.0, high=50.0),
        coordinate_precision=forecast_table.take("coordinate_precision", int, 2, low=0, high=4),
    )
    forecast_table.finish()
    if forecast.enabled and site.latitude is None:
        forecast = replace(forecast, enabled=False)

    config = Config(
        path=path,
        language=top.take("language", str, "auto", choices=("auto", "zh", "en")),
        interval_seconds=interval,
        idle_interval_seconds=idle_interval,
        ffmpeg=top.take("ffmpeg", str, ""),
        site=site,
        decision=decision,
        cameras=cameras,
        output=output,
        alpaca=alpaca,
        notify_command=tuple(command),
        email=email,
        operator=operator,
        forecast=forecast,
        web=web,
    )
    top.finish()
    return config


def set_setting(path: Path, section: str, key: str, value: str) -> None:
    """Write ``key = "value"`` into ``[section]`` of the settings file, keeping everything else."""

    path = Path(path)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    literal = json.dumps(value, ensure_ascii=False)
    header = re.compile(rf"^\s*\[{re.escape(section)}\]\s*(#.*)?$")
    any_header = re.compile(r"^\s*\[")
    assignment = re.compile(rf"^\s*{re.escape(key)}\s*=")
    start = next((index for index, line in enumerate(lines) if header.match(line)), None)
    if start is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines += ["\n", f"[{section}]\n", f"{key} = {literal}\n"]
    else:
        end = next((i for i in range(start + 1, len(lines)) if any_header.match(lines[i])), len(lines))
        found = next((i for i in range(start + 1, end) if assignment.match(lines[i])), None)
        if found is None:
            lines.insert(start + 1, f"{key} = {literal}\n")
        else:
            lines[found] = f"{key} = {literal}\n"
    updated = "".join(lines)
    try:
        tomllib.loads(updated)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path.name}: the change would not parse: {error}") from error
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.replace(temporary, path)


def load_config(path: Path | str) -> Config:
    path = Path(path).resolve()
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except OSError as error:
        raise ConfigError(f"{path.name}: {error.strerror or error}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path.name}: {error}") from error
    return parse_config(data, path)


TEMPLATE = """\
# Sky Monitor settings.  Remove the '#' in front of a setting to change it;
# an unknown or misspelled setting is an error, never silently ignored.

# language = "auto"            # "zh", "en" or "auto" (follows the system)
# ffmpeg = ""                  # full path of ffmpeg when it is not on PATH

[site]
name = "My observatory"
# Degrees; north and east are positive.  They give the Sun gate and the Moon
# allowance.  Without them the monitor relies on the stars alone.
# latitude = 26.70
# longitude = 100.03

[decision]
# interval_seconds = 60        # one look at the sky per interval
# idle_interval_seconds = 300  # between looks while there is nothing to watch (daylight, roof marked open)
# open_after_minutes = 10      # the sky must stay good this long before the roof may open
# cloud_tolerance = "standard" # "lenient" (stars in 55 % of the sky on average), "standard" (70 %), "strict" (85 %);
#                              # a few clouds never block the roof, a cloudy look starts the wait again
# close_after_cycles = 2       # consecutive cloudy looks that close it again
# partly_close_minutes = 30    # close when the sky has not been clear for this long (0 = never)
# unknown_grace_seconds = 120  # an open roof tolerates this long without any sign of stars
# sun_max_altitude_deg = -8    # the Sun must be this low for the roof ...
# imaging_sun_altitude_deg = -12   # ... and this low for imaging
# imaging_moon_max_illumination = 1.0  # < 1: "imaging may run" waits while a Moon lit beyond this is up (the roof is not affected)
# min_cameras = 1              # cameras that must deliver a verdict

[analysis]
# Defaults for every camera; a camera can override them in [camera.analysis].
# clear_coverage = 0.70        # share of the sky cells with stars that is "clear" (set by cloud_tolerance)
# cloudy_coverage = 0.40       # below this share the sky is "cloudy" (set by cloud_tolerance)
# min_stars = 50               # stars a clear sky must show while no reference is known
# min_star_ratio = 0.5         # with a reference: the share of its star count that is "clear"
# reference_star_count = 0     # stars on a clear moonless night (or run: sky-monitor calibrate)
# threshold_sigma = 4.5        # detection threshold over the local noise
# drift_px_per_minute = 8      # how fast stars cross the frame at most

# One [[camera]] per camera.  type = "stream" reads anything ffmpeg can open.
[[camera]]
name = "sky"
type = "stream"
url = "rtsp://USER:${SKY_CAMERA_PASSWORD}@192.168.1.64:554/Streaming/Channels/101"
# frames = 16                  # frames taken per look ...
# seconds = 8                  # ... spread over this many seconds
# max_width = 1920             # larger frames are scaled down
# rectangle = [0.0, 0.0, 1.0, 1.0]     # the part of the frame that shows sky (fractions)
# circle = [0.5, 0.5, 0.48]            # or a circle: centre x, centre y, radius (all-sky lens)
# exclude = [[0.0, 0.0, 0.4, 0.06]]    # boxes to ignore: timestamps, logos, roof edges
# mask = "sky-mask.png"                # or an image, white where the frame shows sky

# An all-sky camera that publishes its latest picture:
# [[camera]]
# name = "allsky"
# type = "snapshot"
# url = "http://192.168.1.70/current/tmp/image.jpg"
# stale_after_seconds = 300
# [camera.analysis]
# drift_px_per_minute = 4

# A picture another program keeps up to date (or a folder: its newest picture):
# [[camera]]
# name = "folder"
# type = "file"
# path = "C:/allsky/latest.jpg"

# A camera that only shows in a vendor's app or web page (Windows): capture that
# part of the screen.
# [[camera]]
# name = "screen"
# type = "stream"
# url = "desktop"
# input_options = ["-f", "gdigrab", "-framerate", "5", "-offset_x", "100", "-offset_y", "200", "-video_size", "1280x720"]

[output]
# directory = "sky-monitor-data"   # status.json, previews, history; relative to this file
# preview = true
# history_days = 60
# archive_minutes = 0           # > 0: keep the stacked frame of every camera this often, and
#                               # whenever its verdict changes (16-bit PNG, for tuning thresholds)
# archive_days = 7

[web]
# share = "local"              # "lan": other computers on the same network may open the status page,
#                              # read-only; the safety devices below stay for this computer
# control_code = ""            # lets those computers use the buttons; easiest set on this computer's page

[alpaca]
# ASCOM Alpaca SafetyMonitor devices: 0 = the roof may open, 1 = imaging may run.
# enabled = true
# host = "127.0.0.1"           # "0.0.0.0" to serve other computers on the network
# port = 11112
# discovery = true

[email]
# Mail when the roof may open (with the camera pictures attached), and again
# when that is no longer so.  QQ/163/corporate mail: SSL on port 465 with the
# SMTP authorisation code as the password.
# enabled = true
# host = "smtp.qq.com"
# port = 465
# security = "ssl"             # "ssl", "starttls" (port 587) or "none"
# username = "you@qq.com"
# password = "${SKY_MONITOR_MAIL_PASSWORD}"
# from = "you@qq.com"
# to = ["you@example.com"]
# on_clear = true
# on_clear_lost = true
# repeat_minutes = 30          # repeat the "may open" mail every so often until "read" or "roof opened" is pressed (0: once)

[operator]
# pause_when_opened = true     # after "roof opened" is marked, stop watching for the rest of the night;
#                              # false keeps the Alpaca devices reporting real conditions and only silences the mails

[forecast]
# Tonight's forecast from several sources, shown on the page and in status.json; every
# night each source is scored against what the cameras measured, and the consensus leans
# on the sources that have been right at this site.  Needs the site's coordinates.
# enabled = true
# providers = ["open-meteo", "7timer"]   # add "caiyun" (token) or "qweather" (key) below
# open_meteo_models = ["best_match", "ecmwf_ifs025", "gfs_global", "icon_global", "cma_grapes_global"]
# refresh_minutes = 60
# caiyun_token = "${CAIYUN_TOKEN}"
# qweather_key = "${QWEATHER_KEY}"
# rain_veto_hours = 0          # > 0: the roof does not open while rain is forecast within this many hours
# rain_veto_probability = 0.5
# coordinate_precision = 2     # decimals of the coordinates sent to the sources (2 is about a kilometre)

[notify]
# Run a program when the answer changes; {state}, {message} and {message_url}
# (URL-encoded) are replaced.
# command = ["curl", "-s", "https://example.com/push?text={message_url}"]
"""
