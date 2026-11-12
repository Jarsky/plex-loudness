"""The plex-loudness controller TUI (Textual), in the neon style of media-health."""

from __future__ import annotations

import os
import time

from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    Label,
    ProgressBar,
    RadioButton,
    RadioSet,
    RichLog,
    SelectionList,
    Static,
)

from . import __version__
from .analysis import FULL, QUICK
from .batches import Queue, run_seconds
from .catalog import Filter, libraries, parse_when, parse_years, preview, select_jobs
from .config import Config
from .importer import apply_import, plan_import
from .plexdb import PlexDB
from .resources import describe as describe_resources
from .resources import overloaded

NEON = ["#ff2bd6", "#d63bff", "#9b5cff", "#6a7dff", "#3fa0ff", "#22d3ee", "#22f5c8"]
PINK, VIOLET, BLUE, CYAN, MINT = "#ff2bd6", "#9b5cff", "#3fa0ff", "#22d3ee", "#22f5c8"
AMBER = "#ffb02e"
STATE_STYLE = {"running": f"bold {CYAN}", "queued": VIOLET, "finished": f"bold {MINT}", "imported": "green",
               "cancelled": "grey50", "paused": f"bold {AMBER}"}
_FIGLET: dict[str, str] = {}


def human(seconds: float) -> str:
    seconds = int(seconds or 0)
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    if seconds < 86400 * 2:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds / 86400:.1f}d"


def banner(width: int, height: int) -> Text:
    """'PLEX LOUDNESS' in a neon gradient: big font, small font, or one line on cramped terminals."""
    if not _FIGLET:
        try:
            import pyfiglet
            for font in ("ansi_shadow", "small"):
                _FIGLET[font] = pyfiglet.figlet_format("PLEX LOUDNESS", font=font, width=400).rstrip("\n")
        except Exception:
            pass
    art = ""
    if height >= 44:
        art = _FIGLET.get("ansi_shadow", "")
    if (not art or max(map(len, art.splitlines())) > width - 4) and height >= 36:
        art = _FIGLET.get("small", "")
    if not art or max(map(len, art.splitlines())) > width - 4:
        art = "P L E X   L O U D N E S S"
    lines = [line for line in art.splitlines() if line.strip()]
    text = Text(justify="center")
    for i, line in enumerate(lines):
        text.append(line + "\n", style=f"bold {NEON[i * len(NEON) // max(len(lines), 1)]}")
    return text


class Confirm(ModalScreen[bool]):
    DEFAULT_CSS = f"""
    Confirm {{ align: center middle; background: black 60%; }}
    #dialog {{ width: 74; height: auto; padding: 1 2; border: round {PINK}; background: #1e1e1e;
               border-title-color: {PINK}; border-title-style: bold; border-title-align: center; }}
    #dialog Horizontal {{ height: auto; margin-top: 1; align-horizontal: right; }}
    """
    BINDINGS = [("escape", "no", "Cancel"), ("y", "yes", "Yes")]

    def __init__(self, message: str, yes: str = "Yes", title: str = "confirm", no: str = "cancel",
                 danger: bool = False):
        super().__init__()
        self.message, self.yes, self.title_text, self.no = message, yes, title, no
        self.danger = danger

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog") as dialog:
            dialog.border_title = self.title_text
            yield Static(self.message)
            with Horizontal():
                yield Button(self.no, id="no")
                yield Button(self.yes, id="yes", classes="danger" if self.danger else "go")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()      # Enter on a reflex keeps things as they are

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_no(self) -> None:
        self.dismiss(False)

    def action_yes(self) -> None:
        self.dismiss(True)


