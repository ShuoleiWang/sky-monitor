from __future__ import annotations

import io
import json
import time

import pytest

from skymonitor import web
from skymonitor.alpaca import AlpacaServer, SafetyDevice, is_loopback
from skymonitor.cli import _serving
from skymonitor.config import ConfigError, load_config, parse_config, set_setting
from skymonitor.service import Monitor, SafetyHolder
from skymonitor.synthetic import SkyScene
from skymonitor.web import ControlPanel
from skymonitor_helpers import Clock, SkySource

SETTINGS = """language = "zh"
# the site
[site]
name = "Test site"
latitude = 26.70
longitude = 100.03

[forecast]
enabled = false

[[camera]]
name = "sky"
type = "file"
path = "unused.png"

[output]
directory = "data"
"""
LOCAL, NEIGHBOUR, OTHER = "127.0.0.1", "192.168.1.31", "192.168.1.32"


class FakeRequest:
    """Enough of BaseHTTPRequestHandler for the server's own routing."""

    def __init__(
        self, method: str, path: str, client: str, payload: object = None, headers: dict | None = None
    ) -> None:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.path = path
        self.client_address = (client, 50000)
        self.headers = {"Content-Length": str(len(body)), **(headers or {})}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status: int | None = None
        self.close_connection = False

    def send_response(self, code: int) -> None:
        self.status = code

    def send_header(self, name: str, value: str) -> None:
        pass

    def end_headers(self) -> None:
        pass

    def text(self) -> str:
        return self.wfile.getvalue().decode("utf-8")

    def json(self) -> dict:
        return json.loads(self.wfile.getvalue())


class Site:
    def __init__(self, tmp_path, *, share: str = "lan", code: str = "", alpaca_remote: bool = False) -> None:
        self.path = tmp_path / "sky-monitor.toml"
        extra = f'\n[web]\nshare = "{share}"\n' + (f'control_code = "{code}"\n' if code else "")
        self.path.write_text(SETTINGS + extra, encoding="utf-8")
        config = load_config(self.path)
        clock = Clock()
        self.monitor = Monitor(
            config,
            sources={"sky": SkySource(SkyScene(), clock)},
            clock=clock,
            holder=SafetyHolder(),
            mailer=lambda *args: None,
        )
        panel = ControlPanel(
            self.monitor,
            language="zh",
            config_path=config.path,
            preview_dir=config.output.directory / "preview",
            site="Test site",
            email_enabled=True,
            share=config.web.share,
            control_code=config.web.control_code,
            lan_url="http://192.168.1.20:11112/",
        )
        device = SafetyDevice("roof", "roof", "id-roof", lambda: True)
        self.server = AlpacaServer(
            [device], host="127.0.0.1", port=0, discovery=False, panel=panel, alpaca_remote=alpaca_remote
        )

    def ask(self, method: str, path: str, client: str, payload: object = None, **headers: str) -> FakeRequest:
        names = {"page": "X-Sky-Monitor", "code": "X-Control-Code"}
        request = FakeRequest(method, path, client, payload, {names[key]: value for key, value in headers.items()})
        self.server._serve(request, method)
        return request

    def close(self) -> None:
        self.server.stop()
        self.monitor.close()


@pytest.fixture
def site(tmp_path):
    made: list[Site] = []

    def make(**options) -> Site:
        made.append(Site(tmp_path, **options))
        return made[-1]

    yield make
    for each in made:
        each.close()


def test_loopback_addresses_are_recognised() -> None:
    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("::ffff:127.0.0.1")
    assert not is_loopback("192.168.1.31") and not is_loopback("100.64.0.7") and not is_loopback("nonsense")


def test_other_computers_see_the_page_read_only(site) -> None:
    shared = site()

    page = shared.ask("GET", "/", NEIGHBOUR)
    status = shared.ask("GET", "/api/status", NEIGHBOUR).json()
    own = shared.ask("GET", "/api/status", LOCAL).json()

    assert page.status == 200 and "sky-monitor.toml" not in page.text()
    assert "sky-monitor.toml" in shared.ask("GET", "/", LOCAL).text()
    assert status["viewer"] == {"local": False, "share": "lan", "codeSet": False, "lanUrl": None}
    assert own["viewer"]["local"] is True and own["viewer"]["lanUrl"] == "http://192.168.1.20:11112/"


def test_a_page_kept_local_refuses_other_computers(site) -> None:
    private = site(share="local")

    assert private.ask("GET", "/", NEIGHBOUR).status == 403
    assert private.ask("GET", "/api/status", NEIGHBOUR).status == 403
    assert private.ask("GET", "/", LOCAL).status == 200


def test_buttons_from_other_computers_need_the_code(site) -> None:
    shared = site()

    no_code = shared.ask("POST", "/api/roof", NEIGHBOUR, {"opened": True}, page="1")
    assert (no_code.status, no_code.json()["error"]) == (403, "local-only")

    saved = shared.ask("POST", "/api/control-code", LOCAL, {"code": "star42"}, page="1")
    assert saved.status == 200 and saved.json()["viewer"]["codeSet"] is True
    assert load_config(shared.path).web.control_code == "star42"
    assert "# the site" in shared.path.read_text(encoding="utf-8")

    wrong = shared.ask("POST", "/api/roof", NEIGHBOUR, {"opened": True}, page="1", code="star41")
    right = shared.ask("POST", "/api/roof", NEIGHBOUR, {"opened": True}, page="1", code="star42")

    assert (wrong.status, wrong.json()) == (403, {"error": "bad-code", "remaining": 4})
    assert right.status == 200 and right.json()["night"]["roofOpened"] is True
    assert shared.monitor.night_state().roof_opened
    # On the observatory computer itself no code is asked for.
    assert shared.ask("POST", "/api/roof", LOCAL, {"opened": False}, page="1").status == 200


