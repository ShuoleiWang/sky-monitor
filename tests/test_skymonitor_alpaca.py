from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request

import pytest

from skymonitor.alpaca import AlpacaServer, SafetyDevice


class Answers:
    roof = False
    imaging = False

    def broken(self) -> bool:
        raise RuntimeError("no answer")


@pytest.fixture
def served():
    answers = Answers()
    devices = [
        SafetyDevice("Sky Monitor - roof may open", "roof", "id-roof", lambda: answers.roof),
        SafetyDevice("Sky Monitor - imaging may run", "imaging", "id-imaging", lambda: answers.imaging),
        SafetyDevice("Broken", "raises", "id-broken", answers.broken),
    ]
    server = AlpacaServer(devices, host="127.0.0.1", port=0, discovery_port=0, location="Test site")
    server.start()
    yield server, answers
    server.stop()


def _call(server: AlpacaServer, method: str, path: str, fields: dict[str, str] | None = None):
    address = f"http://127.0.0.1:{server.port}{path}"
    data = None
    if method == "GET" and fields:
        address += "?" + urllib.parse.urlencode(fields)
    elif method == "PUT":
        data = urllib.parse.urlencode(fields or {}).encode("ascii")
    request = urllib.request.Request(address, data=data, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8")


def _value(server: AlpacaServer, member: str, device: int = 0):
    status, body = _call(server, "GET", f"/api/v1/safetymonitor/{device}/{member}")
    assert status == 200 and body["ErrorNumber"] == 0
    return body["Value"]


def test_a_device_is_unsafe_until_a_client_connects_and_the_monitor_says_safe(served) -> None:
    server, answers = served
    answers.roof = True

    before = _value(server, "issafe")
    status, body = _call(server, "PUT", "/api/v1/safetymonitor/0/connected", {"Connected": "True"})
    connected = _value(server, "issafe")
    answers.roof = False
    cloudy = _value(server, "issafe")
    answers.roof = True
    _call(server, "PUT", "/api/v1/safetymonitor/0/connected", {"Connected": "False"})
    after = _value(server, "issafe")

    assert (status, body["ErrorNumber"], "Value" in body) == (200, 0, False)
    assert (before, connected, cloudy, after) == (False, True, False, False)
    assert server.any_connected() is False
    _call(server, "PUT", "/api/v1/safetymonitor/1/connect")
    assert server.any_connected() is True


def test_the_two_devices_answer_separately(served) -> None:
    server, answers = served
    answers.roof, answers.imaging = True, False
    for device in (0, 1):
        _call(server, "PUT", f"/api/v1/safetymonitor/{device}/connect")

    assert _value(server, "connected", 0) is True
    assert (_value(server, "issafe", 0), _value(server, "issafe", 1)) == (True, False)
    assert _value(server, "devicestate", 0)[0] == {"Name": "IsSafe", "Value": True}


def test_an_answer_that_cannot_be_computed_is_unsafe(served) -> None:
    server, _ = served
    _call(server, "PUT", "/api/v1/safetymonitor/2/connect")

    assert _value(server, "issafe", 2) is False


def test_the_common_members_describe_the_device(served) -> None:
    server, _ = served

    assert _value(server, "name") == "Sky Monitor - roof may open"
    assert _value(server, "description") == "roof"
    assert _value(server, "interfaceversion") == 3
    assert _value(server, "driverversion") == "0.1"
    assert _value(server, "supportedactions") == []
    assert _value(server, "connecting") is False
    assert "Sky Monitor" in _value(server, "driverinfo")


def test_transaction_numbers_are_echoed_and_counted(served) -> None:
    server, _ = served

    _, first = _call(server, "GET", "/api/v1/safetymonitor/0/issafe", {"ClientTransactionID": "41", "ClientID": "7"})
    _, second = _call(server, "GET", "/api/v1/safetymonitor/0/issafe", {"clienttransactionid": "42"})
    _, put = _call(
        server, "PUT", "/api/v1/safetymonitor/0/connected", {"Connected": "true", "ClientTransactionID": "43"}
    )

    assert (first["ClientTransactionID"], second["ClientTransactionID"], put["ClientTransactionID"]) == (41, 42, 43)
    assert first["ServerTransactionID"] < second["ServerTransactionID"] < put["ServerTransactionID"]


def test_malformed_requests_are_refused(served) -> None:
    server, _ = served

    assert _call(server, "GET", "/api/v1/safetymonitor/9/issafe")[0] == 400
    assert _call(server, "GET", "/api/v1/telescope/0/connected")[0] == 400
    assert _call(server, "GET", "/api/v1/safetymonitor/0/nosuchmember")[0] == 400
    assert _call(server, "GET", "/api/v1/safetymonitor/0/issafe", {"ClientTransactionID": "-1"})[0] == 400
    assert _call(server, "PUT", "/api/v1/safetymonitor/0/connected", {"Connected": "maybe"})[0] == 400
    # Alpaca: parameter names are exact on PUT.
    assert _call(server, "PUT", "/api/v1/safetymonitor/0/connected", {"connected": "true"})[0] == 400
    assert _call(server, "GET", "/elsewhere")[0] == 404


def test_actions_and_commands_are_not_implemented(served) -> None:
    server, _ = served

    _, action = _call(server, "PUT", "/api/v1/safetymonitor/0/action", {"Action": "x", "Parameters": ""})
    _, command = _call(server, "PUT", "/api/v1/safetymonitor/0/commandstring", {"Command": "x", "Raw": "false"})

    assert action["ErrorNumber"] == 0x40C
    assert command["ErrorNumber"] == 0x400


def test_the_management_interface_lists_the_devices(served) -> None:
    server, _ = served

    _, versions = _call(server, "GET", "/management/apiversions")
    _, description = _call(server, "GET", "/management/v1/description")
    _, devices = _call(server, "GET", "/management/v1/configureddevices")

    assert versions["Value"] == [1]
    assert description["Value"]["ServerName"] == "Sky Monitor"
    assert description["Value"]["Location"] == "Test site"
    assert devices["Value"][:2] == [
        {
            "DeviceName": "Sky Monitor - roof may open",
            "DeviceType": "SafetyMonitor",
            "DeviceNumber": 0,
            "UniqueID": "id-roof",
        },
        {
            "DeviceName": "Sky Monitor - imaging may run",
            "DeviceType": "SafetyMonitor",
            "DeviceNumber": 1,
            "UniqueID": "id-imaging",
        },
    ]


def test_discovery_answers_with_the_port(served) -> None:
    server, _ = served
    assert server.discovery_port

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.settimeout(5)
        client.sendto(b"something else", ("127.0.0.1", server.discovery_port))
        client.sendto(b"alpacadiscovery1", ("127.0.0.1", server.discovery_port))
        answer, _ = client.recvfrom(1024)

    assert json.loads(answer) == {"AlpacaPort": server.port}


def test_a_taken_port_is_an_error_and_discovery_can_be_off() -> None:
    first = AlpacaServer([], host="127.0.0.1", port=0, discovery=False)
    first.start()
    try:
        assert first.discovery_port is None
        with pytest.raises(OSError):
            AlpacaServer([], host="127.0.0.1", port=first.port, discovery=False)
    finally:
        first.stop()