class LoudnessApp(App):
    TITLE = "plex-loudness"
    CSS = f"""
    Screen {{ background: #1e1e1e; color: #d0d0d0; layout: vertical; }}
    #banner {{ height: auto; content-align: center top; padding-top: 1; }}
    #subtitle {{ height: 1; content-align: center middle; color: grey; }}
    .panel {{ border: round {VIOLET}; border-title-align: center; border-title-style: bold;
              padding: 0 1; background: transparent; }}
    #overview {{ height: auto; border: round {VIOLET}; border-title-color: {MINT}; margin: 0 1; }}
    #stats {{ height: auto; }}
    #overall {{ margin-top: 1; }}
    #overall Bar {{ width: 1fr; }}
    #overall Bar > .bar--bar {{ color: {PINK}; background: #3a3a3a; }}
    #overall Bar > .bar--complete {{ color: {MINT}; }}
    #overall PercentageStatus {{ color: #e0e0e0; text-style: bold; }}
    #overall ETAStatus {{ display: none; }}
    #main {{ height: 1fr; margin: 0 1; }}
    #queue-panel {{ width: 60; border: round {PINK}; border-title-color: {PINK}; }}
    #right {{ width: 1fr; }}
    #batches-panel {{ height: 1fr; min-height: 12; border: round {BLUE}; border-title-color: {BLUE}; }}
    #now-panel {{ height: 1fr; min-height: 15; border: round {CYAN}; border-title-color: {CYAN}; }}
    #active {{ min-height: 6; }}
    #log {{ height: 7; border: round #6a7dff; border-title-color: #6a7dff; border-title-align: center;
            border-title-style: bold; background: transparent; margin: 0 1; padding: 0 1; }}
    #hints {{ height: 1; color: grey; content-align: center middle; }}
    .hint {{ color: grey; margin-top: 1; }}
    #preview {{ margin-top: 1; height: auto; }}
    #workers {{ height: auto; max-height: 8; }}
    Input {{ border: round #4a4a4a; background: transparent; height: 3; }}
    Input:focus {{ border: round {PINK}; }}
    .row {{ height: auto; }}
    .row Input {{ width: 1fr; }}
    SelectionList {{ border: round #4a4a4a; background: transparent; height: auto; max-height: 12; }}
    SelectionList:focus {{ border: round {PINK}; }}
    RadioSet {{ border: round #4a4a4a; background: transparent; width: 1fr; }}
    RadioSet:focus {{ border: round {PINK}; }}
    Checkbox {{ border: none; background: transparent; padding: 0; margin-top: 1; }}
    DataTable {{ background: transparent; height: 1fr; scrollbar-size-vertical: 1; scrollbar-size-horizontal: 0; }}
    DataTable > .datatable--header {{ background: transparent; color: grey; text-style: bold; }}
    DataTable > .datatable--cursor {{ background: {VIOLET} 40%; color: white; }}
    DataTable > .datatable--even-row {{ background: white 3%; }}
    .buttons {{ height: auto; margin-top: 1; }}
    Button {{ border: round #6a7dff; background: transparent; color: #d0d0d0; height: 3; min-width: 10;
              margin-right: 1; text-style: bold; }}
    Button:hover {{ background: {VIOLET} 25%; }}
    Button:focus {{ text-style: bold reverse; }}
    Button.go {{ border: round {PINK}; color: {PINK}; }}
    Button.ok {{ border: round {MINT}; color: {MINT}; }}
    Button.danger {{ border: round #ff5f5f; color: #ff5f5f; }}
    """
    BINDINGS = [("q", "quit", "quit"), ("p", "preview", "preview"), ("a", "queue", "queue"),
                ("i", "import_batch", "import"), ("t", "retry", "retry"), ("s", "pause_batch", "pause/resume"),
                ("c", "cancel", "cancel"), ("w", "pause_worker", "pause/resume worker"), ("d", "delete", "delete"),
                ("l", "reload_libraries", "libraries"), ("f5", "refresh", "refresh")]

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.db = PlexDB(config.plex)
        self.queue = Queue(config.work_dir)
        self.library_names: dict[int, str] = {}
        self.library_rows: list[dict] = []
        self.selected_batch: str | None = None
        self.selected_worker: str | None = None
        self.summaries: list[dict] = []
        self.worker_rows: list[dict] = []

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Static(id="banner")
        yield Static(id="subtitle")
        with Vertical(id="overview", classes="panel") as overview:
            overview.border_title = "overview"
            yield Static(id="stats")
            yield ProgressBar(id="overall", total=100, show_eta=False)
        with Horizontal(id="main"):
            with VerticalScroll(id="queue-panel", classes="panel") as panel:
                panel.border_title = "queue work"
                yield Label("libraries  [dim](space toggles)[/]", classes="hint")
                yield SelectionList[int](id="libs")
                yield Label("added or replaced since / before  [dim]30d · 6m · 1y · YYYY-MM-DD[/]", classes="hint")
                with Horizontal(classes="row"):
                    yield Input(placeholder="since", id="since")
                    yield Input(placeholder="before", id="before")
                yield Label("release year  [dim]2001 · 1990-1999 · 2010-[/]", classes="hint")
                with Horizontal(classes="row"):
                    yield Input(placeholder="any year", id="year")
                    yield Input(placeholder="max files", id="limit", type="integer")
                yield Label("measurement", classes="hint")
                with RadioSet(id="mode"):
                    yield RadioButton("full   exact, same values as Plex", value=True, id="mode-full")
                    yield RadioButton("quick  24×30 s samples, ~0.7 dB off", id="mode-quick")
                yield Checkbox("include tracks Plex already analysed", id="include-done")
                yield Static("[dim]pick libraries and filters, then preview[/]", id="preview")
                with Horizontal(classes="buttons"):
                    yield Button("preview", id="preview-btn")
                    yield Button("queue batch", id="queue-btn", classes="go")
            with Vertical(id="right"):
                with Vertical(id="batches-panel", classes="panel") as panel:
                    panel.border_title = "batches"
                    yield DataTable(id="batches", cursor_type="row", zebra_stripes=True)
                    with Horizontal(classes="buttons"):
                        yield Button("import into plex", id="import-btn", classes="ok")
                        yield Button("retry failed", id="retry-btn")
                        yield Button("pause", id="pause-btn")
                        yield Button("cancel", id="cancel-btn", classes="danger")
                        yield Button("delete", id="delete-btn", classes="danger")
                with Vertical(id="now-panel", classes="panel") as panel:
                    panel.border_title = "now measuring"
                    yield DataTable(id="active", zebra_stripes=True)
                    yield DataTable(id="workers", cursor_type="row", zebra_stripes=True)
                    with Horizontal(classes="buttons"):
                        yield Button("pause worker", id="worker-btn")
        log = RichLog(id="log", wrap=True, markup=True)
        log.border_title = "activity"
        yield log
        yield Static(id="hints")

    def on_mount(self) -> None:
        self.query_one("#batches", DataTable).add_columns(
            "batch", "mode", "state", "tracks", "ok", "failed", "running", "audio h", "filter")
        self.query_one("#active", DataTable).add_columns("worker", "batch", "elapsed", "done", "mem", "title", "track")
        self.query_one("#workers", DataTable).add_columns("worker", "state", "busy", "cpu", "mem", "host",
                                                          "ok", "failed", "speed", "version")
        keys = [("q", "quit"), ("p", "preview"), ("a", "add batch"), ("i", "import"), ("t", "retry"),
                ("s", "pause/resume batch"), ("w", "pause/resume worker"), ("c", "cancel"), ("d", "delete"),
                ("l", "libraries"), ("tab", "next field")]
        self.query_one("#hints", Static).update(
            Text.from_markup("  ·  ".join(f"[bold {CYAN}]{k}[/] {v}" for k, v in keys)))
        self.draw_banner()
        self.log_line(f"config: {self.config.source or 'defaults'}  ·  work dir {self.config.work_dir}")
        self.action_reload_libraries()
        self.action_refresh()
        self.set_interval(3, self.action_refresh)

    def on_resize(self) -> None:
        self.draw_banner()

    def draw_banner(self) -> None:
        size = self.size
        self.query_one("#banner", Static).update(banner(size.width, size.height))
        self.query_one("#banner", Static).display = size.height >= 30

    def update_subtitle(self) -> None:
        alive = [w for w in self.worker_rows if w["alive"]]
        rate = sum(w.get("rate", 0) for w in alive)
        bits = [f"v{__version__}", f"plex: {self.db.describe()}",
                f"{len(alive)} worker{'s' if len(alive) != 1 else ''} · {sum(w.get('slots', 0) for w in alive)} slots",
                f"{rate:.0f}× real time" if rate else "idle", self.config.work_dir]
        self.query_one("#subtitle", Static).update(Text("  ·  ".join(bits), style="grey62", justify="center"))

    def log_line(self, text: str) -> None:
        self.query_one("#log", RichLog).write(Text.from_markup(f"[grey50]{time.strftime('%H:%M:%S')}[/]  {text}"))

    # ------------------------------------------------------------------ overview
    def update_overview(self) -> None:
        items = sum(r["items"] for r in self.library_rows)
        done = sum(r["done"] for r in self.library_rows)
        active = [s for s in self.summaries if s["state"] in ("running", "queued", "paused")]
        pending = sum(s["tracks"] - s["ok"] - s["failed"] for s in active)
        pending_h = sum(s["hours"] - s["hours_done"] for s in active)
        running = sum(s["running"] for s in self.summaries)
        failed = sum(s["failed"] for s in self.summaries if s["state"] != "imported")
        to_import = sum(s["ok"] for s in self.summaries if s["state"] in ("finished", "running", "queued", "paused"))
        alive = [w for w in self.worker_rows if w["alive"]]
        rate = sum(w.get("rate", 0) for w in alive)
        eta = human(pending_h * 3600 / rate) if rate and pending_h else "—"
        grid = Table.grid(expand=True, padding=(0, 2))
        for _ in range(4):
            grid.add_column(style="grey62", no_wrap=True, ratio=2)
            grid.add_column(justify="left", no_wrap=True, ratio=2)
        def cell(value, style="bold #e0e0e0") -> Text:
            return Text(str(value), style=style)

        grid.add_row("Analysed", cell(f"{done:,} / {items:,}" if items else "…"),
                     "Queued", cell(f"{pending:,} tracks", f"bold {VIOLET}"),
                     "Running", cell(running, f"bold {CYAN}"),
                     "Workers", cell(f"{len(alive)} alive", f"bold {MINT}" if alive else "bold #ff5f5f"))
        grid.add_row("Ready to import", cell(f"{to_import:,} tracks", f"bold {MINT}"),
                     "Audio left", cell(f"{pending_h:,.0f} h"),
                     "Failed", cell(failed, "bold #ff5f5f" if failed else "bold #e0e0e0"),
                     "ETA", cell(f"{eta}  ({rate:.0f}×)" if rate else eta))
        self.query_one("#stats", Static).update(grid)
        bar = self.query_one("#overall", ProgressBar)
        bar.update(total=max(items, 1), progress=done)

    # ------------------------------------------------------------------ libraries
    def action_reload_libraries(self) -> None:
        self.load_libraries()

    @work(thread=True, exclusive=True, group="libraries")
    def load_libraries(self) -> None:
        try:
            rows = libraries(self.db)
        except SystemExit as exc:
            self.call_from_thread(self.log_line, f"[#ff5f5f]plex database: {exc}[/]")
            return
        self.call_from_thread(self.show_libraries, rows)

    def show_libraries(self, rows: list[dict]) -> None:
        widget = self.query_one("#libs", SelectionList)
        chosen = set(widget.selected)
        widget.clear_options()
        self.library_rows = rows
        self.library_names = {row["id"]: row["name"] for row in rows}
        for row in rows:
            pct = 100 * row["done"] / row["items"] if row["items"] else 0
            label = Text.assemble((f"{row['name'][:22]:<22} ", "#e0e0e0"),
                                  (f"{row['done']:>6,}/{row['items']:<6,}", "grey62"),
                                  (f" {pct:3.0f}%", f"bold {MINT}" if pct >= 99.5 else f"bold {CYAN}"))
            widget.add_option((label, row["id"], row["id"] in chosen))
        self.update_overview()
        self.update_subtitle()
        self.log_line(f"{len(rows)} video libraries  ·  {self.db.describe()}")

    # ------------------------------------------------------------------ filter / preview / queue
    def current_filter(self) -> Filter:
        def value(widget_id: str) -> str:
            return self.query_one(f"#{widget_id}", Input).value

        year_from, year_to = parse_years(value("year"))
        limit = value("limit").strip()
        return Filter(sections=list(self.query_one("#libs", SelectionList).selected),
                      added_since=parse_when(value("since")), added_before=parse_when(value("before")),
                      year_from=year_from, year_to=year_to, limit=int(limit) if limit else 0,
                      include_done=self.query_one("#include-done", Checkbox).value)

    def current_mode(self):
        pressed = self.query_one("#mode", RadioSet).pressed_button
        return QUICK if pressed is not None and pressed.id == "mode-quick" else FULL

    def show_preview(self, text: str) -> None:
        self.query_one("#preview", Static).update(Text.from_markup(text))

    def action_preview(self) -> None:
        try:
            self.run_preview(self.current_filter(), self.current_mode())
        except ValueError as exc:
            self.show_preview(f"[#ff5f5f]{exc}[/]")

    @work(thread=True, exclusive=True, group="preview")
    def run_preview(self, flt: Filter, mode) -> None:
        self.call_from_thread(self.show_preview, "[grey62]counting…[/]")
        size = preview(self.db, flt)
        rate = sum(w.get("rate", 0) for w in self.queue.workers() if w["alive"])
        eta = f"\n[grey62]about[/] [bold]{human(size['hours'] * 3600 / rate)}[/] [grey62]at {rate:.0f}×[/]" if rate else ""
        text = (f"[bold {PINK}]{size['files']:,}[/] files  ·  [bold {CYAN}]{size['tracks']:,}[/] tracks  ·  "
                f"[bold]{size['hours']:,.0f} h[/] audio{eta}\n[grey50]{flt.describe()} · {mode.name}[/]")
        self.call_from_thread(self.show_preview, text)

    def action_queue(self) -> None:
        self.queue_batch()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_preview()

    def queue_batch(self) -> None:
        try:
            flt, mode = self.current_filter(), self.current_mode()
        except ValueError as exc:
            self.show_preview(f"[#ff5f5f]{exc}[/]")
            return
        self.create_batch(flt, mode)

    @work(thread=True, exclusive=True, group="queue")
    def create_batch(self, flt: Filter, mode) -> None:
        jobs = select_jobs(self.db, flt)
        tracks = sum(len(j["streams"]) for j in jobs)
        if not tracks:
            self.call_from_thread(self.log_line, "[yellow]nothing matches that filter[/]")
            return
        name = ", ".join(self.library_names.get(s, str(s)) for s in flt.sections) or "all libraries"
        batch = self.queue.create(name, jobs, mode.as_dict(), flt.describe(), created_by=os.uname().nodename)
        self.call_from_thread(self.log_line, f"queued [bold {PINK}]{batch.id}[/]  ·  {len(jobs):,} files  ·  "
                                             f"{tracks:,} tracks  ·  {mode.name}")
        self.call_from_thread(self.action_refresh)

    # ------------------------------------------------------------------ batches / workers
    def action_refresh(self) -> None:
        self.load_queue()

    @work(thread=True, exclusive=True, group="refresh")
    def load_queue(self) -> None:
        summaries = [b.summary() for b in self.queue.batches()[-40:]]
        workers = self.queue.workers()
        self.call_from_thread(self.show_queue, summaries, workers)

    def show_queue(self, summaries: list[dict], workers: list[dict]) -> None:
        self.summaries, self.worker_rows = summaries, workers
        table = self.query_one("#batches", DataTable)
        keep = self.selected_batch
        table.clear()
        ordered = list(reversed(summaries))
        alive = [w for w in workers if w["alive"]]
        held = bool(alive) and all(w.get("paused") or w.get("pause_requested") for w in alive)
        for s in ordered:
            done = s["ok"] + s["failed"]
            state = Text(s["state"], style=STATE_STYLE.get(s["state"], ""))
            if held and s["state"] in ("running", "queued"):
                state = Text("worker paused", style=f"bold {AMBER}")
            table.add_row(Text(s["id"], style="#e0e0e0"), Text(s["mode"], style="grey62"), state,
                          f"{s['tracks']:,}", Text(f"{s['ok']:,}", style=MINT if done else ""),
                          Text(f"{s['failed']:,}", style="#ff5f5f" if s["failed"] else "grey50"),
                          Text(str(s["running"]), style=CYAN if s["running"] else "grey50"),
                          f"{s['hours_done']:,.0f}/{s['hours']:,.0f}", Text(s["filter"][:30], style="grey62"),
                          key=s["id"])
        ids = [s["id"] for s in ordered]
        if keep in ids:
            table.move_cursor(row=ids.index(keep))
        active = self.query_one("#active", DataTable)
        active.clear()
        known = {s["id"] for s in summaries}
        wtable = self.query_one("#workers", DataTable)
        keep_worker = self.selected_worker
        wtable.clear()
        for w in workers:
            busy = len(w.get("active", [])) if w["alive"] else 0
            if not w["alive"]:
                state = Text(f"silent {human(time.time() - w['updated'])}", style="#ff5f5f")
            elif w.get("paused"):
                state = Text("paused", style=f"bold {AMBER}")
            elif w.get("pause_requested"):
                state = Text("pausing…", style=AMBER)      # marker set, worker hasn't seen it yet (or is older)
            else:
                state = Text("alive", style=f"bold {MINT}")
            res = (w.get("resources") or {}) if w["alive"] else {}
            cpu, mem, host = describe_resources(res)
            host_text = Text(host, style=f"bold {AMBER}" if overloaded(res) else "grey62")
            if overloaded(res):
                host_text.append("  overloaded", style=AMBER)
            wtable.add_row(Text(w["name"], style="bold #e0e0e0"), state, f"{busy}/{w.get('slots', '?')}",
                           Text(cpu, style=CYAN), Text(mem, style=CYAN), host_text,
                           Text(f"{w.get('ok', 0):,}", style=MINT), str(w.get("failed", 0)),
                           f"{w.get('rate', 0):.0f}×", Text(f"v{w.get('version', '?')}", style="grey62"), key=w["name"])
            if not w["alive"]:
                continue
            for a in w.get("active", []):
                pct = 100 * a["position"] / a["total"] if a.get("total") else 0
                done = Text(f"⏸ {pct:3.0f}%", style=f"bold {AMBER}") if a.get("paused") else \
                    Text(f"{pct:3.0f}%", style=f"bold {PINK}")
                batch_name = a.get("batch", "")
                batch_text = (Text(batch_name.split("-", 2)[-1][:18], style="grey62") if batch_name in known
                              else Text("deleted", style="#ff5f5f"))
                active.add_row(Text(w["name"], style=CYAN), batch_text, human(run_seconds(a)), done,
                               Text(f"{a.get('rss_mb', 0)} MB", style="grey62"), a["title"][:64],
                               Text(a["codec"], style="grey62"))
        names = [w["name"] for w in workers]
        if keep_worker in names:
            wtable.move_cursor(row=names.index(keep_worker))
        self.query_one("#now-panel").border_subtitle = (f"{active.row_count} running" if workers
                                                        else "no workers have reported in yet")
        self.query_one("#batches-panel").border_subtitle = f"{len(summaries)} batches"
        self.update_overview()
        self.update_subtitle()
        self.update_buttons()

    def update_buttons(self) -> None:
        """pause turns into resume for a paused or cancelled batch; cancel is off once cancelled."""
        s = next((s for s in self.summaries if s["id"] == self.selected_batch), None)
        state = s["state"] if s else ""
        pause = self.query_one("#pause-btn", Button)
        pause.label = "resume" if state in ("paused", "cancelled") else "pause"
        pause.disabled = state in ("finished", "imported")
        self.query_one("#cancel-btn", Button).disabled = state in ("cancelled", "finished", "imported")
        worker = self.current_worker()
        button = self.query_one("#worker-btn", Button)
        button.label = f"{'resume' if worker and self.queue.worker_paused(worker) else 'pause'} worker" + \
            (f" {worker}" if worker else "")
        button.disabled = worker is None

    def current_worker(self) -> str | None:
        """The highlighted worker, or the only one there is."""
        names = [w["name"] for w in self.worker_rows]
        if self.selected_worker in names:
            return self.selected_worker
        return names[0] if len(names) == 1 else None

    def warn_paused_workers(self) -> None:
        for name in self.queue.paused_workers():
            self.log_line(f"[bold {AMBER}]worker {name} is paused itself[/], so its tracks stay frozen: "
                          f"press [bold {CYAN}]resume worker[/] (w) under now measuring")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "batches" and event.row_key is not None:
            self.selected_batch = event.row_key.value
            self.update_buttons()
        elif event.data_table.id == "workers" and event.row_key is not None:
            self.selected_worker = event.row_key.value
            self.update_buttons()

    def selected(self):
        if not self.selected_batch:
            self.log_line("[yellow]select a batch first[/]")
            return None
        return self.queue.get(self.selected_batch)

    # ------------------------------------------------------------------ batch actions
    def action_import_batch(self) -> None:
        batch = self.selected()
        if batch:
            self.plan_batch_import(batch)

    @work(thread=True, exclusive=True, group="import")
    def plan_batch_import(self, batch) -> None:
        plan = plan_import(self.db, list(batch.results().values()))
        if not plan.statements:
            self.call_from_thread(self.log_line, f"{batch.id}: nothing to import ({plan.describe()})")
            return
        message = (f"[bold]{batch.id}[/]\n\n{plan.describe()}.\n\n[grey62]The Plex database is backed up first, "
                   "then written in small transactions while Plex keeps running.[/]")

        def answered(yes: bool) -> None:
            if yes:
                self.run_import(batch, plan)

        self.call_from_thread(self.push_screen, Confirm(message, yes=f"import {plan.write:,}", title="import into plex"),
                              answered)

    @work(thread=True, exclusive=True, group="import")
    def run_import(self, batch, plan) -> None:
        self.call_from_thread(self.log_line, f"backing up the database and importing {plan.write:,} tracks…")
        result = apply_import(self.db, plan, progress=lambda text: self.call_from_thread(self.log_line, text.strip()))
        batch.mark_imported({"written": result["written"], "backup": result["backup"]})
        colour = MINT if result["verified"] == result["written"] else "#ff5f5f"
        self.call_from_thread(self.log_line, f"[bold {colour}]imported {result['verified']:,}/{result['written']:,} "
                                             f"tracks[/]  [grey50]backup {result['backup']}[/]")
        self.call_from_thread(self.action_reload_libraries)

    def action_retry(self) -> None:
        batch = self.selected()
        if batch:
            self.log_line(f"{batch.id}: {batch.retry_failed()} failed tracks queued again")
            self.action_refresh()

    def action_pause_batch(self) -> None:
        batch = self.selected()
        if not batch:
            return
        if batch.paused or batch.cancelled:
            batch.resume()
            self.log_line(f"resumed {batch.id} [grey50](frozen tracks carry on)[/]")
            self.warn_paused_workers()
        else:
            batch.pause()
            self.log_line(f"paused {batch.id} [grey50](running tracks freeze within a few seconds, progress kept)[/]")
        self.action_refresh()

    def action_pause_worker(self) -> None:
        name = self.current_worker()
        if not name:
            self.log_line("[yellow]select a worker first (in the list under now measuring)[/]")
            return
        if self.queue.worker_paused(name):
            self.queue.resume_worker(name)
            self.log_line(f"resumed worker {name} [grey50](frozen tracks carry on)[/]")
        else:
            self.queue.pause_worker(name)
            self.log_line(f"paused worker {name} [grey50](frees its CPU; tracks stay claimed and carry on when resumed)[/]")
        self.action_refresh()

    def action_cancel(self) -> None:
        batch = self.selected()
        if not batch:
            return
        if batch.cancelled:
            self.log_line(f"{batch.id} is already cancelled [grey50](resume it with s / the resume button)[/]")
            return

        def answered(yes: bool) -> None:
            if yes:
                batch.cancel()
                self.log_line(f"cancelled {batch.id} [grey50](running tracks finish; resume picks it up again)[/]")
                self.action_refresh()

        self.push_screen(Confirm(f"Cancel [bold]{batch.id}[/]?\n\n[grey62]Running tracks finish and no new ones "
                                 "start. Results so far are kept, and resume picks the batch up again.[/]",
                                 yes="cancel batch", no="keep it", title="cancel batch", danger=True), answered)

    def action_delete(self) -> None:
        batch = self.selected()
        if not batch:
            return

        def answered(yes: bool) -> None:
            if yes:
                self.queue.delete(batch)
                self.selected_batch = None
                self.log_line(f"deleted {batch.id}")
                self.action_refresh()

        self.push_screen(Confirm(f"Delete [bold]{batch.id}[/] and its results?", yes="delete", title="delete batch"),
                         answered)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {"preview-btn": self.action_preview, "queue-btn": self.queue_batch,
                   "import-btn": self.action_import_batch, "retry-btn": self.action_retry,
                   "pause-btn": self.action_pause_batch, "cancel-btn": self.action_cancel,
                   "delete-btn": self.action_delete, "worker-btn": self.action_pause_worker}
        if event.button.id in actions:
            actions[event.button.id]()


def run(config: Config) -> int:
    LoudnessApp(config).run()
    return 0
