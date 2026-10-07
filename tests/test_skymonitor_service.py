from __future__ import annotations

import json
import sys
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from skymonitor import service
from skymonitor.config import ConfigError, parse_config
from skymonitor.service import SCHEMA, Monitor, SafetyHolder
from skymonitor.sources import decode_image
from skymonitor.stars import detect_stars
from skymonitor.synthetic import SkyScene, cloud_bank
from skymonitor_helpers import Clock, SkySource


def _settings(tmp_path, **changes) -> dict:
    settings = {
        "language": "en",
        "site": {"name": "Test site"},
        # A short night for the tests: fixed sources known after 5 minutes, open after 2 more.
        "decision": {"open_after_minutes": 2.0},
        "analysis": {"fixed_window_minutes": 10.0},
        "camera": [{"name": "sky", "type": "file", "path": "unused.png"}],
        "output": {"directory": "data"},
    }
    settings.update(changes)
    return settings


def _monitor(tmp_path, clock: Clock, sources: dict, **changes) -> tuple[Monitor, SafetyHolder]:
    config = parse_config(_settings(tmp_path, **changes), tmp_path / "sky-monitor.toml")
    holder = SafetyHolder()
    return Monitor(config, sources=sources, clock=clock, holder=holder), holder


def _minutes(monitor: Monitor, clock: Clock, count: int) -> list[dict]:
    statuses = []
    for _ in range(count):
        statuses.append(monitor.cycle())
        clock.advance(60)
    return statuses


def test_a_night_from_start_to_cloud(tmp_path) -> None:
    clock = Clock()
    camera = SkySource(SkyScene(fixed_sources=20), clock)
    note = tmp_path / "note.txt"
    write = (
        "import sys, pathlib; pathlib.Path(sys.argv[1]).write_text(sys.argv[2] + '|' + sys.argv[3], encoding='utf-8')"
    )
    monitor, holder = _monitor(
        tmp_path,
        clock,
        {"sky": camera},
        notify={"command": [sys.executable, "-c", write, str(note), "{state}", "{message_url}"]},
    )

    clear = _minutes(monitor, clock, 8)

    reasons = [status["cameras"][0]["reason"] for status in clear]
    assert reasons == ["CONFIRMING"] + ["WARMING_UP"] * 4 + ["CLEAR"] * 3
    assert [status["reason"] for status in clear] == ["NO_DATA"] * 5 + ["WAITING"] * 2 + ["CLEAR"]
    assert [status["safe"] for status in clear] == [False] * 7 + [True]
    assert clear[-1]["imagingOk"] is True
    assert holder.roof_may_open() and holder.imaging_may_run()
    assert "the roof may open and imaging may run" in clear[-1]["message"]
    assert clear[2]["message"] == (
        "Getting ready; the roof stays closed meanwhile. sky: learning the fixed bright spots (about 5 min)"
    )

    data = tmp_path / "data"
    published = json.loads((data / "status.json").read_text(encoding="utf-8"))
    assert published == clear[-1]
    assert published["schema"] == SCHEMA
    assert published["site"] == "Test site"
    assert published["sun"] is None
    assert published["cameras"][0]["name"] == "sky"
    assert published["cameras"][0]["coverage"] > 0.9
    assert datetime.fromisoformat(published["validUntil"]) - datetime.fromisoformat(
        published["updatedAt"]
    ) == timedelta(seconds=180)
    assert (data / "preview" / "sky.jpg").stat().st_size > 10_000
    history = [path for path in (data / "history").iterdir()]
    assert len(history) == 1
    assert len(history[0].read_text(encoding="utf-8").splitlines()) == 8

    deadline = time.monotonic() + 20
    while not note.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    state, _, message = note.read_text(encoding="utf-8").partition("|")
    assert state == "IMAGING"
    assert message.startswith("Clear%20sky")

    camera.cloud = cloud_bank(camera.scene.shape, 1.0)
    cloudy = _minutes(monitor, clock, 2)

    assert [(status["safe"], status["reason"]) for status in cloudy] == [(True, "CLOUDY_HOLDING"), (False, "CLOUDY")]
    assert not holder.roof_may_open()
    assert "keep the roof closed" in cloudy[-1]["message"]

    monitor.close()
    stopped = json.loads((data / "status.json").read_text(encoding="utf-8"))
    assert (stopped["safe"], stopped["reason"]) == (False, "STOPPED")
    assert (data / "state" / "sky.fixed.npz").is_file()


