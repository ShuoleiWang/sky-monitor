from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from PIL import Image

from skymonitor.cli import main
from skymonitor.config import ConfigError, load_config
from skymonitor.synthetic import SkyScene

SETTINGS = """
language = "en"

[[camera]]
name = "sky"
type = "file"
path = "sky.png"
"""


def _write_sky(path, scene: SkyScene = SkyScene(), **frame) -> None:
    Image.fromarray((scene.frame(frames=8, **frame) * 255).round().astype(np.uint8)).save(path)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def test_init_writes_settings_that_load_and_never_overwrites(tmp_path, monkeypatch, capsys) -> None:
    path = tmp_path / "sky-monitor.toml"

    assert main(["init", str(path)]) == 0
    assert main(["init", str(path)]) == 2
    assert "not overwritten" in capsys.readouterr().err

    monkeypatch.delenv("SKY_CAMERA_PASSWORD", raising=False)
    with pytest.raises(ConfigError):
        load_config(path)
    monkeypatch.setenv("SKY_CAMERA_PASSWORD", "hunter2")
    config = load_config(path)
    assert config.cameras[0].address == "rtsp://USER:hunter2@192.168.1.64:554/Streaming/Channels/101"
    assert (config.alpaca.host, config.alpaca.port) == ("127.0.0.1", 11112)
    assert config.site.latitude is None


def test_a_settings_error_is_reported_not_raised(tmp_path, capsys) -> None:
    path = tmp_path / "sky-monitor.toml"
    path.write_text(SETTINGS + "\n[decision]\nopen_after_minute = 3\n", encoding="utf-8")

    assert main(["status", "--config", str(path)]) == 2
    assert "settings: decision: unknown setting(s): open_after_minute" in capsys.readouterr().err
    assert main(["status", "--config", str(tmp_path / "absent.toml")]) == 2


