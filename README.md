# plex-loudness

Fill in Plex's **video loudness data** (the measurements behind *Normalize Loudness* /
*Boost Dialog*) with ffmpeg, in parallel, on as many machines as you like — instead of
Plex's built-in job, which analyses one file at a time and can run out of memory on some
4K files.

The values are **identical to Plex's own**: Plex measures with ffmpeg's `loudnorm` filter,
and plex-loudness runs the same filter with the same settings and stores the result in
exactly the format Plex writes. In testing, full-mode results matched Plex's stored values
on every field of every track compared, and 100,000 existing database rows were rebuilt
byte for byte.

> **Unofficial.** This writes to the Plex library database, which Plex doesn't support.
> Every import takes an online backup first, only touches loudness fields, never replaces
> data Plex already has (unless you ask), and skips files that changed since measuring.
> Try a small batch first.

## How it works

```
 ┌───────────── controller (where Plex runs) ─────────────┐        ┌──── worker(s) (anywhere that sees the media) ────┐
 │ plex-loudness tui / queue / import                      │        │ plex-loudness-worker                              │
 │  • reads the Plex DB: which audio tracks lack loudness  │        │  • claims tracks from queued batches              │
 │  • writes a batch into the shared folder                │ shared │  • runs ffmpeg loudnorm, one process per track    │
 │  • imports finished results into the Plex DB            │ folder │  • writes results + live status                   │
 └─────────────────────────────────────────────────────────┘ <────> └───────────────────────────────────────────────────┘
```

* The **controller** is the only part that touches the database. It runs Plex's own
  `Plex SQLite` — inside the Plex container (`docker exec`) or on the host.
* **Workers** only read media files and the shared folder. Run one next to the disks (a NAS
  is ideal), or several: tracks are claimed atomically, so workers never duplicate work, and a
  crashed worker's tracks are taken over after 10 minutes.
* Each audio track is its own ffmpeg process (loudnorm is single-threaded), so a worker with
  N slots keeps N cores busy. Memory per track is ~150–400 MB.

### What Plex stores

Per audio stream, in `media_streams.extra_data`:

| Plex field | loudnorm value | meaning |
|---|---|---|
| `ln:loudness` | `input_i` | integrated loudness (LUFS) |
| `ln:peak` | `input_tp` | true peak (dBTP) |
| `ln:lra` | `input_lra` | loudness range (LU) |
| `ln:threshold` | `input_thresh` | gating threshold |
| `ln:gainOffset` | `target_offset` | offset after loudnorm's normalisation |
| `ln:loudnessAnalysisVersion` | `0.02` | |

measured with `loudnorm=I=-16:TP=-1:LRA=9:print_format=json`. Music libraries use different
fields (`ld:*`) and are not handled — Plex's music analysis works fine on its own.

## Measurement modes

| Mode | What it reads | Accuracy (vs full) | CPU |
|---|---|---|---|
| `full` (default) | whole audio track | **exact** — same values as Plex | 1× |
| `quick` | 24 × 30 s samples | loudness ±0.7 dB on average (worst ~1.7), peaks under-read | ~4× less |
| `sampled` | N × S s samples | worse with fewer samples (5×10 s: ±1.3 dB avg, ±3 dB worst) | |

Samples miss the loudest moments, so sampled peaks are almost always too low. `import` skips
sampled results unless you pass `--allow-sampled`. Use `quick` for a fast first pass if you
accept approximate values; use `full` for anything you keep.

## Quick start (Docker)

1. **A shared folder** both sides can reach, e.g. a NAS share mounted on the Plex host.
2. **Config**: `cp config.example.toml config.toml` and set `work_dir`, the `[plex]` section
   (on the controller) and `[worker.path_map]` (on each worker — Plex paths → paths the worker
   sees, under `/media` inside the container).
3. **Worker** (next to the media):
   ```bash
   MEDIA_DIR=/mnt/user/media WORK_DIR=/mnt/user/share/plex-loudness PUID=99 PGID=100 \
     docker compose -f docker/compose.worker.yml up -d
   ```
4. **Controller** (on the Plex host):
   ```bash
   WORK_DIR=/mnt/share/plex-loudness docker compose -f docker/compose.controller.yml run --rm controller
   ```
   or simply `./plex-loudness` (TUI) / `./plex-loudness status` — a wrapper that reads `docker/.env`.
   Pick libraries, optionally a date-added window or release years, *Preview*, *Queue batch*.
   Watch progress live; when a batch finishes, *Import into Plex*.

Without Docker: `pip install '.[tui]'` (needs ffmpeg on workers), then `plex-loudness` and
`plex-loudness-worker`. The CLI and worker need only the Python standard library; the TUI
needs Textual.

## CLI

```bash
plex-loudness status                                   # libraries, batches, workers
plex-loudness libraries
plex-loudness queue --library "TV Shows" --added-since 90d --dry-run
plex-loudness queue --library Movies --year 1990-1999 --mode quick
plex-loudness queue --items 12345 67890                # specific Plex items
plex-loudness watch                                    # live text view
plex-loudness batches [show|cancel|resume|retry|delete] <batch>
plex-loudness import <batch> --dry-run
plex-loudness import <batch>                           # backs up, writes, verifies
plex-loudness compare <batch> --plex                   # vs Plex's own values
plex-loudness compare <quick batch> <full batch>       # sampled vs full
plex-loudness selftest                                 # rebuild existing rows byte for byte
plex-loudness-worker [--workers 16] [--batch <id>] [--once]
```

Date filters accept `30d`, `6w`, `3m`, `1y` or `YYYY-MM-DD`; years accept `2001`, `1990-1999`,
`2010-`, `-1980`. Batches are processed oldest first; within a batch, newest additions first.

## Turning Plex's own analysis back on

Plex skips anything that already has loudness data, so after importing you can set
*Settings → Library → Analyze audio tracks for loudness* back on and Plex only measures new
files. To confirm on your server, restart Plex and watch which files its `Plex Transcoder …
loudnorm` processes open: imported items should not appear.

## Development

```bash
pip install -e '.[tui,dev]'
pytest
```

## Licence

MIT — see [LICENSE](LICENSE). Not affiliated with Plex Inc.