def test_frames_are_kept_at_intervals_and_when_the_verdict_changes(tmp_path) -> None:
    clock = Clock()
    camera = SkySource(SkyScene(), clock)
    monitor, _ = _monitor(tmp_path, clock, {"sky": camera}, output={"directory": "data", "archive_minutes": 4.0})

    _minutes(monitor, clock, 7)
    kept = sorted(path.name for path in (tmp_path / "data" / "archive" / "sky").iterdir())

    # Minutes 0 and 4 by the clock, minute 5 because the camera turned from UNKNOWN to CLEAR.
    assert [name.rsplit("-", 1)[1] for name in kept] == ["UNKNOWN.png", "UNKNOWN.png", "CLEAR.png"]
    frame = decode_image((tmp_path / "data" / "archive" / "sky" / kept[-1]).read_bytes())
    assert frame.shape == camera.scene.shape
    assert len(detect_stars(frame).xy) > 250
    monitor.close()


def test_a_camera_that_fails_reads_as_no_data_and_closes_the_roof(tmp_path) -> None:
    clock = Clock()
    camera = SkySource(SkyScene(), clock)
    monitor, holder = _monitor(tmp_path, clock, {"sky": camera})
    assert _minutes(monitor, clock, 8)[-1]["safe"]

    camera.failure = "TIMEOUT"
    failing = _minutes(monitor, clock, 3)

    assert [status["cameras"][0]["reason"] for status in failing] == ["TIMEOUT"] * 3
    assert [(status["safe"], status["reason"]) for status in failing] == [
        (True, "NO_DATA_HOLDING"),
        (True, "NO_DATA_HOLDING"),
        (False, "NO_DATA"),
    ]
    assert "no data (TIMEOUT)" in failing[-1]["message"]
    assert not holder.roof_may_open()
    monitor.close()


def test_a_look_that_outlives_the_monitor_publishes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service, "STOP_WAIT_SECONDS", 0.2)
    clock = Clock()
    camera = SkySource(SkyScene(), clock)
    monitor, holder = _monitor(tmp_path, clock, {"sky": camera})
    assert _minutes(monitor, clock, 8)[-1]["safe"]

    release = threading.Event()
    entered = threading.Event()
    grab = camera.grab

    def slow_grab():
        entered.set()
        release.wait(30)
        return grab()

    camera.grab = slow_grab
    look = threading.Thread(target=monitor.cycle)
    look.start()
    assert entered.wait(10)
    monitor.close()
    stopped = json.loads((tmp_path / "data" / "status.json").read_text(encoding="utf-8"))
    release.set()
    look.join(timeout=30)

    assert not look.is_alive()
    assert (stopped["safe"], stopped["reason"]) == (False, "STOPPED")
    # The late look saw a clear sky; it must not turn the stopped monitor's answer back to safe.
    assert json.loads((tmp_path / "data" / "status.json").read_text(encoding="utf-8")) == stopped
    assert not holder.roof_may_open()


def test_a_fault_in_the_analysis_is_no_data(tmp_path) -> None:
    clock = Clock()

    class Faulty:
        def grab(self):
            raise ZeroDivisionError("a bug")

    monitor, _ = _monitor(tmp_path, clock, {"sky": Faulty()})

    status = monitor.cycle()

    assert status["cameras"][0]["reason"] == "ANALYSIS_ERROR"
    assert (status["safe"], status["verdict"]) == (False, "UNKNOWN")
    monitor.close()


def test_daylight_is_not_safe_and_the_cameras_are_left_alone(tmp_path) -> None:
    noon = Clock(datetime(2026, 10, 6, 5, 0, tzinfo=UTC))
    camera = SkySource(SkyScene(), noon)
    monitor, _ = _monitor(
        tmp_path, noon, {"sky": camera}, site={"name": "Test site", "latitude": 26.7, "longitude": 100.03}
    )

    status = monitor.cycle()

    assert (status["safe"], status["reason"]) == (False, "DAYLIGHT")
    assert status["sun"]["altitudeDeg"] > 40
    assert status["cameras"][0]["reason"] == "DAYLIGHT"
    assert "Not dark: the Sun is at +5" in status["message"]
    assert camera.looks == 0
    monitor.close()


def test_the_sun_and_moon_are_published_at_night(tmp_path) -> None:
    clock = Clock()
    monitor, _ = _monitor(
        tmp_path, clock, {"sky": SkySource(SkyScene(), clock)}, site={"latitude": 26.7, "longitude": 100.03}
    )

    status = monitor.cycle()

    assert status["sun"]["altitudeDeg"] < -30
    assert status["moon"]["altitudeDeg"] < 0
    assert 0.1 < status["moon"]["illumination"] < 0.25
    assert status["moon"]["risesAt"] is not None and status["moon"]["setsAt"] is not None
    monitor.close()


