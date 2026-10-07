from __future__ import annotations

import json
import time
import urllib.request
from datetime import timedelta

import pytest

from skymonitor import mail
from skymonitor.alpaca import AlpacaServer
from skymonitor.cli import main
from skymonitor.config import parse_config
from skymonitor.mail import Attachment, EmailError, EmailSettings, compose, send_email
from skymonitor.night import NightLog, night_of
from skymonitor.service import Monitor, SafetyHolder
from skymonitor.synthetic import SkyScene, cloud_bank
from skymonitor.tray import icon_image, state_of
from skymonitor.web import ControlPanel
from skymonitor_helpers import NIGHT, Clock, SkySource

MAIL = EmailSettings(
    enabled=True,
    host="smtp.example.test",
    port=465,
    username="bot@example.test",
    password="hunter2",
    sender="bot@example.test",
    recipients=("you@example.test",),
)


def _settings(**changes) -> dict:
    settings = {
        "language": "zh",
        # Public coordinates: the night then follows solar time, whatever the computer's time zone.
        "site": {"name": "Test site", "latitude": 26.70, "longitude": 100.03},
        "forecast": {"enabled": False},
        "decision": {"open_after_minutes": 2.0},
        "analysis": {"fixed_window_minutes": 10.0},
        "camera": [{"name": "sky", "type": "file", "path": "unused.png"}],
        "output": {"directory": "data"},
        "email": {
            "enabled": True,
            "host": "smtp.example.test",
            "to": ["you@example.test"],
            "username": "bot@example.test",
            "password": "hunter2",
        },
    }
    settings.update(changes)
    return settings


class Outbox:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, tuple[Attachment, ...]]] = []
        self.fail_next = False

    def __call__(self, settings, subject, text, attachments=()) -> None:
        if self.fail_next:
            self.fail_next = False
            raise EmailError("the server is down")
        self.sent.append((subject, text, tuple(attachments)))

    def wait(self, count: int, seconds: float = 10.0) -> None:
        deadline = time.monotonic() + seconds
        while len(self.sent) < count and time.monotonic() < deadline:
            time.sleep(0.02)


def _monitor(tmp_path, clock, camera, outbox, **changes):
    config = parse_config(_settings(**changes), tmp_path / "sky-monitor.toml")
    return Monitor(
        config,
        sources={"sky": camera},
        clock=clock,
        holder=SafetyHolder(),
        mailer=outbox,
        panel_url="http://127.0.0.1:1/",
    )


def _minutes(monitor, clock, count):
    statuses = []
    for _ in range(count):
        statuses.append(monitor.cycle())
        clock.advance(60)
    return statuses


def test_a_night_belongs_to_its_evening() -> None:
    evening = NIGHT
    # 14:40 UTC is 21:20 solar time at 100 E: the evening of 6 October until noon on the 7th.
    assert night_of(evening, 100.03) == night_of(evening + timedelta(hours=8), 100.03) == "2026-10-06"
    assert night_of(evening + timedelta(hours=24), 100.03) == "2026-10-07"
    # The same moment is the morning of the 6th at 120 W: it still belongs to the night of the 5th.
    assert night_of(evening, -120.0) == "2026-10-05"


def test_the_roof_mark_is_kept_until_the_next_afternoon(tmp_path) -> None:
    path = tmp_path / "night.json"
    log = NightLog(path, 100.03)

    before = log.current(NIGHT)
    opened = log.set_roof_opened(NIGHT + timedelta(minutes=5), True)
    later = NightLog(path, 100.03).current(NIGHT + timedelta(hours=6))
    next_night = log.current(NIGHT + timedelta(hours=24))

    assert not before.roof_opened
    assert opened.roof_opened and opened.opened_at
    assert later.roof_opened and later.night == opened.night
    assert not next_night.roof_opened and next_night.night != opened.night
    assert not log.set_roof_opened(NIGHT + timedelta(hours=24, minutes=1), False).roof_opened


