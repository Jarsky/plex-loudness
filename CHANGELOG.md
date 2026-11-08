# Changelog

## 2.1.0
- TUI restyled in the media-health look: neon figlet header, overview panel with progress bar,
  rounded colour-coded panels, coloured batch states, key hints.
- Docker made simple: one `docker-compose.yml` + `.env`, `ROLE=controller|worker|both`,
  worker drops to PUID/PGID, docker socket via `docker-compose.controller.yml`.
- Auto-detection: Plex container (the one with Plex SQLite), database owner, and Plex folder
  -> media folder mapping (by finding real files under MEDIA_PATH). Env overrides for all.
- Shared folder kept writable when controller and workers run as different users.

## 2.0.0
- Split into a controller (`plex-loudness`, TUI + CLI) and workers (`plex-loudness-worker`)
  sharing a folder; any number of workers, atomic track claims, stale-claim takeover.
- Textual TUI: library selection, date-added and release-year filters, preview with ETA,
  queue, live progress per worker and track, import with confirmation, retry/cancel/delete.
- `quick` preset (24 x 30 s samples) alongside exact `full` mode; custom `sampled`.
- One TOML config; docker and local Plex SQLite backends; no hard-coded paths.

## 1.x (internal)
- Single script: export / analyze / import, exact Plex extra_data serialisation,
  one ffmpeg per track, concat-demuxer sampling, live status, compare.
