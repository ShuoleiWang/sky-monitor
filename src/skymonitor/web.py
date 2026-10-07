"""The control page: the answer, tonight's forecast, the camera pictures, tonight's marks and the night's course.

Served by the same HTTP server as the Alpaca devices.  Standard library only;
the page is self-contained (no fonts or scripts from the network, the
observatory may be offline) and refreshes itself from ``/api/status``.
"""

from __future__ import annotations

import hmac
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .alpaca import is_loopback
from .config import CONTROL_CODE, ConfigError, set_setting
from .mail import EmailError

if TYPE_CHECKING:
    from .service import Monitor

_PREVIEW = re.compile(r"^/preview/([A-Za-z0-9][A-Za-z0-9_-]{0,39})\.jpg$")
# Wrong control codes from one address before it is locked out, and for how long.
LOCK_AFTER = 5
LOCK_SECONDS = 600.0

_TEXTS = {
    "en": {
        "title": "Sky Monitor",
        "state": {"IMAGING": "May open and image", "OPEN": "May open", "CLOSED": "Keep closed", "IDLE": "Standing by"},
        "coverage": "sky with stars",
        "cloud": "about {cloud}% cloud",
        "noMeasure": "no measurement",
        "sun": "Sun",
        "moon": "Moon",
        "moonSets": "Moon sets",
        "moonRises": "Moon rises",
        "lit": "lit",
        "watched": "Watched",
        "ofMinutes": "of {minutes} min",
        "updated": "Updated",
        "live": "live",
        "stale": "The answer is out of date: treat it as not safe.",
        "noStatus": "No answer yet: the monitor is starting.",
        "opened": "Roof opened",
        "read": "Read",
        "cancel": "Cancel 'roof opened'",
        "testMail": "Send a test mail",
        "tonight": "Tonight",
        "openedSince": "roof marked open at {time}",
        "readDone": "reminder read",
        "notOpened": "not marked open",
        "reminderOn": "reminders are on",
        "cameras": "Cameras",
        "stars": "stars",
        "needed": "needs",
        "fixed": "fixed",
        "night": "The night",
        "hours": "last {hours} h",
        "safeBand": "roof may open",
        "lastMail": "Last mail",
        "mailOk": "Mail sent.",
        "mailFailed": "Mail failed: ",
        "sending": "Sending…",
        "settings": "Settings",
        "restart": "restart the monitor after changing them",
        "lanViewer": "Viewing over the network",
        "codeAsk": "Enter the control code",
        "codeWrong": "Wrong code; {n} tries left",
        "codeLocked": "Too many wrong codes; try again in {minutes} min",
        "localOnly": "The buttons work on the observatory computer only: no control code is set",
        "shareLan": "Computers on this network can open {url} (read-only)",
        "shareLocal": 'Only this computer can open this page; set share = "lan" under [web] in the settings to let the network view it',
        "codeSet": "control code set",
        "codeUnset": "no control code: the buttons work on this computer only",
        "setCode": "Set control code",
        "codeSetTitle": "Control code for other computers",
        "codeSetNote": "4–32 letters or digits; empty to switch the buttons off for other computers",
        "codeSaved": "Control code saved",
        "codeInvalid": "Use 4–32 letters or digits",
        "dismiss": "Cancel",
        "ok": "OK",
        "verdict": {"CLEAR": "Clear", "PARTLY": "Partly cloudy", "CLOUDY": "Cloudy", "UNKNOWN": "No verdict"},
        "fcTitle": "Tonight's forecast",
        "fcSources": "{n} sources · updated {time}",
        "fcRain": "Rain",
        "fcCloud": "Cloud",
        "fcWind": "Wind",
        "fcHumidity": "Humidity",
        "fcSeeing": "Seeing / transp.",
        "fcLow": "low cloud",
        "fcMid": "mid cloud",
        "fcHigh": "high cloud",
        "fcLikely": "rain ≥ 30 %",
        "fcWet": "rain expected",
        "fcGust": "gusts",
        "fcNote": "Bars: the sources' weighted median; lines: each source's own cloud. The weights come from comparing every source with the cameras here, night by night. Seeing and transparency: 7Timer, 1 is best.",
        "fcSource": "Source",
        "fcSkill": "Record here",
        "fcWeight": "Weight",
        "fcState": "State",
        "fcMae": "mean error {mae}%",
        "fcN": "{n} h",
        "fcHit": "clear/cloudy right {hit}%",
        "fcNotScored": "not scored yet",
        "fcFetched": "fetched {time}",
        "fcEmpty": "No forecast for tonight yet.",
        "fcRainChip": "Forecast",
        "fcRainWithin": "rain within {hours} h",
    },
    "zh": {
        "title": "Sky Monitor",
        "state": {"IMAGING": "可以开顶，可以拍摄", "OPEN": "可以开顶", "CLOSED": "暂不开顶", "IDLE": "待命"},
        "coverage": "有星天区",
        "cloud": "云量约 {cloud}%",
        "noMeasure": "暂无测量",
        "sun": "太阳",
        "moon": "月亮",
        "moonSets": "月落",
        "moonRises": "月出",
        "lit": "亮度",
        "watched": "已观察",
        "ofMinutes": "/ {minutes} 分钟",
        "updated": "更新于",
        "live": "实时",
        "stale": "结果已过期：按不安全处理。",
        "noStatus": "还没有判定结果：程序正在启动。",
        "opened": "已开顶",
        "read": "已读",
        "cancel": "取消已开顶",
        "testMail": "发送测试邮件",
        "tonight": "今晚",
        "openedSince": "已于 {time} 标记开顶",
        "readDone": "提醒已读",
        "notOpened": "未标记开顶",
        "reminderOn": "提醒进行中",
        "cameras": "相机",
        "stars": "颗星",
        "needed": "需要",
        "fixed": "固定亮点",
        "night": "今夜走势",
        "hours": "最近 {hours} 小时",
        "safeBand": "可开顶时段",
        "lastMail": "最近邮件",
        "mailOk": "邮件已发送。",
        "mailFailed": "发送失败：",
        "sending": "发送中…",
        "settings": "设置文件",
        "restart": "修改后请重启程序",
        "lanViewer": "局域网只读查看",
        "codeAsk": "请输入控制口令",
        "codeWrong": "口令不对，还可以再试 {n} 次",
        "codeLocked": "口令错误次数太多，请 {minutes} 分钟后再试",
        "localOnly": "未设置控制口令，按钮只能在工控机上使用",
        "shareLan": "同一网络的电脑可以打开 {url} 只读查看",
        "shareLocal": '只有本机能打开此页；在设置文件 [web] 里把 share 改为 "lan"，同一网络的电脑就能只读查看',
        "codeSet": "控制口令已设置",
        "codeUnset": "未设置控制口令：其他电脑上的按钮不可用",
        "setCode": "设置控制口令",
        "codeSetTitle": "其他电脑使用按钮时的口令",
        "codeSetNote": "4–32 位字母或数字；留空则其他电脑上的按钮不可用",
        "codeSaved": "控制口令已保存",
        "codeInvalid": "口令需为 4–32 位字母或数字",
        "dismiss": "取消",
        "ok": "确定",
        "verdict": {"CLEAR": "晴", "PARTLY": "部分有云", "CLOUDY": "多云", "UNKNOWN": "无判定"},
        "fcTitle": "今夜预报",
        "fcSources": "{n} 个来源 · 更新于 {time}",
        "fcRain": "降水",
        "fcCloud": "云量",
        "fcWind": "风速",
        "fcHumidity": "湿度",
        "fcSeeing": "视宁度/透明度",
        "fcLow": "低云",
        "fcMid": "中云",
        "fcHigh": "高云",
        "fcLikely": "降水概率 ≥ 30%",
        "fcWet": "预报有降水",
        "fcGust": "阵风",
        "fcNote": "柱：各来源的加权中位数；线：每个来源自己的云量。权重来自每晚与本站相机实测的对比。视宁度与透明度来自 7Timer，1 最好。",
        "fcSource": "来源",
        "fcSkill": "本站检验",
        "fcWeight": "权重",
        "fcState": "状态",
        "fcMae": "平均误差 {mae}%",
        "fcN": "{n} 小时",
        "fcHit": "晴/云命中 {hit}%",
        "fcNotScored": "尚未评分",
        "fcFetched": "取于 {time}",
        "fcEmpty": "暂无今夜的预报。",
        "fcRainChip": "预报",
        "fcRainWithin": "{hours} 小时内有雨",
    },
}

