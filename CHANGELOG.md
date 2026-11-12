# Changelog

## 2.2.1
- **Deleting a batch stops its running tracks.** Workers kept measuring a deleted batch's tracks
  (hours, for a long film in full mode), so a new batch sat at *queued* with no free slots, and the
  results recreated the deleted folder. Now a worker kills them within seconds and frees the slots.
  *Now measuring* also shows each track's batch (*deleted* in red).
- **Paused time isn't counted** in a running track's elapsed time (TUI *now measuring* and
  `plex-loudness status`): the timer stands still while the batch or worker is paused.
- **A paused worker is no longer hidden behind a "running" batch.** Resuming a batch doesn't
  resume a worker paused with `w`, and the batch still looked like it was running while nothing
  moved. Now the batch shows *worker paused* when every live worker is paused, and resuming a batch
  (TUI or `batches resume`) says which workers are still paused and how to resume them.
- **TUI buttons**: *pause* turns into *resume* for a paused or cancelled batch (it also un-cancels).
  *Cancel* is red, only cancels, asks first (the default answer is *keep it*), and is greyed out
  once the batch is cancelled or finished. New **pause worker / resume worker** button under the
  workers list (same as `w`; with one worker it doesn't need selecting).

## 2.2.0
- **Added or replaced since**: the date filters now match files Radarr/Sonarr replaced, not only
  newly added movies/episodes. Plex keeps a movie's original `added_at` when its file is upgraded;
  the new file's audio streams carry the replacement date (`media_streams.created_at`). On a real
  library, "30d" went from 36 to 575 movies.
- **Pause / resume** a batch (`s`, `batches pause|resume`) or a whole worker (`w`,
  `workers pause|resume <name|all>`): running ffmpeg processes are frozen (SIGSTOP/SIGCONT), keep
  their progress, and paused time doesn't count towards the timeout. Cancelling a paused batch
  wakes its tracks so they finish.
- **Worker CPU and memory in the controller**: each worker reports its container's CPU (cores in
  use / limit) and memory (in use / limit) and its host's CPU, load and memory; the TUI flags an
  overloaded host. Also in `plex-loudness workers` / `status`.
- **Timeouts**: a track is killed when ffmpeg makes no progress for `STALL_SECONDS` (900), and the
  hard limit is now 4 x duration + 30 min (`TIMEOUT_FACTOR`; was 1.5x), so slow tracks on a busy
  host finish instead of being killed. Timeout errors say why.
- **Docker**: one compose file, two services: `plex-loudness` (controller) and
  `plex-loudness-worker`, picked with `COMPOSE_PROFILES` (`controller`, `worker`,
  `controller,worker`). Replaces `ROLE` and `docker-compose.controller.yml`; see the README for
  upgrading.
- README: step-by-step controller/worker deployment, the shared folder and media path mapping,
  pause/cancel/retry, timeouts.

## 2.1.1
- README: why this exists (Plex forum reports), banner, screenshot, desktop-worker guide.
- `docker-compose.cifs.yml`: mount the media and shared folder from SMB shares (Docker Desktop).
- GitHub Actions: CI (ruff, pytest on 3.11/3.13, demo screenshots, Docker end-to-end test that
  measures a generated clip with a worker) and Release (multi-arch image to GHCR on `v*` tags).
- `docs/screenshots.py` regenerates the banner and screenshot from demo data.
- `WORK_PATH` defaults to `./work`; thousands separators in import summaries; tidier batch table.

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