def test_mail_is_composed_with_the_pictures_attached() -> None:
    message = compose(MAIL, "可以开顶", "正文", [Attachment("sky.jpg", b"\xff\xd8jpeg", "image/jpeg")])

    assert message["Subject"] == "[Sky Monitor] 可以开顶"
    assert message["To"] == "you@example.test"
    parts = list(message.iter_attachments())
    assert [part.get_filename() for part in parts] == ["sky.jpg"]
    assert parts[0].get_content_type() == "image/jpeg"


class _Smtp:
    instances: list[_Smtp] = []
    refuse_login = False

    def __init__(self, host, port, timeout=None, context=None) -> None:
        self.host, self.port, self.context = host, port, context
        self.calls: list[str] = []
        _Smtp.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.calls.append("quit")

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        if _Smtp.refuse_login:
            raise mail.smtplib.SMTPAuthenticationError(535, b"bad")
        self.calls.append(f"login {user}")

    def send_message(self, message):
        self.calls.append("send " + message["Subject"])


def test_mail_goes_through_ssl_or_starttls_and_failures_carry_no_password(monkeypatch) -> None:
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", _Smtp)
    monkeypatch.setattr(mail.smtplib, "SMTP", _Smtp)
    _Smtp.instances.clear()

    send_email(MAIL, "a", "text")
    send_email(EmailSettings(**{**MAIL.__dict__, "security": "starttls", "port": 587}), "b", "text")

    assert _Smtp.instances[0].calls == ["ehlo", "login bot@example.test", "send [Sky Monitor] a", "quit"]
    assert _Smtp.instances[1].calls == [
        "ehlo",
        "starttls",
        "ehlo",
        "login bot@example.test",
        "send [Sky Monitor] b",
        "quit",
    ]
    assert _Smtp.instances[1].port == 587
    _Smtp.refuse_login = True
    try:
        with pytest.raises(EmailError) as refused:
            send_email(MAIL, "c", "text")
    finally:
        _Smtp.refuse_login = False
    assert "hunter2" not in str(refused.value)
    with pytest.raises(EmailError):
        send_email(EmailSettings(enabled=True), "d", "text")


def test_the_night_mails_once_when_the_roof_may_open_and_once_when_that_is_lost(tmp_path) -> None:
    clock = Clock()
    camera = SkySource(SkyScene(), clock)
    outbox = Outbox()
    monitor = _monitor(tmp_path, clock, camera, outbox)

    opened = _minutes(monitor, clock, 8)
    outbox.wait(1)
    assert opened[-1]["safe"]
    assert len(outbox.sent) == 1
    subject, text, attachments = outbox.sent[0]
    assert subject == "Test site：可以开顶了"
    assert "可以开顶" in text and "http://127.0.0.1:1/" in text
    assert [a.filename for a in attachments] == ["sky.jpg"] and attachments[0].data[:2] == b"\xff\xd8"
    assert monitor.night_state().notified_at is not None

    _minutes(monitor, clock, 3)
    time.sleep(0.2)
    assert len(outbox.sent) == 1

    camera.cloud = cloud_bank(camera.scene.shape, 1.0)
    closed = _minutes(monitor, clock, 2)
    outbox.wait(2)
    assert not closed[-1]["safe"]
    assert outbox.sent[1][0] == "Test site：天空不再满足开顶条件"
    assert monitor.night_state().notified_at is None
    assert monitor.latest_status()["lastEmail"]["ok"] is True

    camera.cloud = None
    _minutes(monitor, clock, 5)
    outbox.wait(3)
    assert len(outbox.sent) == 3 and outbox.sent[2][0] == "Test site：可以开顶了"
    monitor.close()


def test_a_failed_mail_is_tried_again_at_the_next_look(tmp_path) -> None:
    clock = Clock()
    outbox = Outbox()
    monitor = _monitor(tmp_path, clock, SkySource(SkyScene(), clock), outbox)
    _minutes(monitor, clock, 7)
    outbox.fail_next = True

    first = monitor.cycle()
    clock.advance(60)
    deadline = time.monotonic() + 10
    while (monitor.last_email() or {}).get("ok") is not False and time.monotonic() < deadline:
        time.sleep(0.02)
    failure = monitor.last_email()
    second = monitor.cycle()
    outbox.wait(1)

    assert first["safe"] and second["safe"]
    assert failure["error"] == "the server is down"
    assert len(outbox.sent) == 1
    monitor.close()


