"""The forecast sources against recorded answers (Open-Meteo, 7Timer: recorded once for a public
location near Lijiang, 26.70 N 100.03 E, trimmed to a few hours) and answers written from the
documented shapes (Caiyun, QWeather)."""

from __future__ import annotations

import email.message
import gzip
import io
import json
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from skymonitor.config import ForecastConfig
from skymonitor.forecast import ForecastService, ProviderError
from skymonitor.providers import build_providers
from skymonitor.providers.caiyun import Caiyun
from skymonitor.providers.openmeteo import OpenMeteo
from skymonitor.providers.qweather import QWeather
from skymonitor.providers.seventimer import SevenTimer

FIXTURES = Path(__file__).parent / "fixtures" / "forecast"
MODELS = ("best_match", "ecmwf_ifs025", "gfs_global", "icon_global", "cma_grapes_global")
TOKEN = "T0KEN-c41yun-secret"
KEY = "K3Y-qw-secret"
UTC = UTC


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class Answer:
    """What ``urlopen`` returns: a body, read in pieces, and its headers."""

    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = io.BytesIO(body)
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def __enter__(self) -> Answer:
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _http_error(code: int, body: bytes = b"", url: str = "https://example.invalid/") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "error", email.message.Message(), io.BytesIO(body))


class Web:
    """Stands in for the network: answers every request with ``answer`` (or raises it) and keeps the requests."""

    def __init__(self) -> None:
        self.requests: list[tuple[urllib.request.Request, float | None]] = []
        self.answer: object = None

    def __call__(self, request: urllib.request.Request, timeout: float | None = None) -> Answer:
        self.requests.append((request, timeout))
        answer = self.answer(request) if callable(self.answer) else self.answer
        if isinstance(answer, BaseException):
            raise answer
        return answer  # type: ignore[return-value]

    @property
    def url(self) -> str:
        return self.requests[-1][0].full_url

    @property
    def query(self) -> dict[str, str]:
        return {key: values[0] for key, values in parse_qs(urlsplit(self.url).query).items()}


@pytest.fixture
def web(monkeypatch) -> Web:
    fake = Web()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


def test_open_meteo_reads_every_model_from_one_answer(web) -> None:
    web.answer = Answer(_fixture("open-meteo-models.json"))

    forecasts = OpenMeteo(models=MODELS).fetch(26.7, 100.03)

    request, timeout = web.requests[-1]
    assert web.url.startswith("https://api.open-meteo.com/v1/forecast?") and timeout == 20.0
    assert request.get_header("User-agent") == "sky-monitor"
    assert web.query["latitude"] == "26.7" and web.query["longitude"] == "100.03"
    assert web.query["models"] == ",".join(MODELS)
    assert (web.query["timezone"], web.query["forecast_days"], web.query["wind_speed_unit"]) == ("UTC", "3", "ms")
    assert web.query["hourly"].split(",") == [
        "cloud_cover",
        "cloud_cover_low",
        "cloud_cover_mid",
        "cloud_cover_high",
        "precipitation",
        "precipitation_probability",
        "wind_speed_10m",
        "wind_gusts_10m",
        "relative_humidity_2m",
        "dew_point_2m",
        "temperature_2m",
    ]
    assert [f.key for f in forecasts] == [f"open-meteo/{model}" for model in MODELS]
    assert all(f.issued_at is None and f.fetched_at.tzinfo is not None for f in forecasts)
    assert all([h.time for h in f.hours] == [_at(6, h) for h in range(6)] for f in forecasts)

    best = forecasts[0].hours[0]
    assert (best.cloud, best.cloud_low, best.cloud_mid, best.cloud_high) == pytest.approx((1.0, 1.0, 0.3, 0.01))
    assert (best.wind_ms, best.humidity, best.dew_point_c, best.temperature_c) == pytest.approx((0.47, 0.98, 6.8, 7.1))
    # Rain, its probability and gusts are given for the preceding hour: 01:00's values describe 00:00-01:00.
    assert (best.precipitation_mm, best.precipitation_probability, best.gust_ms) == pytest.approx((0.2, 0.35, 3.3))
    assert best.seeing is None and best.transparency is None
    last = forecasts[0].hours[-1]
    assert last.cloud == 1.0 and last.precipitation_mm is None and last.gust_ms is None
    gfs = forecasts[2].hours[0]
    assert (gfs.cloud, gfs.precipitation_mm, gfs.precipitation_probability) == pytest.approx((0.92, 0.6, 0.95))
    # CMA GRAPES has no rain probability: null in every hour.
    cma = forecasts[4].hours
    assert cma[0].cloud == 0.0 and all(h.precipitation_probability is None for h in cma)


