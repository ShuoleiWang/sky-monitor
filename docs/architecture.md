# Sky Monitor — architecture and product direction

Sky Monitor answers two questions for a remote observatory, once a minute: may the roof be
open, and may imaging run. This page states how it is built, what every part may and may
not do, and where it is going as a product and as an open-source project.

## Principles

1. **Evidence before advice.** The cameras are the evidence: stars that move with the sky.
   Forecasts, sensors and operators' marks are advice around that evidence. Nothing opens
   the roof without stars having been seen for the configured wait.
2. **Fail closed, every path.** No frame, a frozen picture, a fault, a stale answer, a
   stopped monitor, a clock jump: each reads as "not safe". A reader of `status.json`
   treats an answer past `validUntil` as not safe; the Alpaca devices do that themselves.
3. **Prove, don't claim.** Thresholds come from recorded nights, not from opinion:
   archived frames are the regression set, and every forecast source is scored every night
   against what the cameras measured.
4. **Nothing leaves the site that need not.** Camera passwords stay in the settings file
   and never reach a log, a status or a mail; forecast sources receive the site's
   coordinates rounded to about a kilometre and nothing else.
5. **Small machines.** One look at a 1080p camera costs about 0.15 s of analysis; the
   monitor idles by day and after the operator has taken over.

## Components

| Module | Owns | May not |
|---|---|---|
| `sources` | Getting frames: ffmpeg streams, HTTP snapshots, files; hard time limits; password redaction; cancellation | Judge the sky |
| `stars`, `analysis` | The science: compact sources, fixed-source learning, sky cells, coverage, a per-camera verdict | Know about roofs, mail or time of day |
| `decision` | Policy: the wait, hysteresis, grace times, daylight, vetoes | Read cameras or files |
| `forecast`, `providers/*` | Tonight's forecast from several sources, their consensus, their measured skill, the rain veto | Decide anything alone |
| `night` | The operator's marks for tonight (roof opened, reminder read) | — |
| `service` | The cycle: cameras → verdicts → decision → outputs; status, history, previews, archive; mail; nightly scoring | Contain science or policy |
| `alpaca`, `web`, `mail`, `tray`, `cli` | Outputs and controls: Alpaca SafetyMonitor devices, the control page, SMTP, the tray icon, the command line | Change the answer |
| `config`, `messages` | Settings read strictly; every sentence in Chinese and English | — |

Dependencies point inwards: outputs depend on `service`, `service` on the science and
policy modules, and nothing depends on the outputs. `synthetic` renders test skies.

## Contracts

- **`status.json`** (`schema: sky-monitor.status/1`): `safe`, `imagingOk`, `reason`,
  `message`, `validUntil`, `coverage`, `heldSeconds`/`heldCoverage`, `sun`, `moon`,
  `night` (the operator's marks), `cameras[]` (verdict, reason, coverage, star counts),
  `forecast` (below), `lastEmail`. New fields are added; existing ones keep their meaning.
- **`forecast`** in the status: `night[]` hour by hour (`t`, `cloud`, `cloudLow/Mid/High`,
  `rainProbability`, `rainMm`, `windMs`, `gustMs`, `humidity`, `dewPointC`, `temperatureC`,
  `seeing`, `transparency`, `sources{key: cloud}`), `rainSoon`, `rainWindowHours`,
  `maxWindMs`, `providers[]` (`key`, `model`, `issuedAt`, `fetchedAt`, `error`, `skill`
  {`n`, `mae`, `hitRate`}, `weight`), `refreshedAt`.
- **Files** under the data directory: `status.json`, `history/<date>.jsonl` (every look,
  without the forecast block), `preview/<camera>.jpg`, `archive/<camera>/<time>-<verdict>.png`,
  `state/<camera>.fixed.npz`, `state/night.json`, `reference.json`,
  `forecast/latest.json`, `forecast/snapshots/<date>.jsonl`, `forecast/skill.jsonl`.
- **Alpaca**: two SafetyMonitor devices (0 roof, 1 imaging), ASCOM Alpaca API v1,
  discovery on UDP 32227; unsafe while no client is connected or the answer is stale.
  They serve the observatory computer only unless `[alpaca] host` names a network address.
- **The page's access model**: the observatory computer sees and does everything; with
  `[web] share = "lan"` other computers see everything except the settings' location and
  may press a button only with the control code (constant-time comparison, five failures
  lock an address out for ten minutes); every POST must carry `X-Sky-Monitor: 1`, which a
  page from elsewhere cannot add without a preflight the server never grants.
- **Extension points**: a frame source implements `grab() -> FrameBurst`; a forecast source
  implements `Provider` (`name`, `fetch(latitude, longitude) -> list[Forecast]`); a
  notification is the `[notify] command` hook today and a `Notifier` interface next.

## The forecast subsystem

Several sources are read every hour (Open-Meteo with several models, 7Timer's astronomy
product, Caiyun and QWeather when the operator has keys). The page shows the consensus
for the hours of darkness ahead — a weighted median per hour and quantity, ties going to
the cloudier value — next to every source's own cloud line. Every snapshot is kept; when
a night ends, each source's cloud fraction issued about six hours ahead is compared with
the cloud fraction the cameras measured (one minus the coverage, hourly medians), and the
error over the last 30 nights becomes the source's weight once it has 24 scored hours;
hours under a bright Moon are left out, since moonlight hides stars the way cloud does.
"Which forecast is most accurate here" is therefore measured at the site, not assumed.
The only way a forecast touches the decision is the optional rain veto: with
`rain_veto_hours` set, a closed roof stays closed while rain is forecast within that
window; the wait keeps counting, so the roof opens as soon as the rain risk is gone. An
open roof is never closed by a forecast alone.

## Roadmap to a product

1. **Own repository** (`sky-monitor`, MIT) — done locally: `src/skymonitor`, `tests`,
   `docs`, `packaging`, `scripts`, README in English and Chinese, CHANGELOG, semantic
   versions from 0.x. Still to come: publishing it, continuous integration on Windows, macOS
   and Linux, and installers built from tags.
2. **Settings on the page**: a form over the TOML file for the common settings (cameras,
   site, mail, tolerance), with the file staying the source of truth and the monitor
   restarting itself after a change.
3. **Validation that runs itself**: a `benchmark` command that replays archived nights
   with the current thresholds and reports what would have opened and closed; the
   forecast skill table; a validation page that states what has and has not been checked.
4. **More evidence**: roof state from the indoor cameras (open roof shows sky, closed
   shows the ceiling) to replace the manual mark; a hardware rain sensor or cloud sensor
   read through ASCOM/Alpaca and combined with the cameras; a Boltwood-format file for
   software that reads one.
5. **Trust**: ASCOM ConformU conformance, a signed installer, N.I.N.A. walkthroughs.

## What is validated, and what is not

Synthetic skies and a simulated live H.264 stream cover the analysis, the decision, the
sources and the interfaces. One real camera (a 2560x1440 HEVC surveillance camera at
2.7 Mb/s facing north-west) has been watched on an overcast night, where the verdict
matched the picture; its behaviour under a clear sky, real mail delivery, N.I.N.A. and
the Windows installer have not been checked yet. The validation section of the README is
kept current.