_PAGE = r"""<!doctype html>
<html lang="__LANG__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>__TITLE__</title>
<style>
  :root {
    --bg: #f5f5f7; --card: rgba(255, 255, 255, 0.82); --card-solid: #ffffff; --line: rgba(0, 0, 0, 0.07);
    --text: #1d1d1f; --muted: #6e6e73; --faint: #a1a1a6;
    --blue: #0071e3; --green: #34c759; --red: #ff3b30; --orange: #ff9f0a; --gray: #8e8e93;
    --shadow: 0 10px 40px rgba(0, 0, 0, 0.07), 0 1px 2px rgba(0, 0, 0, 0.04);
    --tint: transparent;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #000000; --card: rgba(28, 28, 30, 0.86); --card-solid: #1c1c1e; --line: rgba(255, 255, 255, 0.09);
      --text: #f5f5f7; --muted: #98989d; --faint: #636366;
      --blue: #0a84ff; --green: #30d158; --red: #ff453a; --orange: #ffa033; --gray: #8e8e93;
      --shadow: 0 10px 40px rgba(0, 0, 0, 0.5), 0 1px 2px rgba(0, 0, 0, 0.4);
    }
  }
  * { box-sizing: border-box; }
  [hidden] { display: none !important; }
  html { -webkit-text-size-adjust: 100%; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", "PingFang SC", "Helvetica Neue",
      "Hiragino Sans GB", "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 15px; line-height: 1.45; -webkit-font-smoothing: antialiased;
  }
  header {
    position: sticky; top: 0; z-index: 10; backdrop-filter: saturate(180%) blur(20px); -webkit-backdrop-filter: saturate(180%) blur(20px);
    background: color-mix(in srgb, var(--bg) 72%, transparent); border-bottom: 1px solid var(--line);
  }
  .bar { max-width: 1080px; margin: 0 auto; padding: 14px 24px; display: flex; align-items: center; gap: 14px; }
  .bar h1 { margin: 0; font-size: 17px; font-weight: 600; letter-spacing: -0.01em; }
  .bar .site { color: var(--muted); font-size: 15px; }
  .bar .spacer { flex: 1; }
  .live { display: inline-flex; align-items: center; gap: 8px; color: var(--muted); font-size: 13px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--gray); }
  .dot.on { background: var(--green); box-shadow: 0 0 0 0 rgba(52, 199, 89, 0.5); animation: pulse 2.4s infinite; }
  @keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(52, 199, 89, 0.45); } 70% { box-shadow: 0 0 0 9px rgba(52, 199, 89, 0); } 100% { box-shadow: 0 0 0 0 rgba(52, 199, 89, 0); } }
  main { max-width: 1080px; margin: 0 auto; padding: 22px 24px 48px; }
  .card { background: var(--card); backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px); border: 1px solid var(--line);
    border-radius: 22px; box-shadow: var(--shadow); }
  .hero { padding: 28px 30px; display: grid; grid-template-columns: 180px 1fr; gap: 28px; align-items: center;
    background: linear-gradient(135deg, var(--tint), transparent 60%), var(--card); transition: background 0.6s ease; }
  .gauge { position: relative; width: 180px; height: 180px; }
  .gauge svg { width: 100%; height: 100%; transform: rotate(-90deg); }
  .gauge .track { fill: none; stroke: var(--line); stroke-width: 12; }
  .gauge .arc { fill: none; stroke: var(--gray); stroke-width: 12; stroke-linecap: round; transition: stroke-dashoffset 0.9s cubic-bezier(.2,.8,.2,1), stroke 0.6s ease; }
  .gauge .label { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; }
  .gauge .value { font-size: 40px; font-weight: 600; letter-spacing: -0.03em; line-height: 1; }
  .gauge .value small { font-size: 18px; font-weight: 500; color: var(--muted); margin-left: 2px; }
  .gauge .what { color: var(--muted); font-size: 12px; line-height: 1.35; margin-top: 6px; max-width: 118px; text-align: center; white-space: pre-line; }
  .headline { font-size: 34px; font-weight: 700; letter-spacing: -0.025em; line-height: 1.15; margin: 0; }
  .headline.IMAGING, .headline.OPEN { color: var(--green); }
  .headline.CLOSED { color: var(--red); }
  .headline.IDLE { color: var(--muted); }
  .message { margin: 10px 0 0; font-size: 17px; color: var(--text); }
  .chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
  .chip { display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; border-radius: 999px; background: color-mix(in srgb, var(--text) 6%, transparent);
    color: var(--muted); font-size: 13px; font-weight: 500; }
  .chip b { color: var(--text); font-weight: 600; }
  .actions { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin: 18px 0 8px; }
  button { font: inherit; font-weight: 600; font-size: 15px; padding: 11px 20px; border-radius: 999px; border: 0; cursor: pointer;
    transition: transform 0.08s ease, filter 0.15s ease, opacity 0.2s ease; color: white; background: var(--blue); }
  button:hover { filter: brightness(1.08); }
  button:active { transform: scale(0.97); }
  button:disabled { opacity: 0.35; cursor: default; filter: none; transform: none; }
  button.tinted { background: color-mix(in srgb, var(--blue) 14%, transparent); color: var(--blue); }
  button.plain { background: color-mix(in srgb, var(--text) 7%, transparent); color: var(--text); }
  .tonight { color: var(--muted); font-size: 14px; }
  .tonight b { color: var(--text); font-weight: 600; }
  h2 { font-size: 20px; font-weight: 700; letter-spacing: -0.02em; margin: 30px 0 12px; }
  h2 small { color: var(--muted); font-weight: 500; font-size: 14px; margin-left: 10px; }
  .cameras { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; }
  .camera { overflow: hidden; }
  .camera .shot { position: relative; aspect-ratio: 16 / 9; background: #0b0b0d; }
  .camera img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .camera .badge { position: absolute; top: 12px; right: 12px; padding: 5px 11px; border-radius: 999px; font-size: 12px; font-weight: 600;
    color: white; background: rgba(0, 0, 0, 0.45); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); }
  .camera .badge.CLEAR { background: rgba(52, 199, 89, 0.85); }
  .camera .badge.PARTLY { background: rgba(255, 159, 10, 0.9); }
  .camera .badge.CLOUDY { background: rgba(255, 59, 48, 0.85); }
  .camera .facts { padding: 12px 16px 14px; }
  .camera .name { font-weight: 600; }
  .camera .numbers { color: var(--muted); font-size: 13px; margin-top: 3px; }
  .chart { padding: 18px 22px 12px; }
  .chart svg { width: 100%; height: 150px; display: block; }
  .chart .axis { fill: var(--muted); font-size: 11px; }
  .chart .legend { display: flex; gap: 16px; color: var(--muted); font-size: 12px; margin-top: 6px; }
  .legend i { display: inline-block; width: 10px; height: 10px; border-radius: 3px; margin-right: 6px; vertical-align: -1px; }
  table { border-collapse: collapse; font-size: 14px; margin-top: 4px; }
  td { padding: 5px 16px 5px 0; vertical-align: top; }
  td:first-child { color: var(--muted); white-space: nowrap; }
  .chip.rain { background: color-mix(in srgb, var(--blue) 14%, transparent); color: var(--blue); }
  .chip.rain b { color: var(--blue); }
  .forecast { padding: 18px 22px 16px; }
  .fc-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; padding-bottom: 2px; }
  .fc-grid { display: grid; align-items: center; }
  .fc-grid .lab { color: var(--muted); font-size: 12px; white-space: nowrap; padding-right: 10px; }
  .fc-grid .cell { text-align: center; font-size: 12.5px; font-variant-numeric: tabular-nums; padding: 3px 0; white-space: nowrap; }
  .fc-grid .hour { color: var(--muted); font-size: 12px; padding-top: 6px; }
  .fc-grid .dim { color: var(--faint); }
  .fc-grid .gap, .fc-col.gap { box-shadow: inset 1px 0 0 var(--line); }
  .fc-grid .fc-rain { height: 30px; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; gap: 3px;
    color: var(--blue); font-size: 10.5px; font-weight: 600; }
  .drop { width: 7px; height: 7px; border: 1.5px solid var(--blue); border-radius: 0 50% 50% 50%; transform: rotate(45deg); }
  .drop.wet { background: var(--blue); }
  .fc-plot { position: relative; height: 104px; display: grid; border-bottom: 1px solid var(--line); }
  .fc-col { position: relative; height: 100%; }
  .fc-col::before, .fc-bar { content: ""; position: absolute; left: 24%; right: 24%; bottom: 0; border-radius: 5px; }
  .fc-col::before { top: 0; background: color-mix(in srgb, var(--text) 4%, transparent); }
  .fc-bar { display: flex; flex-direction: column-reverse; overflow: hidden; }
  .fc-bar i { display: block; min-height: 1px; }
  .fc-bar .total { background: color-mix(in srgb, var(--text) 34%, transparent); }
  .fc-bar .low, .legend .low { background: color-mix(in srgb, var(--text) 52%, transparent); }
  .fc-bar .mid, .legend .mid { background: color-mix(in srgb, var(--text) 32%, transparent); }
  .fc-bar .high, .legend .high { background: color-mix(in srgb, var(--text) 17%, transparent); }
  .fc-plot svg { position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; pointer-events: none; }
  .fc-legend { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 16px; color: var(--muted); font-size: 12px; margin-top: 12px; }
  .fc-legend .drop { display: inline-block; margin-right: 7px; }
  .fc-note { margin-top: 6px; }
  .fc-table-wrap { overflow-x: auto; margin-top: 14px; border-top: 1px solid var(--line); }
  .fc-table { width: 100%; }
  .fc-table th { text-align: left; color: var(--muted); font-weight: 500; font-size: 12px; padding: 10px 14px 4px 0; white-space: nowrap; }
  .fc-table td { padding: 6px 14px 6px 0; font-size: 13.5px; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .fc-table td:last-child { white-space: normal; min-width: 180px; }
  .fc-table td:first-child { color: var(--text); }
  .fc-table .muted { color: var(--muted); }
  .fc-table .error { color: var(--red); }
  .fc-swatch { display: inline-block; width: 14px; height: 3px; border-radius: 2px; margin-right: 8px; vertical-align: 3px; background: var(--faint); }
  footer { margin-top: 28px; color: var(--faint); font-size: 12.5px; }
  .note { color: var(--muted); font-size: 13px; }
  .viewer { display: inline-flex; padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 600;
    color: var(--blue); background: color-mix(in srgb, var(--blue) 12%, transparent); }
  .sharing { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-top: 10px; color: var(--muted); font-size: 13px; }
  .sharing button { padding: 6px 14px; font-size: 13px; }
  .backdrop { position: fixed; inset: 0; z-index: 50; display: flex; align-items: center; justify-content: center;
    background: rgba(0, 0, 0, 0.32); backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px); }
  .sheet { width: min(360px, calc(100vw - 32px)); padding: 22px 22px 18px; background: var(--card-solid); }
  .sheet h3 { margin: 0 0 6px; font-size: 17px; font-weight: 600; }
  .sheet p { margin: 0 0 14px; color: var(--muted); font-size: 13px; min-height: 1em; }
  .sheet p.error { color: var(--red); }
  .sheet input { width: 100%; font: inherit; font-size: 17px; padding: 11px 14px; border-radius: 12px; border: 1px solid var(--line);
    background: color-mix(in srgb, var(--text) 5%, transparent); color: var(--text); outline: none; letter-spacing: 0.08em; }
  .sheet input:focus { border-color: var(--blue); box-shadow: 0 0 0 3px color-mix(in srgb, var(--blue) 25%, transparent); }
  .sheet .buttons { display: flex; justify-content: flex-end; gap: 10px; margin-top: 16px; }
  @media (max-width: 640px) {
    .hero { grid-template-columns: 1fr; justify-items: center; text-align: center; padding: 24px 20px; }
    .chips { justify-content: center; }
    .headline { font-size: 28px; }
    main { padding: 16px 16px 40px; }
  }
</style>
</head>
<body>
<header><div class="bar">
  <h1>__TITLE__</h1><span class="site" id="site"></span>
  <span class="spacer"></span>
  <span class="viewer" id="viewer" hidden></span>
  <span class="live"><span class="dot" id="dot"></span><span id="liveText"></span></span>
</div></header>
<main>
  <section class="card hero" id="hero">
    <div class="gauge">
      <svg viewBox="0 0 160 160"><circle class="track" cx="80" cy="80" r="68"></circle><circle class="arc" id="arc" cx="80" cy="80" r="68"></circle></svg>
      <div class="label"><div class="value"><span id="pct">–</span><small>%</small></div><div class="what" id="what"></div></div>
    </div>
    <div>
      <h2 class="headline IDLE" id="headline" style="margin:0"></h2>
      <p class="message" id="message"></p>
      <div class="chips" id="chips"></div>
      <div class="actions">
        <button id="open"></button>
        <button id="read" class="tinted"></button>
        <button id="cancel" class="plain"></button>
        <button id="mail" class="plain"></button>
      </div>
      <div class="tonight" id="tonight"></div>
      <div class="note" id="mailResult"></div>
      <div class="sharing" id="sharing" hidden><span id="sharingText"></span><button class="plain" id="setCode"></button></div>
    </div>
  </section>

  <div id="forecastBlock" hidden>
    <h2 id="forecastTitle"></h2>
    <section class="card forecast">
      <div class="fc-scroll"><div class="fc-grid" id="fcGrid"></div></div>
      <div class="note" id="fcEmpty"></div>
      <div class="legend fc-legend" id="fcLegend"></div>
      <div class="note fc-note" id="fcNote"></div>
      <div class="fc-table-wrap"><table class="fc-table" id="fcProviders"></table></div>
    </section>
  </div>

  <h2 id="camerasTitle"></h2>
  <div class="cameras" id="cameras"></div>

  <h2 id="nightTitle"></h2>
  <section class="card chart">
    <svg id="chart" viewBox="0 0 1000 150" preserveAspectRatio="none"></svg>
    <div class="legend" id="legend"></div>
  </section>

  <table id="facts"></table>
  <footer id="footer"></footer>
</main>
<div class="backdrop" id="backdrop" hidden>
  <div class="card sheet" role="dialog" aria-modal="true">
    <h3 id="sheetTitle"></h3>
    <p id="sheetNote"></p>
    <input id="sheetInput" type="password" autocomplete="off" maxlength="32">
    <div class="buttons"><button class="plain" id="sheetCancel"></button><button id="sheetOk"></button></div>
  </div>
</div>
<script>
const T = __TEXTS__;
const INFO = __INFO__;
const CIRCUMFERENCE = 2 * Math.PI * 68;
const byId = (id) => document.getElementById(id);
const setText = (id, value) => { byId(id).textContent = value; };
function stateOf(status) {
  if (!status) return "IDLE";
  if (status.imagingOk) return "IMAGING";
  if (status.safe) return "OPEN";
  if (["DAYLIGHT", "ROOF_OPENED", "STOPPED"].includes(status.reason)) return "IDLE";
  return "CLOSED";
}
const COLOURS = {IMAGING: "var(--green)", OPEN: "var(--green)", CLOSED: "var(--red)", IDLE: "var(--gray)"};
const TINTS = {IMAGING: "rgba(52,199,89,0.22)", OPEN: "rgba(52,199,89,0.22)", CLOSED: "rgba(255,59,48,0.18)", IDLE: "rgba(142,142,147,0.16)"};
function clockTime(iso) { return new Date(iso).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}); }
function chip(label, value) {
  const span = document.createElement("span");
  span.className = "chip";
  span.append(label + " ");
  const b = document.createElement("b");
  b.textContent = value;
  span.append(b);
  return span;
}
let viewer = {local: true, share: "local", codeSet: false, lanUrl: null};
function render(data) {
  const status = data.status;
  const night = data.night || {};
  viewer = data.viewer || viewer;
  const state = stateOf(status);
  const stale = !!(status && status.validUntil && new Date(status.validUntil) < new Date());
  setText("site", INFO.site);
  byId("dot").className = "dot" + (status && !stale ? " on" : "");
  setText("liveText", status ? (stale ? T.stale : T.live + " · " + T.updated + " " + clockTime(status.updatedAt)) : "");

  const hero = byId("hero");
  hero.style.setProperty("--tint", TINTS[stale ? "CLOSED" : state]);
  const headline = byId("headline");
  headline.className = "headline " + (stale ? "CLOSED" : state);
  headline.textContent = stale ? T.stale : T.state[state];
  setText("message", status ? status.message : T.noStatus);

  const coverage = status && status.coverage !== null && status.coverage !== undefined ? status.coverage : null;
  const arc = byId("arc");
  arc.style.strokeDasharray = CIRCUMFERENCE;
  arc.style.strokeDashoffset = CIRCUMFERENCE * (1 - (coverage === null ? 0 : coverage));
  arc.style.stroke = coverage === null ? "var(--gray)" : (coverage >= (status.openCoverage || 0.7) ? "var(--green)" : (coverage >= 0.4 ? "var(--orange)" : "var(--red)"));
  setText("pct", coverage === null ? "–" : Math.round(coverage * 100));
  setText("what", coverage === null ? T.noMeasure : T.coverage + "\n" + T.cloud.replace("{cloud}", Math.round((1 - coverage) * 100)));

  const chips = byId("chips");
  chips.innerHTML = "";
  if (status && status.sun) chips.append(chip(T.sun, status.sun.altitudeDeg.toFixed(1) + "°"));
  if (status && status.moon) chips.append(chip(T.moon, status.moon.altitudeDeg.toFixed(1) + "° · " + Math.round(status.moon.illumination * 100) + "% " + T.lit));
  if (status && status.moon && status.moon.altitudeDeg >= 0 && status.moon.setsAt) chips.append(chip(T.moonSets, clockTime(status.moon.setsAt)));
  if (status && status.moon && status.moon.altitudeDeg < 0 && status.moon.risesAt) chips.append(chip(T.moonRises, clockTime(status.moon.risesAt)));
  if (status && status.heldSeconds > 0) chips.append(chip(T.watched, Math.floor(status.heldSeconds / 60) + " " + T.ofMinutes.replace("{minutes}", Math.round(status.openAfterSeconds / 60))));
  const forecast = status && status.forecast;
  if (forecast && forecast.rainSoon) {
    const rain = chip(T.fcRainChip, T.fcRainWithin.replace("{hours}", forecast.rainWindowHours));
    rain.classList.add("rain");
    chips.append(rain);
  }
  renderForecast(forecast || null);

  byId("open").textContent = T.opened;
  byId("read").textContent = T.read;
  byId("cancel").textContent = T.cancel;
  byId("mail").textContent = T.testMail;
  byId("open").disabled = !!night.roofOpened;
  byId("read").disabled = !!night.roofOpened || !night.notifiedAt || !!night.acknowledged;
  byId("cancel").disabled = !night.roofOpened;
  byId("mail").disabled = !INFO.emailEnabled;
  const remoteLocked = !viewer.local && !viewer.codeSet;
  for (const id of ["open", "read", "cancel", "mail"]) {
    if (remoteLocked) byId(id).disabled = true;
    byId(id).title = remoteLocked ? T.localOnly : "";
  }
  byId("viewer").hidden = viewer.local;
  setText("viewer", T.lanViewer);
  byId("sharing").hidden = !viewer.local;
  setText("setCode", T.setCode);
  byId("setCode").hidden = viewer.share !== "lan";
  setText("sharingText", viewer.share === "lan"
    ? T.shareLan.replace("{url}", viewer.lanUrl || "http://<IP>:" + location.port + "/") + " · " + (viewer.codeSet ? T.codeSet : T.codeUnset)
    : T.shareLocal);
  const parts = [];
  if (night.roofOpened) parts.push(T.openedSince.replace("{time}", night.openedAt ? clockTime(night.openedAt) : "?"));
  else parts.push(T.notOpened);
  if (night.notifiedAt && !night.roofOpened) parts.push(night.acknowledged ? T.readDone : T.reminderOn);
  byId("tonight").innerHTML = "<b>" + T.tonight + "</b> · " + parts.map((p) => p.replace(/</g, "&lt;")).join(" · ");

  setText("camerasTitle", T.cameras);
  const cameras = byId("cameras");
  cameras.innerHTML = "";
  for (const camera of (status && status.cameras) || []) {
    const card = document.createElement("div");
    card.className = "card camera";
    const shot = document.createElement("div");
    shot.className = "shot";
    const picture = document.createElement("img");
    picture.src = "/preview/" + camera.name + ".jpg?t=" + Math.floor(Date.now() / 10000);
    picture.alt = camera.name;
    picture.onerror = () => { picture.style.display = "none"; };
    const badge = document.createElement("span");
    badge.className = "badge " + camera.verdict;
    badge.textContent = (T.verdict[camera.verdict] || camera.verdict) + (camera.reason && camera.verdict === "UNKNOWN" ? " · " + camera.reason : "");
    shot.append(picture, badge);
    const facts = document.createElement("div");
    facts.className = "facts";
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = camera.name;
    const numbers = document.createElement("div");
    numbers.className = "numbers";
    if (camera.coverage !== null && camera.coverage !== undefined) {
      numbers.textContent = T.coverage + " " + Math.round(camera.coverage * 100) + "% · " + camera.starCount + " " + T.stars
        + " (" + T.needed + " " + camera.requiredStars + ") · " + T.fixed + " " + camera.fixedSources;
    } else {
      numbers.textContent = camera.detail || camera.reason || "";
    }
    facts.append(name, numbers);
    card.append(shot, facts);
    cameras.append(card);
  }

  const rows = [];
  const mail = data.lastEmail;
  if (mail) rows.push([T.lastMail, new Date(mail.at).toLocaleString() + " · " + mail.subject + (mail.ok ? "" : " · " + T.mailFailed + mail.error)]);
  const table = byId("facts");
  table.innerHTML = "";
  for (const [key, value] of rows) {
    const row = table.insertRow();
    row.insertCell().textContent = key;
    row.insertCell().textContent = value;
  }
  setText("footer", (viewer.local && INFO.configPath ? T.settings + ": " + INFO.configPath + " · " + T.restart + " · " : "") + "Sky Monitor " + INFO.version);
}
const SOURCE_COLOURS = ["#0a84ff", "#ff9f0a", "#bf5af2", "#30b0c7", "#ff375f", "#5e5ce6", "#34c759", "#ac8e68"];
function esc(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}
const known = (value) => value !== null && value !== undefined;
const pct = (value) => Math.round(value * 100);
const speed = (value) => (value < 10 ? value.toFixed(1) : String(Math.round(value)));
function hourLabel(iso) { return String(new Date(iso).getHours()).padStart(2, "0"); }
function rainMark(hour) {
  const wet = known(hour.rainMm) && hour.rainMm > 0;
  const likely = known(hour.rainProbability) && hour.rainProbability >= 0.3;
  if (!wet && !likely) return "";
  const label = known(hour.rainProbability) ? pct(hour.rainProbability) + "%" : hour.rainMm.toFixed(1);
  return `<span class="drop${wet ? " wet" : ""}"></span><span>${label}</span>`;
}
function cloudBar(hour) {
  const layers = [["low", hour.cloudLow], ["mid", hour.cloudMid], ["high", hour.cloudHigh]].filter(([, v]) => known(v) && v > 0);
  const total = known(hour.cloud) ? hour.cloud : (layers.length ? Math.min(1, Math.max(...layers.map(([, v]) => v))) : null);
  if (!known(total) || total <= 0) return "";
  // The layers overlap, so they share the total cloud in proportion; flex shares must sum to 1 to fill the bar.
  const sum = layers.reduce((acc, [, v]) => acc + v, 0);
  const parts = layers.length ? layers.map(([name, v]) => `<i class="${name}" style="flex:${(v / sum).toFixed(4)}"></i>`).join("") : `<i class="total" style="flex:1"></i>`;
  return `<div class="fc-bar" style="height:${(total * 100).toFixed(1)}%">${parts}</div>`;
}
function hourTitle(hour) {
  const parts = [new Date(hour.t).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})];
  if (known(hour.cloud)) {
    const layers = [[T.fcLow, hour.cloudLow], [T.fcMid, hour.cloudMid], [T.fcHigh, hour.cloudHigh]].filter(([, v]) => known(v));
    parts.push(T.fcCloud + " " + pct(hour.cloud) + "%" + (layers.length ? " (" + layers.map(([n, v]) => n + " " + pct(v) + "%").join(", ") + ")" : ""));
  }
  if (known(hour.rainProbability) || known(hour.rainMm)) {
    parts.push(T.fcRain + " " + [known(hour.rainProbability) ? pct(hour.rainProbability) + "%" : null, known(hour.rainMm) ? hour.rainMm.toFixed(1) + " mm" : null].filter(known).join(" · "));
  }
  if (known(hour.windMs)) parts.push(T.fcWind + " " + speed(hour.windMs) + " m/s" + (known(hour.gustMs) ? " (" + T.fcGust + " " + speed(hour.gustMs) + " m/s)" : ""));
  if (known(hour.humidity)) parts.push(T.fcHumidity + " " + pct(hour.humidity) + "%");
  return parts.join("\n");
}
function sourceLine(night, gap, key, colour) {
  const at = (i) => night[i] && (night[i].sources || {})[key];
  let d = "";
  night.forEach((hour, i) => {
    const value = at(i);
    if (!known(value)) return;
    const x = i + 0.5, y = (100 - value * 100).toFixed(1);
    // A line never bridges the daylight between two nights.
    const before = !gap[i] && known(at(i - 1)), after = !gap[i + 1] && known(at(i + 1));
    // A lone hour still shows, as a short dash.
    if (!before && !after) d += `M${(x - 0.3).toFixed(2)} ${y}L${(x + 0.3).toFixed(2)} ${y}`;
    else d += (before ? "L" : "M") + x.toFixed(2) + " " + y;
  });
  return d ? `<path d="${d}" fill="none" stroke="${colour}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke" opacity="0.9"/>` : "";
}
function renderForecast(forecast) {
  const block = byId("forecastBlock");
  if (!forecast) { block.hidden = true; return; }
  block.hidden = false;
  const night = forecast.night || [];
  const providers = forecast.providers || [];
  const keys = [...new Set([...providers.filter((p) => p.model).map((p) => p.key), ...night.flatMap((h) => Object.keys(h.sources || {}))])];
  const colour = {};
  keys.forEach((key, i) => { colour[key] = SOURCE_COLOURS[i % SOURCE_COLOURS.length]; });
  setText("forecastTitle", T.fcTitle);
  const answered = providers.filter((p) => p.model).length;
  byId("forecastTitle").insertAdjacentHTML("beforeend", "<small>" + esc(T.fcSources.replace("{n}", answered).replace("{time}", forecast.refreshedAt ? clockTime(forecast.refreshedAt) : "–")) + "</small>");

  const n = night.length;
  const gap = night.map((hour, i) => i > 0 && new Date(hour.t) - new Date(night[i - 1].t) > 3600 * 1000);
  const grid = byId("fcGrid");
  grid.style.gridTemplateColumns = `max-content repeat(${n}, minmax(32px, 1fr))`;
  grid.style.minWidth = `calc(${n} * 32px + 96px)`;
  let html = "";
  if (n) {
    const row = (label, cells, kind) => `<div class="lab">${esc(label)}</div>`
      + cells.map((c, i) => `<div class="cell ${kind || ""}${gap[i] ? " gap" : ""}">${c}</div>`).join("");
    html += row(T.fcRain, night.map(rainMark), "fc-rain");
    html += `<div class="lab">${esc(T.fcCloud)}</div><div class="fc-plot" style="grid-column: span ${n}; grid-template-columns: repeat(${n}, 1fr)">`;
    html += night.map((hour, i) => `<div class="fc-col${gap[i] ? " gap" : ""}" title="${esc(hourTitle(hour))}">${cloudBar(hour)}</div>`).join("");
    html += `<svg viewBox="0 0 ${n} 100" preserveAspectRatio="none">${keys.map((key) => sourceLine(night, gap, key, colour[key])).join("")}</svg></div>`;
    html += row("", night.map((hour) => hourLabel(hour.t)), "hour");
    const value = (v, text) => (known(v) ? text(v) : `<span class="dim">–</span>`);
    html += row(T.fcWind + " m/s", night.map((hour) => value(hour.windMs, speed)));
    html += row(T.fcHumidity + " %", night.map((hour) => value(hour.humidity, pct)));
    if (night.some((hour) => known(hour.seeing) || known(hour.transparency))) {
      html += row(T.fcSeeing, night.map((hour) => (known(hour.seeing) || known(hour.transparency))
        ? (known(hour.seeing) ? hour.seeing : "–") + "/" + (known(hour.transparency) ? hour.transparency : "–") : `<span class="dim">–</span>`));
    }
  }
  grid.innerHTML = html;
  setText("fcEmpty", n ? "" : T.fcEmpty);
  const layered = night.some((hour) => known(hour.cloudLow) || known(hour.cloudMid) || known(hour.cloudHigh));
  const swatches = layered ? [["low", T.fcLow], ["mid", T.fcMid], ["high", T.fcHigh]] : [["low", T.fcCloud]];
  byId("fcLegend").innerHTML = swatches.map(([cls, label]) => `<span><i class="${cls}"></i>${esc(label)}</span>`).join("")
    + `<span><span class="drop"></span>${esc(T.fcLikely)}</span><span><span class="drop wet"></span>${esc(T.fcWet)}</span>`;
  setText("fcNote", n ? T.fcNote : "");

  let rows = `<tr><th>${esc(T.fcSource)}</th><th>${esc(T.fcSkill)}</th><th>${esc(T.fcWeight)}</th><th>${esc(T.fcState)}</th></tr>`;
  for (const p of providers) {
    const swatch = `<i class="fc-swatch"${colour[p.key] ? ` style="background:${colour[p.key]}"` : ""}></i>`;
    const skill = p.skill
      ? esc([T.fcMae.replace("{mae}", pct(p.skill.mae)), T.fcN.replace("{n}", p.skill.n), T.fcHit.replace("{hit}", pct(p.skill.hitRate))].join(" · "))
      : `<span class="muted">${esc(T.fcNotScored)}</span>`;
    const weight = known(p.weight) ? p.weight.toFixed(2) : `<span class="muted">–</span>`;
    const state = [p.error ? `<span class="error">${esc(p.error)}</span>` : "", p.fetchedAt ? `<span class="muted">${esc(T.fcFetched.replace("{time}", clockTime(p.fetchedAt)))}</span>` : ""].filter(Boolean).join("<br>");
    rows += `<tr><td>${swatch}${esc(p.key)}</td><td>${skill}</td><td>${weight}</td><td>${state}</td></tr>`;
  }
  byId("fcProviders").innerHTML = providers.length ? rows : "";
}
function renderHistory(data) {
  setText("nightTitle", T.night);
  byId("nightTitle").insertAdjacentHTML("beforeend", "<small>" + T.hours.replace("{hours}", data.hours) + "</small>");
  const svg = byId("chart");
  const points = data.points || [];
  const width = 1000, height = 150, top = 12, bottom = 128;
  const now = Date.now(), start = now - data.hours * 3600 * 1000;
  const x = (t) => ((new Date(t).getTime() - start) / (now - start)) * width;
  const y = (c) => bottom - (bottom - top) * c;
  let safeBands = "", area = "", line = "";
  let bandStart = null;
  for (let i = 0; i < points.length; i++) {
    const p = points[i];
    const px = x(p.t);
    if (p.safe && bandStart === null) bandStart = px;
    if ((!p.safe || i === points.length - 1) && bandStart !== null) {
      const end = p.safe ? px : px;
      safeBands += `<rect x="${bandStart.toFixed(1)}" y="${top}" width="${Math.max(end - bandStart, 2).toFixed(1)}" height="${bottom - top}" fill="rgba(52,199,89,0.16)"/>`;
      bandStart = null;
    }
    if (p.coverage !== null && p.coverage !== undefined) {
      const py = y(p.coverage);
      line += (line ? " L" : "M") + px.toFixed(1) + " " + py.toFixed(1);
    }
  }
  if (line) {
    const first = line.indexOf("M") + 1, firstX = line.slice(first).split(" ")[0];
    const lastX = line.trim().split(" ").slice(-2)[0];
    area = `<path d="${line} L${lastX} ${bottom} L${firstX} ${bottom} Z" fill="rgba(0,113,227,0.14)"/><path d="${line}" fill="none" stroke="var(--blue)" stroke-width="2" stroke-linejoin="round"/>`;
  }
  let axis = "";
  const hoursStep = data.hours > 8 ? 2 : 1;
  for (let h = 0; h <= data.hours; h += hoursStep) {
    const t = new Date(start + h * 3600 * 1000);
    const px = (h / data.hours) * width;
    axis += `<text class="axis" x="${px.toFixed(1)}" y="${height - 4}" text-anchor="${h === 0 ? "start" : h === data.hours ? "end" : "middle"}">${t.getHours().toString().padStart(2, "0")}:00</text>`;
  }
  const grid = [0.4, 0.7].map((c) => `<line x1="0" x2="${width}" y1="${y(c)}" y2="${y(c)}" stroke="var(--line)" stroke-dasharray="4 6"/>`).join("");
  svg.innerHTML = safeBands + grid + area + axis;
  byId("legend").innerHTML = `<span><i style="background: var(--blue)"></i>${T.coverage}</span><span><i style="background: rgba(52,199,89,0.5)"></i>${T.safeBand}</span>`;
}
async function refresh() {
  try {
    const response = await fetch("/api/status", {cache: "no-store"});
    render(await response.json());
  } catch (error) {
    setText("message", String(error));
  }
}
async function history() {
  try {
    const response = await fetch("/api/history?hours=12", {cache: "no-store"});
    renderHistory(await response.json());
  } catch (error) { /* the page still works without the chart */ }
}
function storedCode() { try { return sessionStorage.getItem("skyMonitorCode") || ""; } catch (error) { return ""; } }
function storeCode(code) {
  try { if (code) sessionStorage.setItem("skyMonitorCode", code); else sessionStorage.removeItem("skyMonitorCode"); } catch (error) { /* private mode */ }
}
function askCode(title, note, isError) {
  return new Promise((resolve) => {
    const input = byId("sheetInput");
    setText("sheetTitle", title);
    setText("sheetNote", note || "");
    byId("sheetNote").className = isError ? "error" : "";
    setText("sheetCancel", T.dismiss);
    setText("sheetOk", T.ok);
    input.value = "";
    byId("backdrop").hidden = false;
    input.focus();
    const finish = (value) => {
      byId("backdrop").hidden = true;
      byId("sheetOk").onclick = byId("sheetCancel").onclick = input.onkeydown = null;
      resolve(value);
    };
    byId("sheetOk").onclick = () => finish(input.value.trim());
    byId("sheetCancel").onclick = () => finish(null);
    input.onkeydown = (event) => {
      if (event.key === "Enter") finish(input.value.trim());
      if (event.key === "Escape") finish(null);
    };
  });
}
async function send(path, payload) {
  let code = viewer.local ? "" : storedCode();
  if (!viewer.local && !code) {
    code = await askCode(T.codeAsk);
    if (!code) return null;
  }
  for (;;) {
    const headers = {"Content-Type": "application/json", "X-Sky-Monitor": "1"};
    if (code) headers["X-Control-Code"] = code;
    const response = await fetch(path, {method: "POST", headers, body: JSON.stringify(payload || {})});
    let body = {};
    try { body = await response.json(); } catch (error) { /* no body */ }
    if (response.status === 403 && body.error === "bad-code") {
      storeCode("");
      code = await askCode(T.codeAsk, T.codeWrong.replace("{n}", body.remaining), true);
      if (!code) return null;
      continue;
    }
    if (response.status === 403 && body.error === "local-only") { setText("mailResult", T.localOnly); return null; }
    if (response.status === 429) {
      setText("mailResult", T.codeLocked.replace("{minutes}", Math.ceil((body.retryAfter || 600) / 60)));
      return null;
    }
    if (response.ok && code) storeCode(code);
    return response.ok ? body : null;
  }
}
async function post(path, payload) {
  await send(path, payload);
  refresh();
}
byId("open").onclick = () => post("/api/roof", {opened: true});
byId("cancel").onclick = () => post("/api/roof", {opened: false});
byId("read").onclick = () => post("/api/ack", {acknowledged: true});
byId("mail").onclick = async () => {
  setText("mailResult", T.sending);
  const result = await send("/api/email/test");
  if (result) setText("mailResult", result.ok ? T.mailOk : T.mailFailed + result.error);
};
byId("setCode").onclick = async () => {
  const code = await askCode(T.codeSetTitle, T.codeSetNote);
  if (code === null) return;
  if (code && !/^[A-Za-z0-9]{4,32}$/.test(code)) { setText("mailResult", T.codeInvalid); return; }
  const result = await send("/api/control-code", {code});
  setText("mailResult", result ? T.codeSaved : T.codeInvalid);
  refresh();
};
refresh();
history();
setInterval(refresh, 10000);
setInterval(history, 60000);
</script>
</body>
</html>
"""