def test_open_meteo_with_one_model_reads_the_names_without_a_suffix(web) -> None:
    web.answer = Answer(_fixture("open-meteo-best-match.json"))
    single = OpenMeteo(models=("best_match",)).fetch(26.7, 100.03)
    web.answer = Answer(_fixture("open-meteo-models.json"))
    several = OpenMeteo(models=MODELS).fetch(26.7, 100.03)

    assert [f.key for f in single] == ["open-meteo/best_match"]
    assert single[0].hours == several[0].hours


def test_open_meteo_says_which_model_it_does_not_know(web) -> None:
    web.answer = _http_error(400, _fixture("open-meteo-unknown-model.json"))

    with pytest.raises(ProviderError) as caught:
        OpenMeteo(models=("best_match", "no_such_model")).fetch(26.7, 100.03)

    assert str(caught.value).startswith("HTTP 400: Invalid value")
    assert "no_such_model" in str(caught.value)


def test_7timer_spreads_each_three_hour_point_over_its_hours(web) -> None:
    web.answer = Answer(_fixture("7timer-astro.json"))

    (forecast,) = SevenTimer().fetch(26.7, 100.03)

    assert web.url.startswith("https://www.7timer.info/bin/astro.php?")
    assert web.query == {"lon": "100.03", "lat": "26.7", "ac": "0", "unit": "metric", "output": "json", "tzshift": "0"}
    assert forecast.key == "7timer/astro" and forecast.issued_at == _at(6, 12)
    assert [h.time for h in forecast.hours] == [_at(6, 15) + timedelta(hours=i) for i in range(9)]
    first = forecast.hours[0]
    assert (first.cloud, first.seeing, first.transparency) == (0.75, 3.0, 8.0)
    assert (first.humidity, first.wind_ms, first.temperature_c) == pytest.approx((0.975, 1.9, 7.0))
    assert first.precipitation_probability == 0.0 and first.precipitation_mm is None
    assert forecast.hours[2].cloud == 0.75 and forecast.hours[3].cloud == 0.875
    rainy = forecast.hours[6]
    assert (rainy.time, rainy.cloud, rainy.seeing, rainy.precipitation_probability) == (_at(6, 21), 0.97, 2.0, 0.7)


def test_7timer_classes_outside_their_range_read_as_missing() -> None:
    payload = json.loads(_fixture("7timer-astro.json"))
    point = payload["dataseries"][0]
    point.update(cloudcover=-9999, seeing=-9999, transparency=9, rh2m=16, temp2m=-9999, prec_type="snow")
    point["wind10m"]["speed"] = 0

    hour = SevenTimer().parse(payload, _at(6, 17)).hours[0]

    assert (hour.cloud, hour.seeing, hour.transparency, hour.wind_ms, hour.temperature_c) == (None,) * 5
    assert hour.humidity == 1.0 and hour.precipitation_probability == 0.7