def test_the_roof_mark_pauses_the_night_and_can_be_cancelled(tmp_path) -> None:
    clock = Clock()
    camera = SkySource(SkyScene(), clock)
    outbox = Outbox()
    monitor = _monitor(tmp_path, clock, camera, outbox)
    _minutes(monitor, clock, 8)
    outbox.wait(1)
    looks = camera.looks

    monitor.set_roof_opened(True)
    paused = _minutes(monitor, clock, 3)
    assert camera.looks == looks
    assert all(status["reason"] == "ROOF_OPENED" and not status["safe"] for status in paused)
    assert all(status["verdict"] == "UNKNOWN" for status in paused)
    assert paused[-1]["message"].startswith("已标记开顶")
    assert paused[-1]["night"]["roofOpened"] is True
    assert monitor._idle

    monitor.set_roof_opened(False)
    resumed = _minutes(monitor, clock, 4)
    assert camera.looks > looks
    assert resumed[-1]["reason"] in ("WAITING", "CLEAR", "NO_DATA")
    assert not monitor._idle
    outbox.wait(2)
    # No "lost" mail for the pause itself; the clear sky after the cancel is a new occasion.
    assert [subject for subject, _, _ in outbox.sent] == ["Test site：可以开顶了"] * 2
    monitor.close()


def test_reminders_repeat_until_read_or_opened(tmp_path) -> None:
    clock = Clock()
    outbox = Outbox()
    monitor = _monitor(
        tmp_path,
        clock,
        SkySource(SkyScene(), clock),
        outbox,
        email={
            "enabled": True,
            "host": "smtp.example.test",
            "to": ["you@example.test"],
            "repeat_minutes": 5,
        },
    )
    _minutes(monitor, clock, 8)
    outbox.wait(1)

    _minutes(monitor, clock, 10)
    outbox.wait(3)
    assert [subject for subject, _, _ in outbox.sent] == ["Test site：可以开顶了"] * 3
    assert "此提醒会定时重复" in outbox.sent[0][1]

    monitor.set_acknowledged(True)
    _minutes(monitor, clock, 12)
    time.sleep(0.2)
    assert len(outbox.sent) == 3
    assert monitor.night_state().acknowledged
    monitor.close()


def test_with_automation_reading_the_devices_the_mark_never_pauses(tmp_path) -> None:
    clock = Clock()
    camera = SkySource(SkyScene(), clock)
    outbox = Outbox()
    monitor = _monitor(tmp_path, clock, camera, outbox)
    monitor.set_automation_probe(lambda: True)
    _minutes(monitor, clock, 8)
    outbox.wait(1)
    looks = camera.looks

    monitor.set_roof_opened(True)
    statuses = _minutes(monitor, clock, 3)

    # N.I.N.A. keeps seeing the real answer, so it does not close the roof; the mail stops.
    assert camera.looks == looks + 3
    assert all(status["safe"] and status["reason"] == "CLEAR" for status in statuses)
    assert statuses[-1]["message"].endswith("（已标记开顶，今晚不再发邮件。）")
    time.sleep(0.2)
    assert len(outbox.sent) == 1
    monitor.close()


def test_without_the_pause_the_mark_only_silences_the_mail(tmp_path) -> None:
    clock = Clock()
    camera = SkySource(SkyScene(), clock)
    outbox = Outbox()
    monitor = _monitor(tmp_path, clock, camera, outbox, operator={"pause_when_opened": False})
    _minutes(monitor, clock, 6)
    monitor.set_roof_opened(True)

    statuses = _minutes(monitor, clock, 3)

    assert statuses[-1]["safe"] and statuses[-1]["reason"] == "CLEAR"
    assert statuses[-1]["message"].endswith("（已标记开顶，今晚不再发邮件。）")
    assert outbox.sent == []
    monitor.close()