class ControlPanel:
    def __init__(
        self,
        monitor: Monitor,
        *,
        language: str,
        config_path: Path,
        preview_dir: Path,
        site: str,
        email_enabled: bool,
        share: str = "local",
        control_code: str = "",
        lan_url: str = "",
    ) -> None:
        self._monitor = monitor
        self._language = language if language in _TEXTS else "en"
        self._preview_dir = Path(preview_dir)
        self._config_path = Path(config_path)
        self._share = share
        self._code = control_code
        self._lan_url = lan_url
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

        def page(config_shown: str) -> bytes:
            info = {"site": site, "configPath": config_shown, "emailEnabled": email_enabled, "version": __version__}
            return (
                _PAGE.replace("__LANG__", self._language)
                .replace("__TITLE__", _TEXTS[self._language]["title"])
                .replace("__TEXTS__", json.dumps(_TEXTS[self._language], ensure_ascii=False))
                .replace("__INFO__", json.dumps(info, ensure_ascii=False))
            ).encode("utf-8")

        # Other computers are not shown where the settings live.
        self._page = page(str(config_path))
        self._remote_page = page("")

    @staticmethod
    def _send(request: BaseHTTPRequestHandler, status: int, kind: str, body: bytes) -> None:
        request.send_response(status)
        request.send_header("Content-Type", kind)
        request.send_header("Content-Length", str(len(body)))
        request.send_header("Cache-Control", "no-store")
        request.end_headers()
        request.wfile.write(body)

    def _json(self, request: BaseHTTPRequestHandler, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(request, status, "application/json; charset=utf-8", body)

    def _state(self, client: str) -> dict[str, object]:
        local = is_loopback(client)
        with self._lock:
            code_set = bool(self._code)
        return {
            "status": self._monitor.latest_status(),
            "night": self._monitor.night_state().to_json(),
            "lastEmail": self._monitor.last_email(),
            "language": self._language,
            "viewer": {
                "local": local,
                "share": self._share,
                "codeSet": code_set,
                "lanUrl": self._lan_url if local else None,
            },
        }

    def _authorize(self, request: BaseHTTPRequestHandler, client: str) -> tuple[int, dict[str, object]] | None:
        """None when another computer may press a button; else the refusal to send."""

        now = time.monotonic()
        with self._lock:
            code = self._code
            recent = [moment for moment in self._failures.get(client, []) if now - moment < LOCK_SECONDS]
            self._failures[client] = recent
            if not code:
                return 403, {"error": "local-only"}
            if len(recent) >= LOCK_AFTER:
                return 429, {"error": "locked", "retryAfter": round(LOCK_SECONDS - (now - recent[0]))}
            given = request.headers.get("X-Control-Code") or ""
            if hmac.compare_digest(given.encode("utf-8"), code.encode("utf-8")):
                self._failures.pop(client, None)
                return None
            recent.append(now)
            return 403, {"error": "bad-code", "remaining": LOCK_AFTER - len(recent)}

    def handle(
        self, request: BaseHTTPRequestHandler, method: str, path: str, body: bytes, client: str = "127.0.0.1"
    ) -> bool:
        """Serve the page and its API; False when the address is not ours."""

        split = urlsplit(path)
        route = split.path
        local = is_loopback(client)
        if not local and self._share != "lan":
            self._send(request, 403, "text/plain; charset=utf-8", b"this page is for the observatory computer only")
            return True
        if method == "GET":
            if route in ("/", "/index.html", "/setup", "/setup/"):
                self._send(request, 200, "text/html; charset=utf-8", self._page if local else self._remote_page)
                return True
            if route == "/api/status":
                self._json(request, self._state(client))
                return True
            if route == "/api/history":
                try:
                    hours = float(parse_qs(split.query).get("hours", ["12"])[0])
                except ValueError:
                    hours = 12.0
                hours = min(max(hours, 1.0), 48.0)
                self._json(request, {"hours": hours, "points": self._monitor.history(hours)})
                return True
            preview = _PREVIEW.match(route)
            if preview is not None:
                try:
                    data = (self._preview_dir / f"{preview.group(1)}.jpg").read_bytes()
                except OSError:
                    self._send(request, 404, "text/plain", b"no preview")
                    return True
                self._send(request, 200, "image/jpeg", data)
                return True
            return False
        if method == "POST":
            # A page from elsewhere cannot add this header without the browser asking first.
            if request.headers.get("X-Sky-Monitor") != "1":
                self._json(request, {"error": "missing-header"}, 403)
                return True
            if route == "/api/control-code":
                if not local:
                    self._json(request, {"error": "local-only"}, 403)
                    return True
                try:
                    code = str(json.loads(body.decode("utf-8") or "{}")["code"]).strip()
                except (ValueError, KeyError, TypeError):
                    self._json(request, {"error": 'expected {"code": "..."}'}, 400)
                    return True
                if code and not CONTROL_CODE.match(code):
                    self._json(request, {"error": "4 to 32 letters or digits"}, 400)
                    return True
                try:
                    set_setting(self._config_path, "web", "control_code", code)
                except (OSError, ConfigError) as error:
                    self._json(request, {"error": str(error)}, 500)
                    return True
                with self._lock:
                    self._code = code
                    self._failures.clear()
                self._json(request, self._state(client))
                return True
            if route in ("/api/roof", "/api/ack", "/api/email/test") and not local:
                refusal = self._authorize(request, client)
                if refusal is not None:
                    self._json(request, refusal[1], refusal[0])
                    return True
            if route in ("/api/roof", "/api/ack"):
                key = "opened" if route == "/api/roof" else "acknowledged"
                try:
                    wanted = json.loads(body.decode("utf-8") or "{}")[key]
                except (ValueError, KeyError, TypeError):
                    self._json(request, {"error": f'expected {{"{key}": true|false}}'}, 400)
                    return True
                if key == "opened":
                    self._monitor.set_roof_opened(bool(wanted))
                else:
                    self._monitor.set_acknowledged(bool(wanted))
                self._json(request, self._state(client))
                return True
            if route == "/api/email/test":
                try:
                    self._monitor.send_test_email()
                except EmailError as error:
                    self._json(request, {"ok": False, "error": str(error)})
                    return True
                self._json(request, {"ok": True})
                return True
        return False