def test_caiyun_reads_the_hours_and_folds_in_the_radar_rain(web) -> None:
    web.answer = Answer(_fixture("caiyun-weather.json"))

    (forecast,) = Caiyun(TOKEN).fetch(26.7, 100.03)

    assert (
        web.url == f"https://api.caiyunapp.com/v2.6/{TOKEN}/100.03,26.7/weather?alert=false&dailysteps=1&hourlysteps=48"
    )
    assert forecast.key == "caiyun/v2.6" and forecast.issued_at == _at(6, 17, 20)
    hours = {h.time: h for h in forecast.hours}
    assert list(hours) == [_at(6, 17), _at(6, 18), _at(6, 19), _at(6, 20)]
    now = hours[_at(6, 17)]
    assert (now.cloud, now.humidity, now.temperature_c) == pytest.approx((0.42, 0.86, 9.0))
    assert now.wind_ms == pytest.approx(2.0)  # 7.2 km/h
    # The radar's next two hours start at the server's time, 17:20: minutes 40-99 fall in 18:00,
    # 100-119 in 19:00; each half hour's probability raises the hours it touches.
    assert (now.precipitation_mm, now.precipitation_probability) == pytest.approx((0.0, 0.3))
    assert (hours[_at(6, 18)].precipitation_mm, hours[_at(6, 18)].precipitation_probability) == pytest.approx(
        (0.6, 0.7)
    )
    assert (hours[_at(6, 19)].precipitation_mm, hours[_at(6, 19)].precipitation_probability) == pytest.approx(
        (1.5, 0.7)
    )
    assert (hours[_at(6, 20)].precipitation_mm, hours[_at(6, 20)].precipitation_probability) == pytest.approx(
        (1.2, 0.85)
    )
    assert hours[_at(6, 20)].wind_ms == pytest.approx(5.0) and hours[_at(6, 20)].cloud == 1.0


@pytest.mark.parametrize(
    "failure, expected",
    [
        (
            lambda: _http_error(401, json.dumps({"status": "failed", "error": f"token {TOKEN} is invalid"}).encode()),
            "HTTP 401",
        ),
        (
            lambda: urllib.error.URLError(f"cannot open https://api.caiyunapp.com/v2.6/{TOKEN}/x"),
            "cannot reach the server",
        ),
        (lambda: urllib.error.URLError(TimeoutError("timed out")), "no answer within 20 s"),
        (lambda: TimeoutError("timed out"), "no answer within 20 s"),
        (lambda: Answer(b"<html>busy</html>"), "not JSON"),
        (
            lambda: Answer(json.dumps({"status": "failed", "error": f"'{TOKEN}' is not a valid token"}).encode()),
            "status 'failed'",
        ),
        (lambda: Answer(json.dumps({"status": "ok", "result": {}}).encode()), "unexpected answer (KeyError"),
    ],
    ids=["http-401", "unreachable", "timeout-in-urlerror", "timeout", "not-json", "status-failed", "unexpected"],
)
def test_caiyun_never_shows_its_token(web, failure, expected) -> None:
    web.answer = failure()

    with pytest.raises(ProviderError) as caught:
        Caiyun(TOKEN).fetch(26.7, 100.03)

    assert expected in str(caught.value)
    assert TOKEN not in str(caught.value)
    # Nothing that carried the address travels along for a traceback to print.
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_qweather_reads_the_hours_with_the_key_in_a_header(web) -> None:
    web.answer = Answer(gzip.compress(_fixture("qweather-24h.json")), {"Content-Encoding": "gzip"})

    (forecast,) = QWeather(KEY, host="abc123.re.qweatherapi.com").fetch(26.7, 100.03)

    request, _ = web.requests[-1]
    assert web.url == "https://abc123.re.qweatherapi.com/v7/weather/24h?location=100.03,26.7"
    assert request.get_header("X-qw-api-key") == KEY and KEY not in web.url
    assert forecast.key == "qweather/24h" and forecast.issued_at == _at(6, 17, 15)
    assert [h.time for h in forecast.hours] == [_at(6, 18), _at(6, 19), _at(6, 20), _at(6, 21)]
    first = forecast.hours[0]
    assert (first.cloud, first.precipitation_probability, first.precipitation_mm) == pytest.approx((0.45, 0.2, 0.0))
    assert (first.wind_ms, first.humidity, first.dew_point_c, first.temperature_c) == pytest.approx(
        (2.5, 0.88, 7.0, 9.0)
    )
    third = forecast.hours[2]
    assert (third.cloud, third.precipitation_probability, third.dew_point_c) == (None, None, None)
    assert third.precipitation_mm == pytest.approx(1.2) and third.wind_ms == pytest.approx(5.0)


