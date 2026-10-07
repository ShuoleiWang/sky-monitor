"""The published status in one sentence, in Chinese or English."""

from __future__ import annotations

import locale
import os
import sys
from collections.abc import Mapping
from datetime import datetime

_SITE = {
    "en": {
        "DAYLIGHT": "Not dark: the Sun is at {sun} deg (the roof needs {gate} deg or lower).",
        "CLEAR_IMAGING": "Clear sky: the roof may open and imaging may run. {cameras}",
        "CLEAR": "Clear sky: the roof may open; not dark enough to image yet (Sun at {sun} deg). {cameras}",
        "WAITING": "Watching for {held} of {needed} min: stars over {mean}% of the sky on average ({need}% opens the roof). {cameras}",
        "PARTLY_HOLDING": "Partly cloudy: the roof may stay open, imaging should pause. {cameras}",
        "PARTLY_CLOUDY": "Partly cloudy: keep the roof closed. {cameras}",
        "CLOUDY_HOLDING": "The stars are gone; the roof closes unless they are back at the next look. {cameras}",
        "CLOUDY": "Not enough stars (cloud, fog or a wet camera): keep the roof closed. {cameras}",
        "NO_DATA_HOLDING": "No camera data; the roof closes unless it returns shortly. {cameras}",
        "NO_DATA": "No usable camera data: keep the roof closed. {cameras}",
        "STARTING": "Getting ready; the roof stays closed meanwhile. {cameras}",
        "STOPPED": "The monitor is not running: keep the roof closed.",
        "MONITOR_ERROR": "The monitor failed in this cycle: keep the roof closed.",
        "ROOF_OPENED": "The roof is marked open: no more watching or mail tonight (cancel the mark to resume).",
        "RAIN_FORECAST": "The sky is good, but rain is forecast within {rain} h: the roof stays closed. {cameras}",
        "OPENED_NOTE": " (The roof is marked open: no more mail tonight.)",
    },
    "zh": {
        "DAYLIGHT": "天还没黑：太阳高度 {sun}°（开顶需低于 {gate}°）。",
        "CLEAR_IMAGING": "天空晴朗：可以开顶，可以拍摄。{cameras}",
        "CLEAR": "天空晴朗：可以开顶；天还不够黑，暂不拍摄（太阳高度 {sun}°）。{cameras}",
        "WAITING": "已观察 {held}/{needed} 分钟，平均 {mean}% 的天区有星（达到 {need}% 即允许开顶）。{cameras}",
        "PARTLY_HOLDING": "部分有云：顶可保持打开，建议暂停拍摄。{cameras}",
        "PARTLY_CLOUDY": "部分有云：不要开顶。{cameras}",
        "CLOUDY_HOLDING": "星点消失；下一次仍看不到就关顶。{cameras}",
        "CLOUDY": "看不到足够的星（有云、起雾或镜头有水）：不要开顶。{cameras}",
        "NO_DATA_HOLDING": "相机暂无数据；短时间内不恢复就关顶。{cameras}",
        "NO_DATA": "没有可用的相机数据：不要开顶。{cameras}",
        "STARTING": "正在准备，期间不开顶。{cameras}",
        "STOPPED": "监测程序未运行：不要开顶。",
        "MONITOR_ERROR": "本轮监测出错：不要开顶。",
        "ROOF_OPENED": "已标记开顶：今晚不再监测，也不再发邮件（点“取消已开顶”可恢复）。",
        "RAIN_FORECAST": "天空已达标，但预报 {rain} 小时内有雨，先不开顶。{cameras}",
        "OPENED_NOTE": "（已标记开顶，今晚不再发邮件。）",
    },
}

_CAMERA = {
    "en": {
        "measured": "{name}: stars over {coverage}% of the sky (about {cloud}% cloud), {stars} stars (needs {required})",
        "CONFIRMING": "{name}: confirming the stars",
        "WARMING_UP": "{name}: learning the fixed bright spots (about {minutes} min)",
        "TOO_BRIGHT": "{name}: the picture is too bright",
        "DAYLIGHT": "",
        "failed": "{name}: no data ({reason})",
    },
    "zh": {
        "measured": "{name}：{coverage}% 的天区有星（云量约 {cloud}%），共 {stars} 颗（需要 {required} 颗）",
        "CONFIRMING": "{name}：正在确认星点",
        "WARMING_UP": "{name}：正在学习固定亮点（约 {minutes} 分钟）",
        "TOO_BRIGHT": "{name}：画面过亮",
        "DAYLIGHT": "",
        "failed": "{name}：无数据（{reason}）",
    },
}

_MEASURED = frozenset({"CLEAR", "PARTLY_CLOUDY", "FEW_STARS", "LOW_COVERAGE"})
_PREPARING = frozenset({"CONFIRMING", "WARMING_UP"})