def test_a_calibrated_reference_is_picked_up_while_running(tmp_path) -> None:
    clock = Clock()
    monitor, _ = _monitor(tmp_path, clock, {"sky": SkySource(SkyScene(), clock)})
    before = _minutes(monitor, clock, 6)[-1]

    reference = {"cameras": {"sky": {"starCount": 2000}}}
    (tmp_path / "data" / "reference.json").write_text(json.dumps(reference), encoding="utf-8")
    after = monitor.cycle()

    assert (before["cameras"][0]["verdict"], before["cameras"][0]["requiredStars"]) == ("CLEAR", 50)
    # 2000 stars when clear: the 340 of this sky are a third of what it takes.
    assert after["cameras"][0]["referenceStarCount"] == 2000
    assert (after["cameras"][0]["verdict"], after["cameras"][0]["requiredStars"]) == ("CLOUDY", 1000)
    monitor.close()


def test_the_answer_expires_when_the_monitor_falls_silent() -> None:
    now = [100.0]
    holder = SafetyHolder(clock=lambda: now[0])

    holder.publish(True, True, 180.0)
    fresh = (holder.roof_may_open(), holder.imaging_may_run())
    now[0] += 181.0
    expired = (holder.roof_may_open(), holder.imaging_may_run())

    assert fresh == (True, True)
    assert expired == (False, False)


def test_chinese_messages(tmp_path) -> None:
    clock = Clock()
    monitor, _ = _monitor(tmp_path, clock, {"sky": SkySource(SkyScene(), clock)}, language="zh")

    statuses = _minutes(monitor, clock, 8)

    assert statuses[0]["message"] == "正在准备，期间不开顶。sky：正在确认星点"
    assert statuses[-1]["message"].startswith("天空晴朗：可以开顶，可以拍摄。sky：")
    assert "的天区有星" in statuses[-1]["message"]
    written = (tmp_path / "data" / "status.json").read_text(encoding="utf-8")
    assert "可以开顶" in written
    monitor.close()


def test_settings_are_read_strictly(tmp_path) -> None:
    path = tmp_path / "sky-monitor.toml"

    def refused(**changes) -> str:
        with pytest.raises(ConfigError) as error:
            parse_config(_settings(tmp_path, **changes), path)
        return str(error.value)

    assert "unknown setting(s): open_after_minute" in refused(decision={"open_after_minute": 3})
    assert "cloudy_coverage must be below clear_coverage" in refused(
        analysis={"clear_coverage": 0.5, "cloudy_coverage": 0.6}
    )
    assert "camera: at least one" in refused(camera=[])
    assert "camera[0].type: expected one of" in refused(camera=[{"name": "a", "type": "webcam", "url": "x"}])
    assert "camera[0].url: a snapshot address" in refused(camera=[{"name": "a", "type": "snapshot", "url": "ftp://x"}])
    assert "camera[0].name" in refused(camera=[{"name": "a b", "type": "file", "path": "x"}])
    assert "every camera needs its own name" in refused(camera=[{"name": "a", "type": "file", "path": "x"}] * 2)
    assert "site: give both" in refused(site={"latitude": 26.7})
    assert "threshold_sigma: 1.0 is outside" in refused(analysis={"threshold_sigma": 1.0})
    assert "environment variable SKY_TEST_UNSET_PASSWORD is not set" in refused(
        camera=[{"name": "a", "type": "stream", "url": "rtsp://u:${SKY_TEST_UNSET_PASSWORD}@host/x"}]
    )
    assert "imaging_sun_altitude_deg must not be above" in refused(decision={"imaging_sun_altitude_deg": -5.0})
    assert "interval_seconds: expected a number" in refused(decision={"interval_seconds": "60"})


def test_camera_settings_override_the_common_ones(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKY_TEST_PASSWORD", "hunter2")
    cameras = [
        {
            "name": "wide",
            "type": "stream",
            "url": "rtsp://viewer:${SKY_TEST_PASSWORD}@10.0.0.9/stream",
            "exclude": [[0.0, 0.0, 0.4, 0.06]],
            "analysis": {"min_stars": 30},
        },
        {"name": "allsky", "type": "snapshot", "url": "http://10.0.0.8/image.jpg", "circle": [0.5, 0.5, 0.48]},
    ]

    config = parse_config(_settings(tmp_path, camera=cameras, analysis={"min_stars": 80}), tmp_path / "s.toml")

    wide, allsky = config.cameras
    assert wide.address == "rtsp://viewer:hunter2@10.0.0.9/stream"
    assert (wide.analysis.min_stars, allsky.analysis.min_stars) == (30, 80)
    assert wide.region.exclude == ((0.0, 0.0, 0.4, 0.06),)
    assert allsky.region.circle == (0.5, 0.5, 0.48)
    assert config.output.directory == (tmp_path / "data").resolve()
    assert config.decision.open_after_seconds == 120.0
