# Contributing

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

On Windows: `py -3.12 -m venv .venv`, then `.venv\Scripts\python -m pip install -e ".[dev]"`. The tests must
pass in any time zone (try `TZ=UTC` and `TZ=America/Sao_Paulo`). Report security problems privately, as
[SECURITY.md](SECURITY.md) describes; everyone here follows the [code of conduct](CODE_OF_CONDUCT.md).

Rules that keep the answer trustworthy:

1. **Fail closed.** Any path that cannot measure the sky reads as "not safe". Never add a
   fallback that reports safe without stars having been seen.
2. **Science, policy and outputs stay apart** (see [docs/architecture.md](docs/architecture.md)):
   the analysis knows nothing about roofs, the decision reads no files, outputs never change
   the answer.
3. **Nothing private in the tree.** No site coordinates, camera addresses or passwords, mail
   addresses, keys or recordings of a real site. Tests use public coordinates (26.70, 100.03)
   and synthetic skies; recorded service answers are trimmed and carry no key.
4. **Both languages.** Every sentence a person reads exists in Chinese and English
   (`messages.py`, `web.py`, `tray.py`).
5. **Prove changes on recorded nights** when they can move a verdict, and say in the change
   what was and was not checked.