def resolve_language(setting: str = "auto") -> str:
    """ "zh" or "en"; "auto" follows the system's language."""

    if setting in ("zh", "en"):
        return setting
    for variable in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(variable, "")
        if value:
            return "zh" if value.lower().startswith("zh") else "en"
    if sys.platform == "win32":
        try:
            import ctypes

            # The low ten bits of a Windows language id are the language; 0x04 is Chinese.
            if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x04:
                return "zh"
        except (AttributeError, OSError):
            pass
    try:
        name = (locale.getlocale()[0] or "").lower()
    except ValueError:
        name = ""
    return "zh" if name.startswith(("zh", "chinese")) else "en"


def _camera_text(camera: Mapping[str, object], language: str, warm_up_minutes: float) -> str:
    texts = _CAMERA[language]
    reason = str(camera.get("reason", ""))
    name = camera.get("name", "?")
    if reason in _MEASURED:
        coverage = camera.get("coverage") or 0.0
        return texts["measured"].format(
            name=name,
            coverage=round(100 * float(coverage)),
            cloud=round(100 * (1 - float(coverage))),
            stars=camera.get("starCount", 0),
            required=camera.get("requiredStars", 0),
        )
    if reason in texts:
        return texts[reason].format(name=name, minutes=round(warm_up_minutes))
    return texts["failed"].format(name=name, reason=reason)


def describe(status: Mapping[str, object], language: str, warm_up_minutes: float = 15.0) -> str:
    """One sentence for a person: the answer and what it was read from."""

    reason = str(status.get("reason", ""))
    cameras = status.get("cameras") or []
    if reason == "CLEAR" and status.get("imagingOk"):
        reason = "CLEAR_IMAGING"
    elif reason == "NO_DATA" and cameras and all(c.get("reason") in _PREPARING for c in cameras):
        # The cameras deliver; they are still confirming stars or learning the fixed sources.
        reason = "STARTING"
    template = _SITE[language].get(reason)
    if template is None:
        return reason
    sun = (status.get("sun") or {}).get("altitudeDeg")
    described = [text for camera in cameras if (text := _camera_text(camera, language, warm_up_minutes))]
    separator = "；" if language == "zh" else "; "
    held_coverage = status.get("heldCoverage")
    forecast = status.get("forecast") or {}
    text = template.format(
        rain=f"{float(forecast.get('rainWindowHours') or 0):g}" if isinstance(forecast, Mapping) else "?",
        sun="?" if sun is None else f"{sun:+.1f}",
        gate=f"{status.get('sunGateDeg', 0):+.0f}",
        held=int(float(status.get("heldSeconds", 0)) // 60),
        needed=round(float(status.get("openAfterSeconds", 0)) / 60),
        mean="?" if held_coverage is None else round(100 * float(held_coverage)),
        need=round(100 * float(status.get("openCoverage", 0.7))),
        cameras=separator.join(described),
    ).strip()
    night = status.get("night") or {}
    if reason != "ROOF_OPENED" and isinstance(night, Mapping) and night.get("roofOpened"):
        text += _SITE[language]["OPENED_NOTE"]
    return text


_EMAIL = {
    "en": {
        "clear": (
            "{site}: the roof may open",
            "{message}\n\nTime: {time}\nThis reminder repeats until 'Read' or 'Roof opened' is pressed.\n{link}",
        ),
        "lost": ("{site}: the sky is no longer clear enough", "{message}\n\nTime: {time}\n{link}"),
        "test": ("test mail", "The mail settings of Sky Monitor work.\n{link}"),
        "link": "Status page with the 'Read' and 'Roof opened' buttons: {url}",
    },
    "zh": {
        "clear": (
            "{site}：可以开顶了",
            "{message}\n\n时间：{time}\n此提醒会定时重复，直到在程序里点“已读”或“已开顶”。\n{link}",
        ),
        "lost": ("{site}：天空不再满足开顶条件", "{message}\n\n时间：{time}\n{link}"),
        "test": ("测试邮件", "Sky Monitor 的邮件设置正常。\n{link}"),
        "link": "状态页（“已读”和“已开顶”按钮）：{url}",
    },
}


def email_text(kind: str, status: Mapping[str, object], language: str, url: str = "") -> tuple[str, str]:
    """Subject and body of the mail of ``kind`` ("clear", "lost" or "test")."""

    texts = _EMAIL[language]
    subject, body = texts[kind]
    site = str(status.get("site") or "Sky Monitor")
    link = texts["link"].format(url=url) if url else ""
    when = str(status.get("updatedAt") or "")
    try:
        when = datetime.fromisoformat(when).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        pass
    body = body.format(message=status.get("message", ""), time=when, link=link, site=site)
    return subject.format(site=site), body.strip() + "\n"
