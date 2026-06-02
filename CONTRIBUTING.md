# Contributing to MusicTidy

Thanks for your interest! MusicTidy is a small project run by one person — I read every issue and PR, but I may be slow on response time. Please be patient.

## What lives in this repo

This is the **server** + **landing site** half of MusicTidy:

```
server/    FastAPI server: scan / fingerprint / MusicBrainz / streaming
site/      musictidy.com landing page (Astro)
docs/      design notes, beets config examples
.env.example, Makefile, etc.
```

The **iOS client** lives in a separate (currently private) repository and isn't covered by this CONTRIBUTING.

## Filing an issue

The easiest way to help is a clear bug report or focused feature request.

**Bug**: include
- iOS version (if client-side), or distro / Python version (if server)
- Server commit hash (`git rev-parse --short HEAD`)
- Steps to reproduce
- Relevant logs (`sudo journalctl -u musictidy --since '10min ago' | tail -100`)

**Feature**: describe the user-facing problem first. Implementation specifics are welcome but optional.

[Open an issue →](https://github.com/WanpengQian/musictidy/issues/new/choose)

## Pull requests

Small, focused PRs are easiest to review. If you're tackling something big, please open an issue first so we can align on shape.

### Setup

```bash
git clone https://github.com/WanpengQian/musictidy.git
cd musictidy/server
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp ../.env.example .env   # edit values
.venv/bin/python -m app.main
```

### Tests

```bash
cd server
.venv/bin/python -m pytest tests/ -q
```

Heads up: a few smoke / auth tests currently fail on `main` due to test fixture issues — not caused by your change. The list of known-failing tests is in `tests/known_failing.md` (or skip with `--deselect`).

### Code style

- **Python**: ruff + mypy. Run `ruff check server/` before pushing.
- **Comments**: explain *why*, not *what*. The code already tells you what.
- **Commit messages**: imperative mood ("fix X" not "fixed X"), first line under 70 chars.

## Reporting security issues

**Don't** open a public issue for security bugs. Email `security@musictidy.com` instead. We follow a 90-day responsible disclosure window with public credit.

## Architecture quick map

- `server/app/main.py` — FastAPI app + middleware
- `server/app/api/library.py` — `/items` / `/artists` / `/organize` / `/search` etc.
- `server/app/api/admin.py` — scan / queue / diagnose endpoints
- `server/app/workers/` — async task workers (fingerprint, MB fetch, scan, archive_extract, cue_split, organize)
- `server/app/beets_bridge.py` — beets library wrapper
- `server/app/db.py` — SQLAlchemy + ATTACH for beets

The "why" docs live alongside code as docstrings. Open the file before opening a PR.

## License

By contributing, you agree your contributions are licensed under the [MIT License](LICENSE) — the same license as the rest of the project.