@pytest.mark.parametrize(
    "failure, expected",
    [
        (lambda: Answer(json.dumps({"code": "401"}).encode()), "QWeather answered code 401"),
        (
            lambda: _http_error(
                403, json.dumps({"error": {"status": 403, "title": "Forbidden", "detail": "Invalid host"}}).encode()
            ),
            "HTTP 403: Forbidden: Invalid host",
        ),
        (lambda: TimeoutError("timed out"), "no answer within 20 s"),
        (
            lambda: Answer(json.dumps({"code": "200", "hourly": [{"temp": "9"}]}).encode()),
            "unexpected answer (KeyError",
        ),
    ],
    ids=["code-401", "http-403", "timeout", "unexpected"],
)
def test_qweather_errors_carry_the_code_not_the_key(web, failure, expected) -> None:
    web.answer = failure()

    with pytest.raises(ProviderError) as caught:
        QWeather(KEY).fetch(26.7, 100.03)

    assert expected in str(caught.value) and KEY not in str(caught.value)
    assert web.url.startswith("https://devapi.qweather.com/v7/weather/24h?")


@pytest.mark.parametrize("source", [OpenMeteo(models=MODELS), SevenTimer()], ids=["open-meteo", "7timer"])
@pytest.mark.parametrize(
    "failure, expected",
    [
        (lambda: _http_error(500), "HTTP 500"),
        (lambda: _http_error(503, b"<html>maintenance</html>"), "HTTP 503"),
        (lambda: urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")), "cannot reach the server"),
        (lambda: ConnectionResetError(54, "Connection reset by peer"), "ConnectionResetError"),
        (lambda: TimeoutError("timed out"), "no answer within 20 s"),
        (lambda: Answer(b"Service busy, try later"), "not JSON"),
        (lambda: Answer(b'{"hourly": {"time": "nonsense"}, "dataseries": 7, "init": "x"}'), "unexpected answer"),
    ],
    ids=["http-500", "http-503", "refused", "reset", "timeout", "not-json", "unexpected"],
)
def test_the_open_sources_turn_every_failure_into_a_provider_error(web, source, failure, expected) -> None:
    web.answer = failure()

    with pytest.raises(ProviderError) as caught:
        source.fetch(26.7, 100.03)

    assert expected in str(caught.value)


def test_the_service_keeps_and_reloads_what_the_adapters_read(web, tmp_path) -> None:
    def answer(request: urllib.request.Request) -> Answer:
        name = "7timer-astro.json" if "7timer" in request.full_url else "open-meteo-models.json"
        return Answer(_fixture(name))

    web.answer = answer
    clock = lambda: _at(6, 14)  # noqa: E731
    service = ForecastService([OpenMeteo(models=MODELS), SevenTimer()], 26.70, 100.03, tmp_path, clock=clock)
    service.refresh()
    night = [_at(6, 15) + timedelta(hours=i) for i in range(3)]
    summary = service.summary(night)
    reloaded = ForecastService([], 26.70, 100.03, tmp_path, clock=clock).summary(night)

    assert reloaded == summary
    assert [p["key"] for p in summary["providers"]] == ["7timer/astro"] + [f"open-meteo/{m}" for m in MODELS]
    assert all(p["error"] is None for p in summary["providers"])
    first = summary["night"][0]
    assert (first["cloud"], first["seeing"], first["transparency"], first["humidity"]) == (0.75, 3.0, 8.0, 0.975)
    assert first["sources"]["7timer/astro"] == 0.75 and first["sources"]["open-meteo/best_match"] is None


def test_build_providers_leaves_out_the_keyed_sources_without_keys() -> None:
    every = ("open-meteo", "7timer", "caiyun", "qweather")

    plain = build_providers(ForecastConfig(providers=every))
    keyed = build_providers(
        ForecastConfig(providers=every, caiyun_token=TOKEN, qweather_key=KEY, qweather_host="abc123.re.qweatherapi.com")
    )

    assert [p.name for p in plain] == ["open-meteo", "7timer"]
    assert plain[0].models == ForecastConfig().open_meteo_models
    assert [p.name for p in keyed] == list(every)
    assert keyed[3].host == "abc123.re.qweatherapi.com"
    assert [p.name for p in build_providers(ForecastConfig(providers=("7timer",)))] == ["7timer"]
