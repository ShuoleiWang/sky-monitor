# CLAUDE.md

Sky Monitor decides from an observatory's sky cameras whether the roof may open and
imaging may run, and serves the answer as `status.json`, a control page, mail and ASCOM
Alpaca SafetyMonitor devices. Read [README.md](README.md) for what it does,
[docs/architecture.md](docs/architecture.md) for how it is built and where it is going, and
[CONTRIBUTING.md](CONTRIBUTING.md) for the rules.

## Working agreement

- **Local by default.** Edit locally; commit, push or publish only when the maintainer asks.
  Commit messages carry no AI attribution.
- **Nothing private in the tree.** No site coordinates, camera URLs or passwords, mail
  addresses, keys, recordings or absolute home paths. Before handing over, grep the tree for
  the private values you handled in the session.
- **Fail closed** and keep science (`stars`, `analysis`), policy (`decision`) and outputs
  (`alpaca`, `web`, `mail`, `tray`, `cli`) apart.
- **Prove, don't claim.** A change that can move a verdict is checked on recorded nights;
  reports state what was checked and what was not.
- **Reports** lead with the outcome. The maintainer writes Chinese; answer in the language
  used. Documentation is English with the mirrored `README.zh-CN.md`, updated together.

## Commands

```bash
.venv/bin/python -m pytest -q                      # the test suite (passes in any time zone)
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/python -m skymonitor doctor --config <file>
.venv/bin/python -m skymonitor analyze <picture|video|rtsp url> --out <new.jpg>
```