def test_wrong_codes_lock_an_address_out_for_a_while(site, monkeypatch) -> None:
    shared = site(code="star42")

    answers = [
        shared.ask("POST", "/api/ack", NEIGHBOUR, {"acknowledged": True}, page="1", code="guess").json()
        for _ in range(5)
    ]
    locked = shared.ask("POST", "/api/ack", NEIGHBOUR, {"acknowledged": True}, page="1", code="star42")
    elsewhere = shared.ask("POST", "/api/ack", OTHER, {"acknowledged": True}, page="1", code="star42")

    assert [answer["remaining"] for answer in answers] == [4, 3, 2, 1, 0]
    assert locked.status == 429 and locked.json()["retryAfter"] > 500
    assert elsewhere.status == 200
    monkeypatch.setattr(web, "LOCK_SECONDS", 0.05)
    time.sleep(0.1)
    assert shared.ask("POST", "/api/ack", NEIGHBOUR, {"acknowledged": True}, page="1", code="star42").status == 200


def test_posts_without_the_pages_header_are_refused(site) -> None:
    shared = site(code="star42")

    forged = shared.ask("POST", "/api/roof", LOCAL, {"opened": True})
    forged_code = shared.ask("POST", "/api/control-code", LOCAL, {"code": "evil1"})

    assert (forged.status, forged.json()["error"]) == (403, "missing-header")
    assert forged_code.status == 403
    assert not shared.monitor.night_state().roof_opened
    assert load_config(shared.path).web.control_code == "star42"


def test_the_code_is_set_on_this_computer_only(site) -> None:
    shared = site(code="star42")

    remote = shared.ask("POST", "/api/control-code", NEIGHBOUR, {"code": "mine12"}, page="1", code="star42")
    short = shared.ask("POST", "/api/control-code", LOCAL, {"code": "ab"}, page="1")
    cleared = shared.ask("POST", "/api/control-code", LOCAL, {"code": ""}, page="1")

    assert (remote.status, remote.json()["error"]) == (403, "local-only")
    assert short.status == 400
    assert cleared.status == 200 and cleared.json()["viewer"]["codeSet"] is False
    assert load_config(shared.path).web.control_code == ""
    assert (
        shared.ask("POST", "/api/ack", NEIGHBOUR, {"acknowledged": True}, page="1", code="star42").json()["error"]
        == "local-only"
    )


def test_the_safety_devices_stay_on_this_computer(site) -> None:
    shared = site()
    devices_shared = site(alpaca_remote=True)

    assert shared.ask("GET", "/api/v1/safetymonitor/0/connected", NEIGHBOUR).status == 403
    assert shared.ask("GET", "/management/v1/configureddevices", NEIGHBOUR).status == 403
    assert shared.ask("GET", "/api/v1/safetymonitor/0/connected", LOCAL).status == 200
    assert devices_shared.ask("GET", "/api/v1/safetymonitor/0/connected", NEIGHBOUR).status == 200


def test_where_the_server_listens() -> None:
    def config(**sections):
        settings = {"camera": [{"name": "sky", "type": "file", "path": "x.png"}], **sections}
        return parse_config(settings, __import__("pathlib").Path("/tmp/s.toml"))

    assert _serving(config()) == ("127.0.0.1", False)
    assert _serving(config(web={"share": "lan"})) == ("0.0.0.0", False)
    assert _serving(config(alpaca={"host": "0.0.0.0"})) == ("0.0.0.0", True)


def test_web_settings_are_read_strictly(tmp_path, monkeypatch) -> None:
    def parse(**web_settings):
        return parse_config(
            {"camera": [{"name": "sky", "type": "file", "path": "x.png"}], "web": web_settings}, tmp_path / "s.toml"
        )

    with pytest.raises(ConfigError, match="web.share"):
        parse(share="public")
    with pytest.raises(ConfigError, match="web.control_code"):
        parse(control_code="ab")
    monkeypatch.setenv("SKY_TEST_CODE", "night77")
    assert parse(share="lan", control_code="${SKY_TEST_CODE}").web.control_code == "night77"


def test_one_setting_is_written_and_everything_else_kept(tmp_path) -> None:
    path = tmp_path / "s.toml"
    path.write_text(
        '# my notes\n[web]\nshare = "lan" # shared\ncontrol_code = "old1"\n\n[[camera]]\nname = "sky"\n',
        encoding="utf-8",
    )

    set_setting(path, "web", "control_code", "new2")
    set_setting(path, "output", "directory", "D:/data")
    text = path.read_text(encoding="utf-8")

    assert 'control_code = "new2"' in text and "old1" not in text
    assert text.startswith('# my notes\n[web]\nshare = "lan" # shared\n')
    assert text.rstrip().endswith('[output]\ndirectory = "D:/data"')
    fresh = tmp_path / "t.toml"
    fresh.write_text('[[camera]]\nname = "sky"\n[web]\n', encoding="utf-8")
    set_setting(fresh, "web", "control_code", "abc12")
    assert fresh.read_text(encoding="utf-8").endswith('[web]\ncontrol_code = "abc12"\n')
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ConfigError):
        set_setting(path, "we]b", "x", "y")
    assert path.read_text(encoding="utf-8") == before
