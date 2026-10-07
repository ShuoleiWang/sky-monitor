# Changelog

All notable changes are documented here. The format follows Keep a Changelog;
versions follow Semantic Versioning from the first stable release.

## [Unreleased] — 0.1.0a1

First version: sky cameras decide whether the roof may open.

- Reads an observatory's sky cameras (RTSP/HTTP streams through ffmpeg, HTTP
  snapshots, image files) and publishes once a minute whether the roof may be
  open and whether imaging may run, as `status.json`, on a control page and as
  two ASCOM Alpaca SafetyMonitor devices. The evidence is stars: compact
  sources that are seen again where the sky carried them and that do not stay
  fixed in the frame, counted per sky cell. The roof opens on the average share
  of sky with stars over a ten-minute wait, so a few clouds do not block it
  (`cloud_tolerance`); cloud, missing or frozen camera data, daylight, a clock
  jump or a stopped monitor read as not safe.
- As a product: a tray program (`sky-monitor tray`) that starts with Windows
  and watches from dusk; an Apple-style control page on `127.0.0.1:11112` with
  the answer, a sky gauge, the camera pictures, the night's course, tonight's
  forecast and the buttons *Roof opened*, *Read* and *Cancel*; mail when the
  roof may open (pictures attached), repeated every 30 minutes until *Read* or
  *Roof opened*, and when the sky closes again. *Roof opened* ends the night's
  watching and can be cancelled. `scripts/build-windows.ps1` builds an
  installer (not yet run).
- Tonight's forecast beside the cameras: Open-Meteo (five models), 7Timer's
  astronomy product and, with a key, Caiyun or QWeather, combined into a
  weighted median per hour; every source is scored each night against the
  cloud the cameras measured, and the scores set the weights. Its only effect
  on the answer is an optional rain veto that keeps a closed roof closed; the
  sources receive the site's coordinates rounded to about a kilometre.
- The Moon: a bright Moon high up lowers the stars a clear sky must show, its
  halo's cells are set aside, imaging can wait for a Moon lit beyond a chosen
  fraction to set (the roof never does), moonrise and moonset show on the page,
  and moonlit hours do not score the forecast sources.
- Sharing the page: with `[web] share = "lan"`, computers on the same network
  can view the page read-only; its buttons need a control code set on the
  observatory computer's page (five wrong codes lock that computer out for ten
  minutes), and the safety devices stay for the observatory computer. While
  N.I.N.A. or other software reads the safety devices, *Roof opened* only ends
  the mail, so that the devices never turn unsafe because of the mark.
- Checked by 171 tests on synthetic skies and recorded forecast answers, a
  rehearsal on a simulated live H.264 stream, and one real installation on
  Windows 11: its first, overcast night read cloudy throughout, as the
  pictures showed, and on its second, partly cloudy night it allowed the roof
  at 19:41 as twilight deepened (101 stars, 71 % of the sky on average), which
  the operator judged reasonable. A camera does not measure wind, humidity or
  the first rain: keep a hardware rain sensor.
