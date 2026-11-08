"""The plex-loudness controller TUI (Textual)."""

from __future__ import annotations

import os
import time

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (Button, Checkbox, DataTable, Footer, Header, Input, Label, RadioButton, RadioSet,
                             RichLog, SelectionList, Static)

from . import __version__
from .analysis import FULL, QUICK
from .batches import Queue
from .catalog import Filter, libraries, parse_when, parse_years, preview, select_jobs
from .config import Config
from .importer import apply_import, plan_import
from .plexdb import PlexDB


def human(seconds: float) -> str:
    seconds = int(seconds or 0)
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    if seconds < 86400 * 2:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds / 86400:.1f}d"


class Confirm(ModalScreen[bool]):
    DEFAULT_CSS = """
    Confirm { align: center middle; }
    #dialog { width: 72; height: auto; padding: 1 2; border: thick $warning; background: $surface; }
    #dialog Horizontal { height: auto; margin-top: 1; align-horizontal: right; }
    #dialog Button { margin-left: 2; }
    """
    BINDINGS = [("escape", "no", "Cancel")]

    def __init__(self, message: str, yes: str = "Yes"):
        super().__init__()
        self.message, self.yes = message, yes

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self.message)
            with Horizontal():
                yield Button("Cancel", id="no")
                yield Button(self.yes, id="yes", variant="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_no(self) -> None:
        self.dismiss(False)


class LoudnessApp(App):
    TITLE = "plex-loudness"
    CSS = """
    #left { width: 58; border-right: solid $panel; padding: 0 1; }
    #left Label { margin-top: 1; color: $text-muted; }
    #libs { height: auto; max-height: 12; }
    #preview { margin-top: 1; height: auto; color: $accent; }
    .row { height: auto; }
    .row Input { width: 1fr; }
    .buttons { height: auto; margin-top: 1; }
    .buttons Button { margin-right: 1; min-width: 10; }
    #right { padding: 0 1; }
    .title { text-style: bold; margin-top: 1; }
    #batches { height: 12; }
    #active { height: 1fr; min-height: 6; }
    #workers { height: auto; color: $text-muted; }
    #log { height: 8; border-top: solid $panel; }
    """
    BINDINGS = [("q", "quit", "Quit"), ("r", "reload_libraries", "Reload libraries"),
                ("p", "preview", "Preview"), ("i", "import_batch", "Import"), ("f5", "refresh", "Refresh")]

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.db = PlexDB(config.plex)
        self.queue = Queue(config.work_dir)
        self.library_names: dict[int, str] = {}
        self.selected_batch: str | None = None

    # ---- layout
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with VerticalScroll(id="left"):
                yield Label("Libraries (space to toggle)")
                yield SelectionList[int](id="libs")
                yield Label("Added since / before  (30d, 6m, 1y, YYYY-MM-DD)")
                with Horizontal(classes="row"):
                    yield Input(placeholder="since", id="since")
                    yield Input(placeholder="before", id="before")
                yield Label("Release year  (2001, 1990-1999, 2010-)")
                yield Input(placeholder="any year", id="year")
                yield Label("Limit files  (empty = all; newest first)")
                yield Input(placeholder="all", id="limit", type="integer")
                yield Label("Measurement")
                with RadioSet(id="mode"):
                    yield RadioButton("Full: exact, same values as Plex", value=True, id="mode-full")
                    yield RadioButton("Quick: 24x30 s samples, ~0.7 dB off", id="mode-quick")
                yield Checkbox("Include tracks Plex already analysed", id="include-done")
                yield Static("[dim]Pick libraries and filters, then Preview[/]", id="preview")
                with Horizontal(classes="buttons"):
                    yield Button("Preview", id="preview-btn")
                    yield Button("Queue batch", id="queue-btn", variant="primary")
            with Vertical(id="right"):
                yield Static("Batches", classes="title")
                yield DataTable(id="batches", cursor_type="row", zebra_stripes=True)
                with Horizontal(classes="buttons"):
                    yield Button("Import into Plex", id="import-btn", variant="warning")
                    yield Button("Retry failed", id="retry-btn")
                    yield Button("Cancel / resume", id="cancel-btn")
                    yield Button("Delete", id="delete-btn", variant="error")
                yield Static("Running now", classes="title")
                yield DataTable(id="active", zebra_stripes=True)
                yield Static("", id="workers")
        yield RichLog(id="log", wrap=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"v{__version__} | work dir {self.config.work_dir}"
        self.query_one("#batches", DataTable).add_columns(
            "Batch", "Mode", "State", "Tracks", "OK", "Failed", "Running", "Audio h", "Filter")
        self.query_one("#active", DataTable).add_columns("Worker", "Elapsed", "Done", "Mem", "Title", "Track")
        self.log_line(f"Config: {self.config.source or 'defaults'}")
        self.action_reload_libraries()
        self.action_refresh()
        self.set_interval(3, self.action_refresh)

    def log_line(self, text: str) -> None:
        self.query_one("#log", RichLog).write(f"[dim]{time.strftime('%H:%M:%S')} |[/] {text}")

    # ---- libraries
    def action_reload_libraries(self) -> None:
        self.load_libraries()

    @work(thread=True, exclusive=True, group="libraries")
    def load_libraries(self) -> None:
        try:
            rows = libraries(self.db)
        except SystemExit as exc:
            self.call_from_thread(self.log_line, f"[red]Plex database: {exc}[/]")
            return
        self.call_from_thread(self.show_libraries, rows)

    def show_libraries(self, rows: list[dict]) -> None:
        widget = self.query_one("#libs", SelectionList)
        chosen = set(widget.selected)
        widget.clear_options()
        self.library_names = {row["id"]: row["name"] for row in rows}
        for row in rows:
            pct = 100 * row["done"] / row["items"] if row["items"] else 0
            label = f"{row['name'][:22]:<22} {row['done']:>5}/{row['items']:<5} {pct:3.0f}%"
            widget.add_option((label, row["id"], row["id"] in chosen))
        self.log_line(f"{len(rows)} video libraries loaded")

    # ---- filter / preview / queue
    def current_filter(self) -> Filter:
        value = lambda wid: self.query_one(f"#{wid}", Input).value
        year_from, year_to = parse_years(value("year"))
        limit = value("limit").strip()
        return Filter(sections=list(self.query_one("#libs", SelectionList).selected),
                      added_since=parse_when(value("since")), added_before=parse_when(value("before")),
                      year_from=year_from, year_to=year_to, limit=int(limit) if limit else 0,
                      include_done=self.query_one("#include-done", Checkbox).value)

    def current_mode(self):
        pressed = self.query_one("#mode", RadioSet).pressed_button
        return QUICK if pressed is not None and pressed.id == "mode-quick" else FULL

    def action_preview(self) -> None:
        try:
            self.run_preview(self.current_filter(), self.current_mode())
        except ValueError as exc:
            self.query_one("#preview", Static).update(f"[red]{exc}[/]")

    @work(thread=True, exclusive=True, group="preview")
    def run_preview(self, flt: Filter, mode) -> None:
        self.call_from_thread(self.query_one("#preview", Static).update, "Counting...")
        size = preview(self.db, flt)
        rate = sum(w.get("rate", 0) for w in self.queue.workers() if w["alive"])
        eta = f", about {human(size['hours'] * 3600 / rate)} at the current {rate:.0f}x" if rate else ""
        text = (f"{size['files']:,} files, {size['tracks']:,} audio tracks, {size['hours']:,.0f} h of audio{eta}\n"
                f"[dim]{flt.describe()}; mode {mode.name}[/]")
        self.call_from_thread(self.query_one("#preview", Static).update, text)

    def queue_batch(self) -> None:
        try:
            flt, mode = self.current_filter(), self.current_mode()
        except ValueError as exc:
            self.query_one("#preview", Static).update(f"[red]{exc}[/]")
            return
        self.create_batch(flt, mode)

    @work(thread=True, exclusive=True, group="queue")
    def create_batch(self, flt: Filter, mode) -> None:
        jobs = select_jobs(self.db, flt)
        tracks = sum(len(j["streams"]) for j in jobs)
        if not tracks:
            self.call_from_thread(self.log_line, "Nothing matches that filter")
            return
        name = ", ".join(self.library_names.get(s, str(s)) for s in flt.sections) or "all libraries"
        batch = self.queue.create(name, jobs, mode.as_dict(), flt.describe(), created_by=os.uname().nodename)
        self.call_from_thread(self.log_line, f"Queued [b]{batch.id}[/]: {len(jobs)} files, {tracks} tracks ({mode.name})")
        self.call_from_thread(self.action_refresh)

    # ---- batches / workers
    def action_refresh(self) -> None:
        self.load_queue()

    @work(thread=True, exclusive=True, group="refresh")
    def load_queue(self) -> None:
        summaries = [b.summary() for b in self.queue.batches()[-30:]]
        workers = self.queue.workers()
        self.call_from_thread(self.show_queue, summaries, workers)

    def show_queue(self, summaries: list[dict], workers: list[dict]) -> None:
        table = self.query_one("#batches", DataTable)
        keep = self.selected_batch
        table.clear()
        for s in reversed(summaries):
            table.add_row(s["id"], s["mode"], s["state"], s["tracks"], s["ok"], s["failed"], s["running"],
                          f"{s['hours_done']:.0f}/{s['hours']:.0f}", s["filter"][:40], key=s["id"])
        ids = [s["id"] for s in reversed(summaries)]
        if keep in ids:
            table.move_cursor(row=ids.index(keep))
        active = self.query_one("#active", DataTable)
        active.clear()
        lines = []
        for w in workers:
            busy = len(w.get("active", [])) if w["alive"] else 0
            state = "[green]alive[/]" if w["alive"] else f"[red]silent {human(time.time() - w['updated'])}[/]"
            lines.append(f"{w['name']}: {state}, {busy}/{w.get('slots', '?')} busy, {w.get('ok', 0)} ok, "
                         f"{w.get('failed', 0)} failed, {w.get('rate', 0):.0f}x real time")
            if not w["alive"]:
                continue
            for a in w.get("active", []):
                pct = f"{100 * a['position'] / a['total']:.0f}%" if a.get("total") else "?"
                active.add_row(w["name"], human(time.time() - a["started"]), pct, f"{a.get('rss_mb', 0)} MB",
                               a["title"][:60], a["codec"])
        self.query_one("#workers", Static).update("\n".join(lines) or "[yellow]No workers have reported in yet[/]")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "batches" and event.row_key is not None:
            self.selected_batch = event.row_key.value

    def require_batch(self):
        if not self.selected_batch:
            self.log_line("[yellow]Select a batch first[/]")
            return None
        return self.queue.get(self.selected_batch)

    # ---- actions on the selected batch
    def action_import_batch(self) -> None:
        batch = self.require_batch()
        if batch:
            self.plan_batch_import(batch)

    @work(thread=True, exclusive=True, group="import")
    def plan_batch_import(self, batch) -> None:
        plan = plan_import(self.db, list(batch.results().values()))
        if not plan.statements:
            self.call_from_thread(self.log_line, f"{batch.id}: nothing to import ({plan.describe()})")
            return
        message = (f"Import {batch.id}?\n\n{plan.describe()}.\n\nThe Plex database is backed up first, "
                   "then written in small transactions while Plex keeps running.")

        def answered(yes: bool) -> None:
            if yes:
                self.run_import(batch, plan)

        self.call_from_thread(self.push_screen, Confirm(message, yes=f"Import {plan.write}"), answered)

    @work(thread=True, exclusive=True, group="import")
    def run_import(self, batch, plan) -> None:
        self.call_from_thread(self.log_line, f"Backing up and importing {plan.write} tracks...")
        result = apply_import(self.db, plan, progress=lambda text: self.call_from_thread(self.log_line, text))
        batch.mark_imported({"written": result["written"], "backup": result["backup"]})
        colour = "green" if result["verified"] == result["written"] else "red"
        self.call_from_thread(self.log_line, f"[{colour}]Imported {result['verified']}/{result['written']} tracks[/]"
                                             f" (backup {result['backup']})")
        self.call_from_thread(self.action_reload_libraries)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button = event.button.id
        if button == "preview-btn":
            self.action_preview()
        elif button == "queue-btn":
            self.queue_batch()
        elif button == "import-btn":
            self.action_import_batch()
        elif button in ("retry-btn", "cancel-btn", "delete-btn"):
            batch = self.require_batch()
            if not batch:
                return
            if button == "retry-btn":
                self.log_line(f"{batch.id}: {batch.retry_failed()} failed tracks queued again")
            elif button == "cancel-btn":
                if batch.cancelled:
                    batch.resume()
                    self.log_line(f"Resumed {batch.id}")
                else:
                    batch.cancel()
                    self.log_line(f"Cancelled {batch.id} (running tracks finish)")
            else:
                def answered(yes: bool) -> None:
                    if yes:
                        self.queue.delete(batch)
                        self.selected_batch = None
                        self.log_line(f"Deleted {batch.id}")
                        self.action_refresh()
                self.push_screen(Confirm(f"Delete {batch.id} and its results?", yes="Delete"), answered)
            self.action_refresh()


def run(config: Config) -> int:
    LoudnessApp(config).run()
    return 0