def test_analyze_reads_a_picture_and_marks_it(tmp_path, capsys) -> None:
    picture = tmp_path / "sky.png"
    marked = tmp_path / "marked.jpg"
    _write_sky(picture)

    assert main(["analyze", str(picture), "--out", str(marked)]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["input"] == "sky.png"
    assert (result["width"], result["height"], result["looks"]) == (640, 360, 1)
    assert result["starCount"] > 250
    assert result["coverage"] > 0.9
    # One look cannot tell a star from a hot pixel: no verdict.
    assert (result["verdict"], result["reason"], result["provisional"]) == ("UNKNOWN", "CONFIRMING", True)
    assert marked.stat().st_size > 10_000
    assert main(["analyze", str(picture), "--out", str(marked)]) == 2
    assert main(["analyze", str(tmp_path / "absent.png")]) == 2


def test_doctor_checks_every_camera(tmp_path, capsys) -> None:
    path = tmp_path / "sky-monitor.toml"
    path.write_text(SETTINGS + "\n[alpaca]\nenabled = false\n", encoding="utf-8")

    assert main(["doctor", "--config", str(path)]) == 1
    assert "camera sky: FAILED, UNREADABLE" in capsys.readouterr().out

    _write_sky(tmp_path / "sky.png")
    assert main(["doctor", "--config", str(path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["cameras"][0]["ok"] is True
    assert (report["cameras"][0]["width"], report["cameras"][0]["height"]) == (640, 360)
    assert report["cameras"][0]["compactSources"] > 250
    assert report["site"] is None


def test_status_answers_with_its_exit_code(tmp_path, capsys) -> None:
    path = tmp_path / "sky-monitor.toml"
    path.write_text(SETTINGS, encoding="utf-8")
    data = tmp_path / "sky-monitor-data"
    data.mkdir()

    def publish(**fields) -> None:
        status = {"validUntil": _iso(_now() + timedelta(minutes=3)), "message": "the answer", **fields}
        (data / "status.json").write_text(json.dumps(status), encoding="utf-8")

    assert main(["status", "--config", str(path)]) == 2
    publish(safe=True, imagingOk=False)
    assert main(["status", "--config", str(path)]) == 0
    assert capsys.readouterr().out.strip() == "the answer"
    assert main(["status", "--config", str(path), "--imaging"]) == 1
    publish(safe=False, imagingOk=False)
    assert main(["status", "--config", str(path)]) == 1
    # A monitor that stopped publishing: its last "safe" no longer counts.
    publish(safe=True, imagingOk=True, validUntil=_iso(_now() - timedelta(seconds=5)))
    assert main(["status", "--config", str(path)]) == 2
    (data / "status.json").write_text("{ half a file", encoding="utf-8")
    assert main(["status", "--config", str(path)]) == 2


def _history(tmp_path, counts: list[int], *, age_minutes: float = 0.0, moon: dict | None = None) -> None:
    history = tmp_path / "sky-monitor-data" / "history"
    history.mkdir(parents=True, exist_ok=True)
    lines = []
    for index, count in enumerate(counts):
        moment = _now() - timedelta(minutes=age_minutes + len(counts) - index)
        entry = {
            "updatedAt": _iso(moment),
            "moon": moon,
            "cameras": [{"name": "sky", "verdict": "CLEAR", "starCount": count}],
        }
        lines.append(json.dumps(entry))
    (history / f"{_now().astimezone().date().isoformat()}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_calibrate_keeps_the_star_count_of_a_steady_sky(tmp_path, capsys) -> None:
    path = tmp_path / "sky-monitor.toml"
    path.write_text(SETTINGS, encoding="utf-8")
    reference = tmp_path / "sky-monitor-data" / "reference.json"

    _history(tmp_path, [400, 410, 395, 405, 402, 398, 407])
    assert main(["calibrate", "--config", str(path)]) == 0
    assert "a clear sky shows 402 stars; from now on 201 or more count as clear" in capsys.readouterr().out
    assert json.loads(reference.read_text(encoding="utf-8"))["cameras"]["sky"]["starCount"] == 402


def test_calibrate_refuses_an_unsteady_or_short_record(tmp_path, capsys) -> None:
    path = tmp_path / "sky-monitor.toml"
    path.write_text(SETTINGS, encoding="utf-8")
    reference = tmp_path / "sky-monitor-data" / "reference.json"

    _history(tmp_path, [400, 120, 380, 60, 410, 90, 300])
    assert main(["calibrate", "--config", str(path)]) == 1
    assert "a clear sky is steadier" in capsys.readouterr().out
    assert not reference.exists()

    _history(tmp_path, [400, 410, 395])
    assert main(["calibrate", "--config", str(path)]) == 1
    assert "only 3 look(s)" in capsys.readouterr().out

    _history(tmp_path, [400] * 8, age_minutes=120)
    assert main(["calibrate", "--config", str(path)]) == 1

    _history(tmp_path, [400] * 8, moon={"altitudeDeg": 40.0, "illumination": 0.9})
    assert main(["calibrate", "--config", str(path)]) == 0
    assert "the Moon was up" in capsys.readouterr().out


FORECAST_SETTINGS = (
    SETTINGS
    + """
[site]
name = "Lijiang test"
latitude = 26.70
longitude = 100.03
"""
)


def _latest(tmp_path) -> None:
    """A forecast as the monitor keeps it: two sources over the next day and a half."""

    from skymonitor.forecast import Forecast, HourForecast, hour_of

    start = hour_of(_now()) - timedelta(hours=2)
    hours = [start + timedelta(hours=index) for index in range(36)]
    meteo = Forecast(
        "open-meteo",
        "best_match",
        None,
        _now(),
        tuple(
            HourForecast(
                t,
                cloud=0.2,
                cloud_low=0.1,
                cloud_mid=0.05,
                cloud_high=0.15,
                precipitation_probability=0.1,
                precipitation_mm=0.0,
                wind_ms=3.0,
                humidity=0.6,
            )
            for t in hours
        ),
    )
    timer = Forecast(
        "7timer",
        "astro",
        _now() - timedelta(hours=5),
        _now(),
        tuple(HourForecast(t, cloud=0.4, seeing=2.0, transparency=3.0, humidity=0.7) for t in hours),
    )
    folder = tmp_path / "sky-monitor-data" / "forecast"
    folder.mkdir(parents=True)
    payload = {"refreshedAt": _iso(_now()), "forecasts": [meteo.to_json(), timer.to_json()]}
    (folder / "latest.json").write_text(json.dumps(payload), encoding="utf-8")


def test_forecast_prints_tonight_and_each_source(tmp_path, capsys) -> None:
    path = tmp_path / "sky-monitor.toml"
    path.write_text(SETTINGS, encoding="utf-8")
    assert main(["forecast", "--config", str(path)]) == 2
    assert "the forecast is off" in capsys.readouterr().err

    path.write_text(FORECAST_SETTINGS, encoding="utf-8")
    assert main(["forecast", "--config", str(path)]) == 2
    assert "no forecast yet" in capsys.readouterr().err

    _latest(tmp_path)
    assert main(["forecast", "--config", str(path)]) == 0
    text = capsys.readouterr().out
    assert "Tonight's forecast for Lijiang test: 2 source(s)" in text
    # Equal weights: the median of two values is the higher one (40 % cloud, 70 % humidity).
    assert "    40%     10%      5%     15%     10%     0.0     3.0       -     70%       2       3" in text
    assert "open-meteo/best_match" in text and "7timer/astro" in text

    assert main(["forecast", "--config", str(path), "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert len(summary["night"]) >= 6
    assert {hour["cloud"] for hour in summary["night"]} == {0.4}
    assert summary["night"][0]["sources"] == {"open-meteo/best_match": 0.2, "7timer/astro": 0.4}
    assert [p["key"] for p in summary["providers"]] == ["7timer/astro", "open-meteo/best_match"]
    assert all(p["weight"] == 1.0 and p["error"] is None for p in summary["providers"])


def test_forecast_refresh_asks_every_source_and_says_how_it_went(tmp_path, capsys, monkeypatch) -> None:
    from skymonitor import cli
    from skymonitor.forecast import Forecast, HourForecast, ProviderError, hour_of

    class Source:
        def __init__(self, name: str, cloud: float | None) -> None:
            self.name, self.cloud = name, cloud

        def fetch(self, latitude: float, longitude: float) -> list[Forecast]:
            assert (latitude, longitude) == (26.7, 100.03)
            if self.cloud is None:
                raise ProviderError("no answer within 20 s")
            start = hour_of(_now())
            hours = tuple(HourForecast(start + timedelta(hours=index), cloud=self.cloud) for index in range(30))
            return [Forecast(self.name, "m", None, _now(), hours)]

    monkeypatch.setattr(cli, "build_providers", lambda settings: [Source("quick", 0.3), Source("slow", None)])
    path = tmp_path / "sky-monitor.toml"
    path.write_text(FORECAST_SETTINGS, encoding="utf-8")

    assert main(["forecast", "--config", str(path), "--refresh"]) == 0
    text = capsys.readouterr().out
    assert "OK, 1 forecast(s)" in text and "FAILED: no answer within 20 s" in text
    assert (tmp_path / "sky-monitor-data" / "forecast" / "latest.json").is_file()

    assert main(["forecast", "--config", str(path), "--refresh", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert [(entry["provider"], entry["ok"]) for entry in summary["asked"]] == [("quick", True), ("slow", False)]
    assert next(p for p in summary["providers"] if p["key"] == "slow")["error"] == "no answer within 20 s"

    monkeypatch.setattr(cli, "build_providers", lambda settings: [Source("slow", None)])
    (tmp_path / "sky-monitor-data" / "forecast" / "latest.json").unlink()
    assert main(["forecast", "--config", str(path), "--refresh"]) == 1
