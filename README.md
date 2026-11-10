<p align="center">
  <a href="https://github.com/Jarsky/plex-loudness/actions/workflows/ci.yml"><img src="https://github.com/Jarsky/plex-loudness/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-9b5cff" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-3fa0ff" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/docker-ready-22d3ee" alt="Docker">
  <img src="https://img.shields.io/badge/ffmpeg-loudnorm-ff2bd6" alt="ffmpeg loudnorm">
</p>

<p align="center"><img src="docs/banner.svg" alt="plex-loudness" width="760"></p>

Fills in Plex's **video loudness data** (the stuff behind *Normalize Loudness* and *Boost Dialog*)
using ffmpeg, in parallel, on as many machines as you want to throw at it.

The values come out **identical to Plex's own**. Plex measures with ffmpeg's `loudnorm` filter,
so plex-loudness runs the same filter with the same settings and writes the result in exactly the
format Plex stores. In testing, every field of every compared track matched what Plex had, and
100,000 existing database rows were rebuilt byte for byte.

![The controller TUI](docs/screenshots/tui.svg)

## Why this exists

Since Plex Media Server 1.43.4, Plex can [even out the volume of movies and TV](https://support.plex.tv/articles/audio-track-enhancements-for-video/)
on supported clients. It's a great feature, especially for TV where every show seems to be mixed
at a different level. The catch is that every audio track has to be analysed first, and Plex's
own analysis really struggles with a big library:

* **It does one file at a time.** The analysis is a single background job using about one CPU
  core, and it measures the whole soundtrack at true-peak precision. That runs at a few times real
  time, so a library with tens of thousands of hours of audio takes *months*, no matter how many
  cores the server has.
* **It can eat all your memory.** On some files (often 4K remuxes with Dolby Digital+, TrueHD or
  DTS-HD audio) the job keeps growing until it hits the container's memory limit and gets killed,
  sometimes taking Plex down with it. Failed items get retried after every restart, so it just
  keeps happening. Plenty of people have hit this:
  [loudness data generation memory leak](https://forums.plex.tv/t/loudness-data-generation-memory-leak/804550),
  [analyze-loudness runaway memory usage](https://forums.plex.tv/t/plex-media-scanner-analyze-loudness-runaway-memory-usage/712960),
  [memory leaks with "Analyze audio tracks for loudness"](https://forums.plex.tv/t/memory-leaks-when-enable-the-option-of-analyze-audio-tracks-for-loudness/776490).
* **It fights with playback** on the same machine, all day, for as long as the backlog lasts.

So plex-loudness does the same measurement outside of Plex. It uses stock ffmpeg (no Plex
transcoder, no EAE audio service), runs one process per track, and spreads the work over every
core on every machine you point at it. Once the backlog is imported you can turn Plex's own
analysis back on and let it handle new files as they arrive.

## How it works

```
 ┌──────── controller (where Plex runs) ────────┐          ┌──────── worker(s): NAS, desktop, … ────────┐
 │ TUI / CLI                                     │          │ plex-loudness-worker                        │
 │ • reads the Plex DB: which tracks need data   │  shared  │ • claims tracks from queued batches          │
 │ • queues batches in the shared folder         │  folder  │ • runs ffmpeg loudnorm, one process/track    │
 │ • imports finished results into the Plex DB   │ <──────> │ • writes results and live status             │
 └───────────────────────────────────────────────┘          └──────────────────────────────────────────────┘
```

The **controller** and the **workers** are split up on purpose:

* The controller is lightweight. It only needs to reach the Plex database, so it lives next to Plex.
* Measuring is pure CPU work (one core per track), so workers can run **wherever the cores are**.
  That might be the NAS sitting next to the disks, or a beefy **desktop** while you're not using
  it. Run as many as you like at once. Tracks are claimed atomically so workers never double up,
  and if a worker disappears its tracks get picked up by the others after 10 minutes.
* Workers never touch the Plex database. They only read your media and the shared folder.

### What Plex stores

Per audio stream, in `media_streams.extra_data`, measured with
`loudnorm=I=-16:TP=-1:LRA=9:print_format=json`:

| Plex field | loudnorm value | meaning |
|---|---|---|
| `ln:loudness` | `input_i` | integrated loudness (LUFS) |
| `ln:peak` | `input_tp` | true peak (dBTP) |
| `ln:lra` | `input_lra` | loudness range (LU) |
| `ln:threshold` | `input_thresh` | gating threshold |
| `ln:gainOffset` | `target_offset` | offset after loudnorm's normalisation |
| `ln:loudnessAnalysisVersion` | `0.02` | |

Music libraries use different fields (`ld:*`) and aren't handled here, since Plex's music
analysis copes fine on its own.

## Deploying a controller and workers

There's one image and one `docker-compose.yml` with two services. Each machine runs whichever it
needs, chosen with `COMPOSE_PROFILES` in its `.env`:

| On this machine | `COMPOSE_PROFILES` | Container | Needs |
|---|---|---|---|
| the one running Plex | `controller` | `plex-loudness` | `WORK_PATH` (the docker socket is mounted for you) |
| wherever there's CPU and the media (NAS, desktop, …) | `worker` | `plex-loudness-worker` | `WORK_PATH`, `MEDIA_PATH`, `PUID`/`PGID`, `WORKER_NAME` |
| one machine doing everything | `controller,worker` | both | all of the above |

Run as many workers as you like, one per machine. Give each its own `WORKER_NAME` (the machine's
name is a good choice). That's how it shows up in the controller.

### 1. Make the shared folder

The controller and the workers never talk over the network. They share one folder, which holds the
queued batches, each worker's results, and live status. **Every machine must reach the same folder**,
usually a NAS share, and `WORK_PATH` is *that machine's own path* to it:

```
NAS (worker)                       Plex host (controller)                Desktop (worker)
/mnt/user/plex-loudness   <=SMB/NFS=>  /mnt/nas/plex-loudness   <=SMB=>   //nas/plex-loudness
WORK_PATH=/mnt/user/plex-loudness  WORK_PATH=/mnt/nas/plex-loudness     (SMB_WORK in .env, see below)
```

* Mount the share on the Plex host like any other (`/etc/fstab`, or your NAS's own mounts) **before**
  starting the controller. Inside every container it is always `/work`.
* **Permissions:** the controller and workers may run as different users. plex-loudness keeps every
  file it creates group- and world-writable, so the only requirement is that each machine's
  `PUID`/`PGID` can write to `WORK_PATH`. With SMB, mount it with `file_mode=0666,dir_mode=0777`
  (or as a user that owns the share).
* **Filesystem:** a local disk, NFSv4 or SMB 3 all work. Tracks are claimed by creating a file
  atomically (`O_EXCL`), which these honour. Avoid NFSv3 without locking.
* Check it: `touch` a file in `WORK_PATH` on one machine and see it appear on the others.

### 2. The controller (on the Plex host)

```bash
git clone https://github.com/Jarsky/plex-loudness && cd plex-loudness
cp .env.example .env
#   COMPOSE_PROFILES=controller
#   WORK_PATH=/mnt/nas/plex-loudness     # this host's path to the shared folder
docker compose up -d --build
docker exec -it plex-loudness plex-loudness      # the TUI   (or ./plex-loudness)
```

It finds the Plex container by itself (whichever container has Plex SQLite in it) and the user that
owns the database. Override with `PLEX_CONTAINER` / `PLEX_USER` if needed. It needs no media.

### 3. A worker (next to the media)

```bash
git clone https://github.com/Jarsky/plex-loudness && cd plex-loudness
cp .env.example .env
#   COMPOSE_PROFILES=worker
#   WORK_PATH=/mnt/user/plex-loudness    # this machine's path to the SAME shared folder
#   MEDIA_PATH=/mnt/user/media           # your media root on this machine
#   PUID=99  PGID=100                    # can read the media and write WORK_PATH
#   WORKER_NAME=nas  WORKERS=12  CPUS=12
docker compose up -d --build
docker logs -f plex-loudness-worker
```

Within a few seconds the worker appears in the controller's worker list.

**Media paths.** Plex knows a file as e.g. `/movies/Alien (1979)/Alien.mkv`, a path *inside the Plex
container*. The worker mounts `MEDIA_PATH` at `/media` and finds Plex's folders under it by itself:
for the first file of each Plex root (`/movies`, `/tv`, …) it looks for the same file under `/media`
and its subfolders (`/media/movies`, `/media/tv_shows`, …) and remembers the match. If your layout
is unusual, set it explicitly: `PATH_MAP=/movies=/media/films,/tv=/media/tv_shows`.

**How many tracks at once.** Each track is one single-threaded ffmpeg. `WORKERS` above the cores that
are actually free doesn't help: on a busy host every track just runs slower, and slow enough tracks
hit the timeout. The controller shows each host's load. If it says *overloaded*, lower `WORKERS`
or pause that worker while the host is busy.

### A worker on your desktop

A desktop CPU with 16+ threads can chew through a backlog several times faster than a NAS. With
Docker (Docker Desktop on Windows/macOS, or plain Docker on Linux) you can mount the NAS shares
straight into the container using the SMB override:

```bash
# .env on the desktop
COMPOSE_PROFILES=worker
COMPOSE_FILE=docker-compose.yml:docker-compose.cifs.yml
WORK_PATH=.                     # not used with the SMB override, but must be set
SMB_HOST=nas.local
SMB_MEDIA=media                 # the share holding your library (read-only)
SMB_WORK=plex-loudness          # the shared folder the controller uses
SMB_USER=me
SMB_PASSWORD=secret
WORKERS=14                      # leave a couple of threads for yourself
CPUS=14
WORKER_NAME=desktop
```

Run `docker compose up -d --build`. Need the CPU back for a while? **Pause** the desktop from the
controller (`w` in the TUI, or `plex-loudness workers pause desktop`): its ffmpeg processes are
frozen, not killed, and carry on from where they were when you resume. `docker compose stop` also
works, but then the tracks it was on start again from the beginning on another worker.

Not using Docker? `pip install .`, put ffmpeg on your PATH, then run `plex-loudness-worker` with
`WORK_DIR`, `MEDIA_ROOT` (and `PATH_MAP` if needed) set as environment variables. This has only
been tested on Linux so far.

### Upgrading from 2.1 (one service with `ROLE`)

In each `.env`, replace `ROLE=…` with `COMPOSE_PROFILES=controller` / `worker` (`both` becomes
`controller,worker`) and delete the `COMPOSE_FILE=…docker-compose.controller.yml` line. Then
`docker compose up -d --build`, and on worker machines remove the old container:
`docker rm -f plex-loudness`. Workers are now called `plex-loudness-worker`.

## The TUI

* **queue work**: pick libraries (it shows how much each one already has), optionally an
  *added or replaced since/before* window and/or release years and a file limit, choose *full* or
  *quick*, hit **preview** (`p`) to see files, tracks, hours and an ETA at the current worker speed,
  then **queue batch** (`a`). A file counts as added when Radarr/Sonarr *replaced* it too: Plex keeps
  the movie's original "added" date, but the new file's audio streams are new.
* **batches**: progress of each batch. **Import into plex** (`i`) shows what it's about to write and
  asks first. Also **retry failed** (`t`), **pause / resume** (`s`), **cancel / resume** (`c`) and
  **delete** (`d`).
* **now measuring**: every track being measured on every worker, with progress and memory. Paused
  tracks show ⏸.
* **workers**: one row per worker: state, busy slots, **CPU** (cores in use / its limit), **memory**
  (in use / its limit), and its **host's** CPU, load and memory, flagged *overloaded* when the host
  has more work than cores. Select a worker and press `w` to pause or resume it.
* **overview**: analysed vs total, queued, running, failed, ready to import, speed and ETA.

### Pause, cancel, retry

| | What happens to running tracks | New tracks |
|---|---|---|
| **pause** a batch (`s`) | frozen in place (no CPU), progress kept | none from that batch |
| **pause** a worker (`w`) | that worker's tracks frozen in place | that worker takes none |
| **resume** | carry on from where they were | yes |
| **cancel** a batch (`c`) | finish normally | none from that batch |

Pausing freezes ffmpeg (SIGSTOP/SIGCONT), so a two-hour track paused at 90% still only has 10% left
when resumed, and paused time doesn't count towards its timeout. Paused tracks stay claimed by
their worker. If that worker is stopped altogether, they go back to the queue after 10 minutes.

**Retry failed** (`t`, or `plex-loudness batches retry <batch>`) queues only the batch's *failed
tracks* again. Tracks that succeeded, and the other tracks of the same file, aren't measured again.
A retried track is measured from its start, since a killed ffmpeg run can't be continued. The error
says why each one failed: `plex-loudness batches show <batch>`.

### Timeouts

A track is stopped if its ffmpeg makes **no progress for 15 minutes** (`STALL_SECONDS`), which
catches a stuck or runaway process, or runs past **4 × its duration + 30 minutes**
(`TIMEOUT_FACTOR`). A busy host can run well under real time: true-peak measurement of a TrueHD 7.1
track at 0.5× on a NAS at twice its core count is slow, not broken. If you see `ffmpeg timeout`,
the host was overloaded. Retry the failed tracks with fewer `WORKERS`, or while the host is quieter.

## Measurement modes

| Mode | Reads | Accuracy (vs full) | CPU |
|---|---|---|---|
| `full` (default) | the whole audio track | **exact**, same as Plex | 1× |
| `quick` | 24 × 30 s samples | loudness ±0.7 dB on average (worst ~1.7), peaks under-read | ~4× less |
| `sampled` | N × S s samples | worse with fewer samples (5 × 10 s: ±1.3 dB avg, ±3 dB worst) | |

Samples tend to miss the loudest moments, so sampled peaks almost always come out too low. That's
why `import` skips sampled results unless you pass `--allow-sampled`.

## CLI

Everything in the TUI is also a command (`docker exec plex-loudness plex-loudness <command>`, or
`./plex-loudness <command>`):

```bash
plex-loudness status                                   # libraries, batches, workers
plex-loudness queue --library "TV Shows" --added-since 90d --dry-run
plex-loudness queue --library Movies --year 1990-1999 --mode quick
plex-loudness queue --items 12345 67890                # specific Plex items
plex-loudness watch                                    # live text view
plex-loudness batches [show|pause|resume|cancel|retry|delete] <batch>
plex-loudness workers [list]                           # each worker's state, CPU, memory, host load
plex-loudness workers pause|resume <name|all>          # free a machine's CPU without losing progress
plex-loudness import <batch> --dry-run
plex-loudness import <batch>                           # backs up, writes, verifies
plex-loudness compare <batch> --plex                   # against Plex's own values
plex-loudness compare <quick batch> <full batch>       # sampled vs full
plex-loudness selftest                                 # rebuild existing rows byte for byte
plex-loudness-worker [--workers 16] [--batch <id>] [--once]
```

Dates accept `30d`, `6w`, `3m`, `1y` or `YYYY-MM-DD` and match files *added or replaced* in that window. Years accept `2001`, `1990-1999`, `2010-`
or `-1980`.

## Safety

This writes to the Plex library database, which Plex doesn't officially support, so it's careful
about it. Every import:

* takes an online backup of the database first (SQLite `.backup`, safe while Plex is running);
* only changes the loudness fields of the audio streams it measured, matched by Plex's own stream
  id, and never replaces data Plex already has (unless you pass `--overwrite`);
* skips any file whose size changed since it was measured (say Radarr or Sonarr upgraded it);
* writes in small transactions through Plex's own SQLite build, then reads every row back to check.

Start with a small batch, check it looks right in Plex, then go big.

## Turning Plex's analysis back on

The imported values are exactly what Plex would have written itself, so once the backlog is done
you should be able to switch *Settings → Library → Analyze audio tracks for loudness* back on and
let Plex take care of new files. It's worth checking on your own server before relying on it:
restart Plex and keep an eye on which files its `Plex Transcoder … loudnorm` processes open.
Imported items shouldn't show up.

## Development

```bash
pip install -e '.[tui,dev]'
ruff check . && pytest
python docs/screenshots.py          # regenerate the banner and screenshots from demo data
```

## Licence

MIT, see [LICENSE](LICENSE). This is a hobby project and isn't affiliated with Plex Inc.
