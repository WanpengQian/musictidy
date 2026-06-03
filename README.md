# MusicTidy

> Self-hosted music library — your music, your server, your phone.

MusicTidy turns the pile of MP3 / FLAC / APE files you've been hoarding for 20 years into a clean private music archive. It runs on your own machine, identifies every track via [AcoustID](https://acoustid.org) + [MusicBrainz](https://musicbrainz.org), pulls album art, normalizes the directory structure, splits CUE+APE images, extracts RAR/ZIP/7z archives, and serves a native iOS client over HTTPS.

- 🌐 Project page: <https://musictidy.com>
- 🎵 Live demo: <https://demo.musictidy.com> · scheme `https` · host `demo.musictidy.com` · password `demo2026`
- 🖥 Web client: **<https://app.musictidy.com>** — free, no install, just point at your server
- 📱 iOS client: App Store
- 📚 Deployment guide: <https://musictidy.com/deploy> · Cloudflare Tunnel quickstart: [`docs/cloudflare-tunnel.md`](docs/cloudflare-tunnel.md)
- 🔐 Privacy policy: <https://musictidy.com/privacy>
- 💬 Bugs / questions: [GitHub Issues](https://github.com/WanpengQian/musictidy/issues)

## What lives in this repo

```
server/    Python 3.11 + FastAPI server (the OSS focus)
site/      Astro source for musictidy.com
docs/      design notes, beets config example
```

The iOS client is distributed via the App Store. Its source lives in a separate repository for now.

## Quick start (self-host)

```bash
# Debian / Ubuntu
sudo apt install -y python3 python3-venv ffmpeg libchromaprint-tools unar git

git clone https://github.com/WanpengQian/musictidy.git
cd musictidy/server
python3 -m venv .venv
.venv/bin/pip install -e .

cp ../.env.example .env
# edit at minimum: APP_PASSWORD, MUSIC_ROOT, ACOUSTID_API_KEY

.venv/bin/python -m app.main
# → http://localhost:8765/healthz
```

### Make it reachable from outside without opening ports

The recommended way is **Cloudflare Tunnel** — free, no port forwarding, no DDNS, automatic HTTPS, works behind CGNAT. Walks through it in [`docs/cloudflare-tunnel.md`](docs/cloudflare-tunnel.md). Once your tunnel is up and you have e.g. `m.your-domain.com` pointing at your local server:

- iOS app: enter `m.your-domain.com` in server setup
- Web: open [https://app.musictidy.com](https://app.musictidy.com), enter the same address

The web client is hosted free at `app.musictidy.com` and connects directly to **your** server — your music never touches our infrastructure.

Full deployment guide (systemd unit, reverse proxy, backups, upgrades) at <https://musictidy.com/deploy>.

## How it works (one paragraph)

You drop your audio files into `MUSIC_ROOT` — any directory shape works. On scan, MusicTidy ingests every file into a beets library. The fingerprint worker computes a chromaprint for each track, queries AcoustID for a MusicBrainz recording match, and writes `mb_trackid` / `mb_releasegroupid` / `mb_albumartistid` back to the tags. A second worker fetches the release-group + artist metadata from MusicBrainz and caches it locally. CUE+APE/FLAC images are auto-split into per-track files; ZIP/RAR/7z archives are auto-extracted (via `unar`, which handles GBK/Shift-JIS filenames). The iOS client talks to a small HTTP API (`/api/v1/items`, `/api/v1/artists/{mbid}/owned-albums`, `/api/v1/items/{id}/stream`) and gets album art via a server-side proxy.

## Why does this exist?

Read the [pitch](https://musictidy.com/#why) for the long version. Short:

- Streaming services delete songs from your library, swap remix versions, and end your subscription on their terms.
- Your local files are a graveyard of broken metadata, mojibake filenames, missing covers, and unplayable CUE+APE images.
- Existing self-hosted options either treat music as a video afterthought (Jellyfin) or cost $150/yr and aren't open source (Roon).

MusicTidy is the smallest possible answer to "I just want my music to be mine again."

## What's integrated

This project stands on the shoulders of these open-source giants — full list in [`docs/third-party.md`](docs/third-party.md):

- **[beets](https://beets.io/)** — music library autotagger + path templating (10+ years mature)
- **[chromaprint](https://acoustid.org/chromaprint)** + **[AcoustID](https://acoustid.org/)** — audio fingerprinting
- **[MusicBrainz](https://musicbrainz.org/)** — canonical metadata
- **[Cover Art Archive](https://coverartarchive.org/)** — album art
- **[ffmpeg](https://ffmpeg.org/)** — APE/FLAC → AAC realtime transcoding
- **[FastAPI](https://fastapi.tiangolo.com/)** + **[htmx](https://htmx.org/)** — server + admin web

## Status

Pre-1.0. The server has been daily-driven against a ~3000-track personal library for months. The iOS client is in TestFlight and pending App Store review.

| Area | Status |
| --- | --- |
| Scan / fingerprint / MusicBrainz | ✓ stable |
| Organize (path normalize) | ✓ stable |
| Archive / CUE extraction | ✓ stable |
| iOS client (browse / stream / offline cache / Face ID) | ✓ TestFlight |
| iOS App Store submission | ⏳ pending |
| Subtitled / classical music model | ⏳ post-1.0 |
| CarPlay | ⏳ post-1.0 |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Small focused PRs and clear bug reports both very welcome.

For security issues, please email `security@musictidy.com` instead of opening a public issue.

## License

[MIT](LICENSE) © 2026 Wanpeng Qian
