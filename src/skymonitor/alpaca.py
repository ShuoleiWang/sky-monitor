"""ASCOM Alpaca SafetyMonitor devices and the Alpaca discovery responder.

N.I.N.A., Voyager, SGP, ACP and other observatory software read ``IsSafe``
from here.  Standard library only.  A device reports unsafe while no client
has connected to it; whether the monitor's answer is present and fresh is the
business of the callable each device is given.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from . import __version__

if TYPE_CHECKING:
    from .web import ControlPanel

DISCOVERY_PORT = 32227
_NOT_IMPLEMENTED = 0x400
_ACTION_NOT_IMPLEMENTED = 0x40C
_DEVICE_PATH = re.compile(r"^/api/v1/([a-z]+)/([^/]+)/([a-z]+)$")
_TEXT_MEMBERS = ("description", "driverinfo", "driverversion", "name")
_LOG = logging.getLogger(__name__)
_NO_VALUE = object()


def is_loopback(address: str) -> bool:
    """Whether a client address is this computer."""

    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return ip.is_loopback or bool(mapped and mapped.is_loopback)


@dataclass(frozen=True)
class SafetyDevice:
    name: str
    description: str
    unique_id: str
    is_safe: Callable[[], bool]


class _HttpServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second monitor take the port of the first without an error.
    allow_reuse_address = os.name != "nt"


class AlpacaServer:
    def __init__(
        self,
        devices: Sequence[SafetyDevice],
        *,
        host: str = "127.0.0.1",
        port: int = 11112,
        discovery: bool = True,
        discovery_port: int = DISCOVERY_PORT,
        location: str = "",
        panel: ControlPanel | None = None,
        alpaca_remote: bool | None = None,
    ) -> None:
        self._devices = tuple(devices)
        self._panel = panel
        # Whether other computers may use the safety devices (and find them by discovery);
        # by default only when the server itself listens beyond this computer.
        self._alpaca_remote = (
            (not is_loopback(host) and host != "localhost") if alpaca_remote is None else alpaca_remote
        )
        self._connected = [False] * len(self._devices)
        self._location = location
        self._lock = threading.Lock()
        self._transaction = 0
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        # A responder for a server only this machine can reach answers only this machine.
        self._local_only = not self._alpaca_remote
        self._http = _HttpServer((host, port), self._handler())
        self._discovery = self._open_discovery(discovery_port) if discovery else None

    def any_connected(self) -> bool:
        """Whether some client (N.I.N.A., a dome driver) has connected to one of the devices."""

        with self._lock:
            return any(self._connected)

    @property
    def port(self) -> int:
        return int(self._http.server_address[1])

    @property
    def discovery_port(self) -> int | None:
        return None if self._discovery is None else int(self._discovery.getsockname()[1])

    def start(self) -> None:
        targets = [lambda: self._http.serve_forever(poll_interval=0.25)]
        if self._discovery is not None:
            targets.append(self._answer_discovery)
        for target in targets:
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        if self._threads:
            self._http.shutdown()
        self._http.server_close()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads.clear()
        if self._discovery is not None:
            self._discovery.close()

    def _open_discovery(self, port: int) -> socket.socket | None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("", port))
            listener.settimeout(0.5)
        except OSError as error:
            listener.close()
            _LOG.warning("Alpaca discovery is off (UDP port %d): %s", port, error)
            return None
        return listener

    def _answer_discovery(self) -> None:
        listener = self._discovery
        assert listener is not None
        answer = json.dumps({"AlpacaPort": self.port}).encode("ascii")
        while not self._stop.is_set():
            try:
                data, sender = listener.recvfrom(1024)
            except TimeoutError:
                continue
            except OSError:
                # Windows reports a refused earlier reply here; the socket itself is fine.
                if self._stop.is_set():
                    return
                continue
            if not data.startswith(b"alpacadiscovery1"):
                continue
            if self._local_only and not is_loopback(sender[0]):
                continue
            try:
                listener.sendto(answer, sender)
            except OSError:
                continue

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "SkyMonitor"
            # An idle keep-alive connection gives its thread back.
            timeout = 30

            def do_GET(self) -> None:
                server._serve(self, "GET")

            def do_PUT(self) -> None:
                server._serve(self, "PUT")

            def do_POST(self) -> None:
                server._serve(self, "POST")

            def log_message(self, format: str, *args: object) -> None:
                _LOG.debug("alpaca: " + format, *args)

        return Handler

    @staticmethod
    def _send(request: BaseHTTPRequestHandler, status: int, kind: str, body: bytes) -> None:
        request.send_response(status)
        request.send_header("Content-Type", kind)
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    def _refuse(self, request: BaseHTTPRequestHandler, status: int, text: str) -> None:
        self._send(request, status, "text/plain; charset=utf-8", text.encode("utf-8"))

    def _reply(
        self,
        request: BaseHTTPRequestHandler,
        client_transaction: int,
        *,
        value: object = _NO_VALUE,
        error: int = 0,
        message: str = "",
    ) -> None:
        with self._lock:
            self._transaction += 1
            server_transaction = self._transaction
        payload: dict[str, object] = {} if value is _NO_VALUE else {"Value": value}
        payload.update(
            ClientTransactionID=client_transaction,
            ServerTransactionID=server_transaction,
            ErrorNumber=error,
            ErrorMessage=message,
        )
        self._send(request, 200, "application/json; charset=utf-8", json.dumps(payload).encode())

    def _is_safe(self, index: int) -> bool:
        with self._lock:
            connected = self._connected[index]
        try:
            return connected and bool(self._devices[index].is_safe())
        except Exception:  # an answer that cannot be computed is not a safe one
            _LOG.exception("the safety state could not be read")
            return False

    def _serve(self, request: BaseHTTPRequestHandler, method: str) -> None:
        split = urlsplit(request.path)
        path = split.path.rstrip("/").lower()
        raw = b""
        if method in ("PUT", "POST"):
            try:
                length = int(request.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 <= length <= 65536:
                request.close_connection = True
                return self._refuse(request, 400, "bad Content-Length")
            raw = request.rfile.read(length)
        alpaca_path = path.startswith(("/api/v1/", "/management/"))
        client = str(request.client_address[0])
        if alpaca_path and not self._alpaca_remote and not is_loopback(client):
            return self._refuse(request, 403, "the safety devices serve this computer only")
        if self._panel is not None and not alpaca_path:
            if self._panel.handle(request, method, request.path, raw, client=client):
                return
        if method == "POST":
            return self._refuse(request, 404 if not alpaca_path else 400, "unknown address")
        if method == "GET":
            # Alpaca: parameter names are case-insensitive on GET and exact on PUT.
            pairs = parse_qs(split.query, keep_blank_values=True)
            fields = {name.lower(): values[-1] for name, values in pairs.items()}
            transaction_text = fields.get("clienttransactionid")
        else:
            body = raw.decode("utf-8", "replace")
            fields = {name: values[-1] for name, values in parse_qs(body, keep_blank_values=True).items()}
            transaction_text = fields.get("ClientTransactionID")
        client_transaction = 0
        if transaction_text is not None:
            if not transaction_text.isdigit() or int(transaction_text) > 0xFFFFFFFF:
                return self._refuse(request, 400, "ClientTransactionID is not an unsigned integer")
            client_transaction = int(transaction_text)

        if path == "/management/apiversions":
            return self._reply(request, client_transaction, value=[1])
        if path == "/management/v1/description":
            description = {
                "ServerName": "Sky Monitor",
                "Manufacturer": "Sky Monitor contributors",
                "ManufacturerVersion": __version__,
                "Location": self._location,
            }
            return self._reply(request, client_transaction, value=description)
        if path == "/management/v1/configureddevices":
            configured = [
                {
                    "DeviceName": device.name,
                    "DeviceType": "SafetyMonitor",
                    "DeviceNumber": number,
                    "UniqueID": device.unique_id,
                }
                for number, device in enumerate(self._devices)
            ]
            return self._reply(request, client_transaction, value=configured)
        if path in ("", "/setup") or path.startswith("/setup/"):
            page = "<html><body><h1>Sky Monitor</h1><p>Settings live in sky-monitor.toml.</p></body></html>"
            return self._send(request, 200, "text/html; charset=utf-8", page.encode("utf-8"))

        match = _DEVICE_PATH.match(path)
        if match is None:
            return self._refuse(request, 400 if path.startswith("/api/") else 404, "unknown address")
        kind, number, member = match.groups()
        if kind != "safetymonitor" or not number.isdigit() or int(number) >= len(self._devices):
            return self._refuse(request, 400, "no such device")
        index = int(number)
        device = self._devices[index]

        if method == "GET":
            if member == "connected":
                with self._lock:
                    value: object = self._connected[index]
            elif member == "connecting":
                value = False
            elif member in _TEXT_MEMBERS:
                value = {
                    "description": device.description,
                    "driverinfo": f"Sky Monitor {__version__}: sky cameras to roof safety",
                    "driverversion": "0.1",
                    "name": device.name,
                }[member]
            elif member == "interfaceversion":
                value = 3
            elif member == "supportedactions":
                value = []
            elif member == "issafe":
                value = self._is_safe(index)
            elif member == "devicestate":
                moment = datetime.now(UTC).isoformat(timespec="milliseconds")
                value = [
                    {"Name": "IsSafe", "Value": self._is_safe(index)},
                    {"Name": "TimeStamp", "Value": moment},
                ]
            else:
                return self._refuse(request, 400, "unknown member")
            return self._reply(request, client_transaction, value=value)

        if member == "connected":
            wanted = fields.get("Connected", "").lower()
            if wanted not in ("true", "false"):
                return self._refuse(request, 400, "Connected must be True or False")
            connect: bool | None = wanted == "true"
        elif member in ("connect", "disconnect"):
            connect = member == "connect"
        elif member == "action":
            return self._reply(request, client_transaction, error=_ACTION_NOT_IMPLEMENTED, message="no actions")
        elif member in ("commandblind", "commandbool", "commandstring"):
            return self._reply(request, client_transaction, error=_NOT_IMPLEMENTED, message="not implemented")
        else:
            return self._refuse(request, 400, "unknown member")
        with self._lock:
            self._connected[index] = bool(connect)
        return self._reply(request, client_transaction)
