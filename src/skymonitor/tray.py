"""A tray icon with the monitor behind it: green when the roof may open, red when not.

Needs ``pystray`` (``pip install sky-monitor[tray]``); the Windows build ships it.
"""

from __future__ import annotations

import threading
import time
import webbrowser
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw

if TYPE_CHECKING:
    from .service import Monitor

_COLOURS = {
    "IMAGING": (34, 197, 94),
    "OPEN": (34, 197, 94),
    "CLOSED": (220, 38, 38),
    "IDLE": (120, 130, 140),
}
_TEXTS = {
    "en": {
        "page": "Open the status page",
        "opened": "Roof opened",
        "cancel": "Cancel 'roof opened'",
        "read": "Reminder read",
        "quit": "Quit",
    },
    "zh": {"page": "打开状态页", "opened": "已开顶", "cancel": "取消已开顶", "read": "提醒已读", "quit": "退出"},
}


def icon_image(state: str, size: int = 64) -> Image.Image:
    """A filled disc in the colour of ``state``."""

    picture = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(picture)
    colour = _COLOURS.get(state, _COLOURS["IDLE"])
    draw.ellipse([4, 4, size - 5, size - 5], fill=colour + (255,), outline=(255, 255, 255, 200), width=3)
    return picture


def state_of(status: dict | None) -> str:
    """The tray colour for a status: IMAGING, OPEN, CLOSED or IDLE."""

    if not status:
        return "IDLE"
    if status.get("imagingOk"):
        return "IMAGING"
    if status.get("safe"):
        return "OPEN"
    if status.get("reason") in ("DAYLIGHT", "ROOF_OPENED", "STOPPED"):
        return "IDLE"
    return "CLOSED"


def run_tray(monitor: Monitor, stop: threading.Event, *, language: str, url: str) -> int:
    """Show the icon and run the monitor until Quit; returns the exit code."""

    try:
        import pystray
    except ImportError:
        print("sky-monitor: the tray needs pystray: pip install sky-monitor[tray]")
        return 2
    texts = _TEXTS.get(language, _TEXTS["en"])

    def open_page(icon, item) -> None:
        webbrowser.open(url)

    def toggle_roof(icon, item) -> None:
        monitor.set_roof_opened(not monitor.night_state().roof_opened)
        icon.update_menu()

    def quit_monitor(icon, item) -> None:
        stop.set()
        icon.stop()

    def roof_label(item) -> str:
        return texts["cancel"] if monitor.night_state().roof_opened else texts["opened"]

    def acknowledge(icon, item) -> None:
        monitor.set_acknowledged(True)
        icon.update_menu()

    def can_acknowledge(item) -> bool:
        night = monitor.night_state()
        return bool(night.notified_at) and not night.acknowledged and not night.roof_opened

    menu = pystray.Menu(
        pystray.MenuItem(texts["page"], open_page, default=True),
        pystray.MenuItem(roof_label, toggle_roof),
        pystray.MenuItem(texts["read"], acknowledge, enabled=can_acknowledge),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(texts["quit"], quit_monitor),
    )
    icon = pystray.Icon("sky-monitor", icon_image("IDLE"), "Sky Monitor", menu)

    def follow(icon) -> None:
        icon.visible = True
        worker = threading.Thread(target=monitor.run, args=(stop,), name="monitor", daemon=True)
        worker.start()
        shown = None
        while not stop.is_set():
            status = monitor.latest_status()
            state = state_of(status)
            if state != shown:
                icon.icon = icon_image(state)
                shown = state
            if status:
                icon.title = str(status.get("message", "Sky Monitor"))[:120]
            time.sleep(3.0)
        icon.stop()

    icon.run(setup=follow)
    stop.set()
    return 0