@pytest.fixture
def panel_server(tmp_path):
    clock = Clock()
    camera = SkySource(SkyScene(), clock)
    outbox = Outbox()
    monitor = _monitor(tmp_path, clock, camera, outbox)
    config = parse_config(_settings(), tmp_path / "sky-monitor.toml")
    panel = ControlPanel(
        monitor,
        language="zh",
        config_path=config.path,
        preview_dir=config.output.directory / "preview",
        site="Test site",
        email_enabled=True,
    )
    server = AlpacaServer([], host="127.0.0.1", port=0, discovery=False, panel=panel)
    server.start()
    yield server, monitor, clock, outbox
    server.stop()
    monitor.close()


def _http(server, method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.port}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "X-Sky-Monitor": "1"} if method == "POST" else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as error:
        return error.code, "", error.read()


def test_the_control_page_shows_the_answer_and_takes_the_roof_mark(panel_server) -> None:
    server, monitor, clock, outbox = panel_server

    status, kind, page = _http(server, "GET", "/")
    assert status == 200 and kind.startswith("text/html") and "可以开顶，可以拍摄".encode() in page
    # The forecast card waits hidden until a status carries a forecast.
    assert b'<div id="forecastBlock" hidden>' in page and "今夜预报".encode() in page
    _, _, before = _http(server, "GET", "/api/status")
    assert json.loads(before)["status"] is None
    assert _http(server, "GET", "/preview/sky.jpg")[0] == 404

    monitor.cycle()
    _, kind, picture = _http(server, "GET", "/preview/sky.jpg")
    assert kind == "image/jpeg" and picture[:2] == b"\xff\xd8"
    assert _http(server, "GET", "/preview/../status.json")[0] in (400, 404)
    _, _, after = _http(server, "GET", "/api/status")
    assert json.loads(after)["status"]["reason"] == "NO_DATA"
    assert json.loads(after)["status"]["forecast"] is None  # no site coordinates, no forecast

    _, _, marked = _http(server, "POST", "/api/roof", {"opened": True})
    assert json.loads(marked)["night"]["roofOpened"] is True
    assert monitor.night_state().roof_opened
    assert _http(server, "POST", "/api/roof", {"nonsense": 1})[0] == 400
    _, _, cleared = _http(server, "POST", "/api/roof", {"opened": False})
    assert json.loads(cleared)["night"]["roofOpened"] is False

    _, _, mailed = _http(server, "POST", "/api/email/test")
    assert json.loads(mailed) == {"ok": True}
    assert outbox.sent[-1][0] == "测试邮件"
    _, _, read = _http(server, "POST", "/api/ack", {"acknowledged": True})
    assert json.loads(read)["night"]["acknowledged"] is True
    _, _, course = _http(server, "GET", "/api/history?hours=6")
    course = json.loads(course)
    assert course["hours"] == 6 and len(course["points"]) == 1
    assert course["points"][0]["safe"] is False and course["points"][0]["reason"] == "NO_DATA"
    assert _http(server, "GET", "/api/v1/safetymonitor/0/issafe")[0] == 400


def test_the_roof_command_writes_what_the_monitor_reads(tmp_path, capsys) -> None:
    path = tmp_path / "sky-monitor.toml"
    path.write_text('language = "en"\n[[camera]]\nname = "sky"\ntype = "file"\npath = "sky.png"\n', encoding="utf-8")

    assert main(["roof", "status", "--config", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["roofOpened"] is False
    assert main(["roof", "opened", "--config", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["roofOpened"] is True
    assert main(["roof", "cancel", "--config", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["roofOpened"] is False
    assert main(["roof", "read", "--config", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["acknowledged"] is True
    assert main(["test-email", "--config", str(path)]) == 2


def test_the_tray_colour_follows_the_answer() -> None:
    assert state_of(None) == "IDLE"
    assert state_of({"safe": True, "imagingOk": True}) == "IMAGING"
    assert state_of({"safe": True, "imagingOk": False}) == "OPEN"
    assert state_of({"safe": False, "reason": "CLOUDY"}) == "CLOSED"
    assert state_of({"safe": False, "reason": "DAYLIGHT"}) == "IDLE"
    picture = icon_image("CLOSED")
    assert picture.size == (64, 64) and picture.mode == "RGBA"
