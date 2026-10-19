from __future__ import annotations

import asyncio
import plistlib
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Label, ProgressBar, Static

from .actions import ActionRunner
from .cache import REGIONS, ScanCache
from .models import ScanNode
from .scanner import Scanner
from .utils import gdu_size, human_size


class ConfirmScreen(ModalScreen[bool]):
    CSS = """
    ConfirmScreen { align: center middle; background: rgba(0,0,0,0.78); }
    #dialog { width: 84; height: auto; max-height: 26; background: #111722;
              border: round #4b5b73; padding: 1 2; }
    #title { text-style: bold; margin-bottom: 1; }
    #buttons { height: 3; margin-top: 1; align-horizontal: right; }
    #buttons Button { min-width: 12; margin-left: 1; }
    """

    def __init__(self, title: str, message: str, destructive: bool = False):
        super().__init__()
        self.dialog_title = title
        self.message = message
        self.destructive = destructive

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.dialog_title, id="title")
            yield Static(self.message)
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(
                    "Confirm", id="confirm",
                    variant="error" if self.destructive else "success",
                )

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "cancel":
            self.dismiss(False)
        elif event.button.id == "confirm":
            self.dismiss(True)


class CleanerApp(App):
    TITLE = "MacDiskScanner"

    CSS = """
    Screen { background: #0b0e15; color: #e6ebf4; }

    #topbar { height: 1; background: #111622; padding: 0 2; }
    #topbar_title { width: 1fr; }
    #topbar_disk { width: auto; color: #8b97ab; }

    #body { height: 1fr; }

    #sidebar { width: 32; background: #0e121b; border-right: vkey #1f2736; padding: 1 1; }
    .side-label { height: 2; color: #5d6a80; text-style: bold; padding: 0 1; }
    .nav { width: 100%; height: 1; min-width: 0; margin-bottom: 1; padding: 0 1;
           border: none; background: #0e121b; color: #9aa5b8;
           content-align: left middle; text-align: left; text-style: none; }
    .nav:hover { background: #151c2c; }
    .nav:focus { text-style: none; }
    .nav.active { background: #18213a; color: #ffffff; text-style: bold; }
    #spacer { height: 1fr; }
    #disk_card { height: auto; padding: 1 1; background: #121826; border: round #222b3c; }

    #center { width: 1fr; min-width: 60; }
    #section { height: 4; padding: 1 2 0 2; }
    #section_text { width: 1fr; }
    #section_title { height: 1; }
    #section_subtitle { height: 1; color: #8b97ab; }
    #rescan_region { width: auto; min-width: 0; height: 1; margin-top: 1; padding: 0 1;
                     border: none; background: #1b2335; color: #c9d3e3; text-style: bold; }
    #rescan_region:hover { background: #243050; }

    #tree { height: 1fr; margin: 0 2 1 2; background: #0e121b; border: round #1f2736;
            scrollbar-size-horizontal: 0; }
    #tree > .datatable--header { background: #0e121b; color: #6b778c; text-style: bold; }
    #tree > .datatable--cursor { background: #1d2c4a; }
    #tree > .datatable--hover { background: #141b2a; }

    #right { width: 42; background: #0e121b; border-left: vkey #1f2736; padding: 1 2; }
    #details_title { height: auto; text-style: bold; }
    #details_path { height: auto; color: #6b778c; margin-bottom: 1; }
    #details_body { height: 1fr; }
    #actions { height: auto; }
    #actions Button { width: 100%; height: 3; margin-top: 1; border: none; text-style: bold; }
    #run_action.danger { background: #b8323f; color: #ffffff; }
    #run_action.safe { background: #1f7a4d; color: #ffffff; }
    #run_action:disabled { background: #1b2335; color: #6b778c; }
    #finder, #terminal { background: #1b2335; color: #c9d3e3; }

    #statusbar { height: 1; background: #111622; padding: 0 2; }
    #status { width: 1fr; color: #8b97ab; }
    #progress { width: 32; margin: 0 2; }
    #progress Bar { width: 25; }
    #progress Bar > .bar--bar { color: #4f8cff; background: #1b2335; }
    #progress Bar > .bar--complete { color: #3ecf8e; background: #1b2335; }
    #progress PercentageStatus { width: 5; color: #8b97ab; }
    #hints { width: auto; color: #6b778c; }
    """

    BINDINGS = [
        ("enter", "toggle_expand", "Expand / scan"),
        ("left", "collapse", "Collapse"),
        ("right", "expand", "Expand"),
        ("d", "run_action", "Run action"),
        ("r", "refresh_selected", "Refresh selected"),
        ("m", "measure_sizes", "Measure exact sizes"),
        ("shift+r", "force_full_scan", "Full scan"),
        ("q", "quit", "Quit"),
    ]

    NAV = ("workspaces", "worktrees", "temporary", "caches", "home")

    NAV_LABELS = {
        "workspaces": "Workspaces",
        "worktrees": "Worktrees",
        "temporary": "Temporary",
        "caches": "Caches",
        "home": "Home",
    }

    # region -> (scanner method name, display label)
    SCAN_METHODS = {
        "workspaces": ("scan_workspaces", "Scanning workspaces"),
        "worktrees": ("scan_worktrees", "Scanning git worktrees"),
        "temporary": ("scan_private_tmp", "Scanning /private/tmp"),
        "caches": ("scan_known_caches", "Scanning known caches"),
        "home": ("scan_home", "Scanning Home (full)"),
    }

    # Regions scanned automatically at startup (Home stays manual-only).
    QUICK_REGIONS = ("workspaces", "worktrees", "temporary", "caches")

    def __init__(self):
        super().__init__()
        self.cache = ScanCache()
        self.runner = ActionRunner()
        self.region_roots: dict[str, Optional[ScanNode]] = {
            region: None for region in REGIONS
        }
        self.visible_nodes: list[ScanNode] = []
        self.active_category = "workspaces"
        self.expanded: set[str] = set()
        self.selected_path: Optional[str] = None

        # Lazy expansion must be instant, so sizes of newly discovered
        # directories are calculated afterwards in the background.
        # Only one size scan runs at a time to avoid hammering the SSD.
        self._size_semaphore = asyncio.Semaphore(1)
        self._sizing_parents: dict[str, asyncio.Task] = {}

        # A clean/delete operation modifies the same filesystem that background
        # size jobs inspect. Keep one operation at a time and pause conflicting UI.
        self._operation_running = False
        self._active_operation_path: Optional[str] = None

        # Guards concurrent region/full scans (UI stays browsable meanwhile).
        self._scanning_regions: Optional[list[str]] = None

        # Expanding the UI remains available while cargo clean/delete runs.
        # New expensive size calculations are deferred until the file operation
        # finishes so they don't compete for disk I/O.
        self._deferred_size_parents: set[str] = set()

        self._busy = False
        self._busy_since = 0.0
        self._status_text = "Ready."
        self._disk_task: Optional[asyncio.Task] = None
        self._snapshots: tuple[float, Optional[int]] = (0.0, None)

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static(
                Text.assemble(("MacDiskScanner", "bold #ffffff"),
                              ("  developer disk cleaner", "#6b778c")),
                id="topbar_title",
            )
            yield Static("", id="topbar_disk")

        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("REGIONS", classes="side-label")
                for key in self.NAV:
                    yield Button(
                        self.NAV_LABELS[key],
                        id=f"nav_{key}",
                        classes="nav active" if key == self.active_category else "nav",
                    )
                yield Static("", id="spacer")
                yield Static("", id="disk_card")

            with Vertical(id="center"):
                with Horizontal(id="section"):
                    with Vertical(id="section_text"):
                        yield Static("", id="section_title")
                        yield Static("", id="section_subtitle")
                    yield Button("↻ Rescan", id="rescan_region")
                yield DataTable(id="tree", cursor_type="row")

            with Vertical(id="right"):
                yield Static("Select an item", id="details_title")
                yield Static("", id="details_path")
                yield Static("", id="details_body")
                with Vertical(id="actions"):
                    yield Button("", id="run_action")
                    yield Button("Open in Finder", id="finder")
                    yield Button("Open in Terminal", id="terminal")

        with Horizontal(id="statusbar"):
            yield Static("Ready.", id="status")
            yield ProgressBar(total=100, show_eta=False, id="progress")
            yield Static(
                Text.assemble(
                    *[
                        part
                        for key, label in (
                            ("↵", "open"), ("←→", "fold"), ("d", "action"),
                            ("r", "refresh"), ("R", "full scan"), ("q", "quit"),
                        )
                        for part in ((key, "bold #c9d3e3"), (f" {label}  ", "#6b778c"))
                    ]
                ),
                id="hints",
            )

    def on_mount(self):
        # App.on_mount may run before every child widget is queryable.
        # Defer the first storage-card update until the initial UI refresh.
        self.call_after_refresh(
            self.update_disk_card,
        )

        # Keep storage values live afterwards.
        self.set_interval(
            5.0,
            self.update_disk_card,
        )
        self.set_interval(1.0, self._tick_status)

        pending = []
        stale = []

        for region in self.QUICK_REGIONS:
            cached = self.cache.load_region(region, allow_stale=True)
            if cached:
                roots, meta = cached
                self.region_roots[region] = roots[0] if roots else None
                if meta["stale"]:
                    stale.append(region)
            else:
                pending.append(region)

        cached = self.cache.load_region("home", allow_stale=True)
        if cached:
            roots, meta = cached
            self.region_roots["home"] = roots[0] if roots else None
            if meta["stale"]:
                stale.append("home")

        self.set_default_expansion()
        self.refresh_ui()
        # Column widths depend on the laid-out table width.
        self.call_after_refresh(self.refresh_table)

        if pending:
            self.set_progress(
                5,
                f"Scanning quick regions: {', '.join(pending)}…",
            )
            self.set_status(
                "Quick region scans are running in the background. "
                "Home (full scan) is manual only."
            )
            asyncio.create_task(self.scan_regions(pending))
        elif stale:
            self.set_progress(100, "Loaded cached results (some regions stale).")
            self.set_status(
                "Some regions are older than 30 minutes. "
                "Use per-region 重新扫描 or the full scan to refresh them."
            )
        else:
            self.set_progress(100, "Loaded cached results.")
            self.set_status(
                "All regions are fresh. Home full scan stays manual."
            )
        self.schedule_workspace_sizing()

    # ---------- lookup ----------

    def main_widget(self, selector, expect_type=None):
        # App.query_one searches the active screen; while a ConfirmScreen is
        # on top, timers and background tasks would fail to find the main
        # widgets. They all live on the bottom (default) screen.
        return self.screen_stack[0].query_one(selector, expect_type)

    def all_nodes(self):
        for root in self.all_region_roots():
            yield from root.walk()

    def all_region_roots(self):
        return [r for r in self.region_roots.values() if r]

    def find_node(self, path: Optional[str]):
        if not path:
            return None
        for node in self.all_nodes():
            if node.path == path:
                return node
        return None

    def root_by_kind(self, kind: str):
        return self.region_roots.get(kind)

    def category_roots(self):
        root = self.root_by_kind(self.active_category)
        return [root] if root else []

    def current_node(self):
        table = self.main_widget("#tree", DataTable)
        row = table.cursor_row
        if row < 0 or row >= len(self.visible_nodes):
            return None
        return self.visible_nodes[row]

    # ---------- progress / size ----------

    def set_progress(self, pct: int, text: str, busy: Optional[bool] = None):
        pct = max(0, min(100, pct))
        busy = pct < 100 if busy is None else busy
        if busy and not self._busy:
            self._busy_since = time.monotonic()
        self._busy = busy
        self.main_widget("#progress", ProgressBar).update(progress=pct)
        self.set_status(text)

    def set_status(self, text: str):
        self._status_text = str(text).replace("\n", " ")
        self._render_status()

    def _render_status(self):
        line = Text()
        if self._busy:
            line.append("⟳ ", style="bold #4f8cff")
        line.append(self._status_text)
        if self._busy:
            # Some steps (e.g. one GDU pass over Home) report no progress for
            # tens of seconds; the elapsed time shows the work is alive.
            elapsed = int(time.monotonic() - self._busy_since)
            line.append(f" · {elapsed}s", style="#6b778c")
        self.main_widget("#status", Static).update(line)

    def _tick_status(self):
        if self._busy:
            self._render_status()

    def make_progress_scanner(self):
        def progress(pct: int, message: str):
            self.call_from_thread(self.set_progress, pct, message)
        return Scanner(progress=progress)

    def node_size(self, node: ScanNode) -> int:
        # Virtual groups (Workspaces, Caches, Worktrees) have no size of
        # their own; show what their measured members add up to.
        if node.metadata.get("virtual"):
            return sum(
                c.size for c in node.children
                if not c.metadata.get("size_pending", False)
            )
        return node.size

    def display_size(self, node: ScanNode):
        # "…" means discovered but background size calculation has not
        # completed yet. It is not zero and it is not unknown forever.
        if node.metadata.get("size_pending", False):
            return "…"
        return human_size(self.node_size(node))

    # ---------- background directory sizing ----------

    def schedule_child_sizing(self, parent_path: str):
        """
        Start/continue measuring direct children.

        A clean/delete operation itself is already disk-heavy. During it,
        directory browsing remains interactive, but expensive background gdu
        jobs are queued and resumed automatically afterwards.
        """
        if self._operation_running:
            self._deferred_size_parents.add(parent_path)
            return

        existing = self._sizing_parents.get(parent_path)
        if existing and not existing.done():
            return

        task = asyncio.create_task(
            self._measure_pending_children(parent_path)
        )
        self._sizing_parents[parent_path] = task

        def finished(_task):
            current = self._sizing_parents.get(parent_path)
            if current is _task:
                self._sizing_parents.pop(parent_path, None)

        task.add_done_callback(finished)

    def flush_deferred_sizing(self):
        pending = list(self._deferred_size_parents)
        self._deferred_size_parents.clear()

        for parent_path in pending:
            node = self.find_node(parent_path)
            if node and node.children:
                self.schedule_child_sizing(parent_path)

    async def _measure_pending_children(self, parent_path: str):
        parent = self.find_node(parent_path)
        if not parent:
            return

        pending_paths = [
            child.path
            for child in parent.children
            if child.metadata.get("real_path", False)
            and not child.metadata.get("virtual", False)
        ]

        if not pending_paths:
            self.set_status(
                "No real direct children to measure."
            )
            return

        self.set_progress(
            20,
            f"Exact child sizing {parent.name} "
            f"({len(pending_paths)} direct children)…",
        )
        self.set_status(
            "One background GDU pass is calculating all direct-child sizes."
        )

        scanner = Scanner()

        try:
            async with self._size_semaphore:
                self.set_progress(35, f"Exact child sizing {parent.name}…")
                sizes = await asyncio.to_thread(
                    scanner.measure_children_sizes,
                    parent,
                )

        except asyncio.CancelledError:
            return
        except Exception as exc:
            self.set_progress(
                100,
                f"Exact child size scan failed: {parent.name}",
            )
            self.set_status(
                f"Could not calculate sizes under {parent.name}: {exc}"
            )
            return

        updated = 0

        for path in pending_paths:
            live = self.find_node(path)
            if not live:
                continue

            try:
                key = str(Path(path).resolve())
            except Exception:
                key = path

            size = sizes.get(key)
            if size is None:
                continue

            live.size = size
            live.scanned_at = time.time()
            live.metadata["size_pending"] = False
            updated += 1

        self.save_cache()
        self.refresh_table()

        self.set_progress(
            100,
            f"Exact sizes updated {updated}/{len(pending_paths)} "
            f"children under {parent.name}",
        )
        self.set_status(
            "Exact direct-child size calculation finished."
        )

    # ---------- clean/delete operation state ----------

    def cancel_sizing_under(self, root_path: str):
        prefix = root_path.rstrip("/") + "/"
        for parent_path, task in list(self._sizing_parents.items()):
            if parent_path == root_path or parent_path.startswith(prefix):
                task.cancel()
                self._sizing_parents.pop(parent_path, None)

    def set_operation_controls(
        self,
        running: bool,
        label: str = "",
    ):
        """
        File operations run in the background.

        While one is active:
        - block starting another clean/delete
        - KEEP table navigation and expand/collapse usable
        """
        self._operation_running = running

        if running:
            action_btn = self.main_widget("#run_action", Button)
            action_btn.remove_class("danger", "safe")
            action_btn.display = True
            action_btn.disabled = True
            action_btn.label = label or "Running in background…"
            return

        self.update_action_buttons(self.current_node())
        self.flush_deferred_sizing()

    def is_under_active_operation(self, path: str) -> bool:
        if not self._operation_running or not self._active_operation_path:
            return False

        active = self._active_operation_path.rstrip("/")
        candidate = path.rstrip("/")

        return (
            candidate == active
            or candidate.startswith(active + "/")
        )

    def action_progress_from_worker(self, message: str):
        # Project commands do not expose a meaningful percentage. Keep a clear
        # non-final "running" state and show the exact current stage.
        self.set_progress(45, message)
        self.set_status(message)

    def make_action_progress_callback(self):
        def progress(message: str):
            self.call_from_thread(
                self.action_progress_from_worker,
                message,
            )
        return progress

    def remove_path_everywhere(self, target_path: str):
        """
        Remove every cached occurrence of a path. A known cache may appear both
        under Home and under the Caches view; both must disappear immediately.
        """
        def remove_from(node: ScanNode) -> int:
            removed_measured = 0
            kept = []

            for child in node.children:
                if child.path == target_path:
                    if not child.metadata.get("size_pending", False):
                        removed_measured += child.size
                    continue

                nested_removed = remove_from(child)
                removed_measured += nested_removed
                kept.append(child)

            if len(kept) != len(node.children):
                node.children = kept

            if removed_measured:
                if node.metadata.get("virtual"):
                    node.size = sum(
                        c.size
                        for c in node.children
                        if not c.metadata.get("size_pending", False)
                    )
                elif not node.metadata.get("size_pending", False):
                    node.size = max(0, node.size - removed_measured)

            return removed_measured

        for region, root in self.region_roots.items():
            if root is None:
                continue
            if root.path == target_path:
                self.region_roots[region] = None
                continue
            remove_from(root)

        if self.selected_path == target_path:
            self.selected_path = None

    # ---------- cards ----------

    def _apfs_container_usage(self):
        """
        Return authoritative APFS container physical capacity.

        Modern macOS system/data/preboot/recovery/VM volumes share one APFS
        container. Volume-level df values are not the right number for the
        left-hand "physical disk space" card.

        Returns:
            (capacity, physical_used, physical_free, data_volume_used)
        """
        data_mount = "/System/Volumes/Data"

        # First identify which APFS container owns the mounted Data volume.
        proc = subprocess.run(
            [
                "/usr/sbin/diskutil",
                "info",
                "-plist",
                data_mount,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )

        if proc.returncode != 0:
            raise RuntimeError(
                proc.stderr.decode(
                    "utf-8",
                    errors="ignore",
                ).strip()
                or "diskutil info failed"
            )

        info = plistlib.loads(proc.stdout)
        container_ref = info.get(
            "APFSContainerReference"
        )

        if not container_ref:
            raise RuntimeError(
                "APFSContainerReference not found"
            )

        proc = subprocess.run(
            [
                "/usr/sbin/diskutil",
                "apfs",
                "list",
                "-plist",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=8,
        )

        if proc.returncode != 0:
            raise RuntimeError(
                proc.stderr.decode(
                    "utf-8",
                    errors="ignore",
                ).strip()
                or "diskutil apfs list failed"
            )

        payload = plistlib.loads(proc.stdout)

        for container in payload.get(
            "Containers",
            [],
        ):
            if (
                container.get("ContainerReference")
                != container_ref
            ):
                continue

            capacity = int(
                container.get(
                    "CapacityCeiling",
                    0,
                )
                or 0
            )
            physical_free = int(
                container.get(
                    "CapacityFree",
                    0,
                )
                or 0
            )
            physical_used = max(
                0,
                capacity - physical_free,
            )

            data_used = 0

            for volume in container.get(
                "Volumes",
                [],
            ):
                roles = volume.get("Roles") or []

                if "Data" in roles:
                    data_used += int(
                        volume.get(
                            "CapacityInUse",
                            0,
                        )
                        or 0
                    )

            if capacity > 0:
                return (
                    capacity,
                    physical_used,
                    physical_free,
                    data_used,
                )

        raise RuntimeError(
            f"APFS container {container_ref} not found"
        )

    def _available_capacity_for_important_usage(self):
        """
        Ask macOS Foundation for the space available to user-requested /
        important writes. Unlike plain df/statfs free space, this can include
        purgeable space that macOS is able to reclaim automatically.

        Returns bytes or None.
        """
        script = r"""
ObjC.import('Foundation');
var ref = Ref();
var url = $.NSURL.fileURLWithPath('/System/Volumes/Data');
var ok = url.getResourceValueForKeyError(
    ref,
    'NSURLVolumeAvailableCapacityForImportantUsageKey',
    null
);
if (!ok || ref[0] === undefined || ref[0] === null) {
    throw new Error('capacity unavailable');
}
Math.round(ObjC.unwrap(ref[0]));
"""

        try:
            proc = subprocess.run(
                [
                    "/usr/bin/osascript",
                    "-l",
                    "JavaScript",
                    "-e",
                    script,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )

            if proc.returncode != 0:
                return None

            value = int(proc.stdout.strip())

            if value > 0:
                return value

        except Exception:
            pass

        return None

    def _filesystem_usage(self):
        """
        Return:
            total_capacity,
            physical_used,
            physical_free,
            important_available

        `physical_free` is APFS container unallocated capacity.
        `important_available` is Apple's Foundation estimate of how much space
        user-requested/important writes may use, including reclaimable
        purgeable storage when available.
        """
        try:
            (
                total,
                physical_used,
                physical_free,
                _data_used,
            ) = self._apfs_container_usage()

            important_available = (
                self._available_capacity_for_important_usage()
            )

            return (
                total,
                physical_used,
                physical_free,
                important_available,
            )

        except Exception:
            usage = shutil.disk_usage("/")

            return (
                usage.total,
                usage.total - usage.free,
                usage.free,
                None,
            )

    def _local_snapshot_count(self) -> Optional[int]:
        """Time Machine local snapshots of the Data volume, cached for 60 s."""
        checked_at, count = self._snapshots
        if time.time() - checked_at < 60:
            return count
        try:
            proc = subprocess.run(
                ["/usr/bin/tmutil", "listlocalsnapshots", "/"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=10,
            )
            count = sum(
                1 for line in proc.stdout.splitlines()
                if line.startswith("com.apple.")
            ) if proc.returncode == 0 else None
        except Exception:
            count = None
        self._snapshots = (time.time(), count)
        return count

    def _disk_snapshot(self):
        return self._filesystem_usage(), self._local_snapshot_count()

    def update_disk_card(self):
        """
        Refresh the storage card off the UI thread: diskutil + Foundation
        take ~0.4 s, which froze the interface on every 5 s tick.
        """
        if self._disk_task and not self._disk_task.done():
            return
        self._disk_task = asyncio.create_task(self._update_disk_card())

    async def _update_disk_card(self):
        try:
            usage, snapshots = await asyncio.to_thread(self._disk_snapshot)
        except Exception:
            usage, snapshots = None, None

        try:
            disk_card = self.main_widget("#disk_card", Static)
            topbar = self.main_widget("#topbar_disk", Static)
        except Exception:
            return

        if usage is None:
            disk_card.update(Text("STORAGE\n\nUnavailable", style="#6b778c"))
            return

        total, physical_used, physical_free, important_available = usage
        topbar.update(
            f"Macintosh HD · {human_size(physical_used)} used · "
            f"{human_size(physical_free)} free"
        )

        # Every row is a slice of the container, so the rows add up to its
        # capacity. Purgeable space (mostly local snapshots) is counted as
        # used by APFS but as available by macOS, so it gets its own slice.
        purgeable = 0
        if important_available is not None:
            purgeable = max(0, min(important_available - physical_free, physical_used))

        home = self.region_roots.get("home")
        tmp = self.region_roots.get("temporary")
        home_size = (
            home.size
            if home is not None and not home.metadata.get("size_pending")
            else None
        )
        tmp_size = tmp.size if tmp is not None else 0

        slices = []
        if home_size is not None:
            slices.append(("Home", home_size, "#4f8cff"))
        slices.append(("/private/tmp", tmp_size, "#2fb5c9"))
        known = sum(v for _, v, _ in slices)
        slices.append((
            "Other" if home_size is not None else "Other (no Home)",
            max(0, physical_used - purgeable - known),
            "#6b778c",
        ))
        slices.append(("Purgeable", purgeable, "#8a6fd1"))
        slices.append(("Free", physical_free, "#2a3346"))

        width = 24
        t = Text.assemble(
            ("STORAGE", "bold #5d6a80"),
            (f"{human_size(total):>17}", "#6b778c"),
            "\n\n",
        )
        denominator = max(1, sum(v for _, v, _ in slices))
        used_cells = 0
        for i, (_, value, color) in enumerate(slices):
            cells = (
                round(value / denominator * width)
                if i < len(slices) - 1
                else width - used_cells
            )
            cells = max(0, min(cells, width - used_cells))
            used_cells += cells
            t.append("█" * cells, style=color)
        t.append("\n\n")

        for label, value, color in slices:
            t.append("■ ", style=color)
            t.append(f"{label:<15}")
            t.append(f"{human_size(value):>8}\n", style="bold")

        t.append("\n")
        t.append(f"{'Available':<17}", style="#c9d3e3")
        t.append(f"{human_size(physical_free + purgeable):>8}\n", style="bold #3ecf8e")
        # Lines stay within the 24-cell card width so nothing wraps.
        notes = ["(Free + Purgeable)", ""]
        if snapshots:
            notes.append(f"Purgeable: {snapshots} snapshots")
            notes.append("+ caches macOS can free")
        notes.append("Other: apps/system/users")
        if home_size is None:
            notes.append("Scan Home to split it")
        t.append("\n".join(notes), style="#6b778c")
        disk_card.update(t)

    def can_expand(self, node: ScanNode):
        return bool(
            node.children
            or (
                node.metadata.get("lazy_expandable")
                and not node.metadata.get("children_loaded")
            )
        )

    def flatten_visible(self):
        rows = []

        def visit(node, depth):
            rows.append((depth, node))
            if node.children and node.path in self.expanded:
                for child in sorted(
                    node.children,
                    key=lambda x: (
                        0 if x.metadata.get("size_pending") else 1,
                        x.size,
                        x.name.lower(),
                    ),
                    reverse=True,
                ):
                    visit(child, depth + 1)

        for root in sorted(self.category_roots(), key=lambda x: x.size, reverse=True):
            visit(root, 0)

        self.visible_nodes = [node for _, node in rows]
        return rows

    # Kind colour by what the user can do with the item.
    ACTION_COLORS = {
        "delete_cache": "#f0b357",
        "worktree_remove": "#b48cff",
        "project_clean": "#4f8cff",
        "delete_manual": "#8b97ab",
        "none": "#6b778c",
    }

    def name_text(self, node: ScanNode, depth: int, width: int) -> Text:
        t = Text("  " * depth)
        if self.can_expand(node):
            open_ = node.path in self.expanded and node.metadata.get("children_loaded")
            t.append("▾ " if open_ else "▸ ", style="#6b778c")
        else:
            t.append("  ")
        t.append(node.name, style="bold" if depth == 0 else "")
        t.truncate(width, overflow="ellipsis")
        return t

    def size_text(self, node: ScanNode) -> Text:
        if node.metadata.get("size_pending", False):
            return Text("…", justify="right", style="#6b778c")
        size = self.node_size(node)
        style = "bold #ffffff" if size >= 1024 ** 3 else "#c9d3e3"
        return Text(human_size(size), justify="right", style=style)

    def share_text(self, node: ScanNode, base: int, width: int = 10) -> Text:
        if node.metadata.get("size_pending", False) or base <= 0:
            return Text("")
        ratio = min(1.0, node.size / base)
        cells = ratio * width
        full = int(cells)
        bar = Text("█" * full, style="#4f8cff")
        partial = " ▏▎▍▌▋▊▉"[int((cells - full) * 8)].strip()
        if full < width and partial:
            bar.append(partial, style="#4f8cff")
        bar.pad_right(width - len(bar))
        bar.append(f" {ratio * 100:4.1f}%", style="#6b778c")
        return bar

    def kind_text(self, node: ScanNode) -> Text:
        action = node.metadata.get("action_type", "none")
        if node.metadata.get("in_use_by"):
            return Text("● in use", style="#e5534b")
        return Text(f"● {node.category}", style=self.ACTION_COLORS.get(action, "#6b778c"))

    def modified_text(self, node: ScanNode) -> Text:
        mtime = node.metadata.get("mtime")
        if not mtime:
            return Text("")
        if time.time() - mtime < 24 * 3600:
            return Text("today", style="#f0b357")
        local = time.localtime(mtime)
        fmt = "%b %d" if local.tm_year == time.localtime().tm_year else "%Y-%m"
        return Text(time.strftime(fmt, local), style="#8b97ab")

    def region_share_base(self) -> int:
        roots = self.category_roots()
        if not roots:
            return 0
        root = roots[0]
        measured = sum(
            c.size for c in root.children
            if not c.metadata.get("size_pending", False)
        )
        return max(root.size, measured)

    def refresh_table(self):
        table = self.main_widget("#tree", DataTable)
        rows = self.flatten_visible()

        # Columns are rebuilt on each refresh so Name absorbs the free width;
        # fixed widths + cell padding (2 per column) make up the rest.
        fixed = {"Size": 8, "Share": 16, "Kind": 14, "Modified": 8}
        available = max(0, table.size.width - 4) or 90
        name_width = max(20, available - sum(fixed.values()) - 2 * (len(fixed) + 1))

        table.clear(columns=True)
        table.add_column("Name", width=name_width)
        table.add_column(Text("Size", justify="right"), width=fixed["Size"])
        table.add_column("Share", width=fixed["Share"])
        table.add_column("Kind", width=fixed["Kind"])
        table.add_column("Modified", width=fixed["Modified"])

        base = self.region_share_base()
        for idx, (depth, node) in enumerate(rows):
            kind = self.kind_text(node)
            kind.truncate(fixed["Kind"], overflow="ellipsis")
            table.add_row(
                self.name_text(node, depth, name_width),
                self.size_text(node),
                # The region root is always 100 %; leave it blank.
                self.share_text(node, base) if depth else Text(""),
                kind,
                self.modified_text(node),
                key=str(idx),
            )

        if self.selected_path:
            for idx, node in enumerate(self.visible_nodes):
                if node.path == self.selected_path:
                    table.move_cursor(row=idx)
                    self.show_details(node)
                    return

        if self.visible_nodes:
            self.selected_path = self.visible_nodes[0].path
            table.move_cursor(row=0)
            self.show_details(self.visible_nodes[0])
        else:
            self.selected_path = None
            self.clear_details()

    def on_resize(self, _event):
        self.call_after_refresh(self.refresh_table)

    def region_size_text(self, key: str) -> str:
        root = self.root_by_kind(key)
        if root is None:
            return "—"
        if root.metadata.get("virtual"):
            measured = sum(
                c.size for c in root.children
                if not c.metadata.get("size_pending", False)
            )
            pending = any(c.metadata.get("size_pending", False) for c in root.children)
            text = human_size(measured) if measured else ("" if pending else "0")
            return f"{text}…" if pending else text
        if root.metadata.get("size_pending", False):
            return "…"
        return human_size(root.size)

    def refresh_nav(self):
        for key in self.NAV:
            dot, dot_style = self._nav_status_dot(key)
            label = Text.assemble(
                (f"{dot} ", dot_style),
                f"{self.NAV_LABELS[key]:<13}",
                (f"{self.region_size_text(key):>9}", "bold"),
            )
            self.main_widget(f"#nav_{key}", Button).label = label

    def _nav_status_dot(self, region: str) -> tuple[str, str]:
        meta = self.cache.region_meta(region)
        if self._scanning_regions and region in self._scanning_regions:
            return "⟳", "#4f8cff"
        if meta is None:
            return "○", "#5d6a80"
        if meta["stale"]:
            return "◐", "#f0b357"
        return "●", "#3ecf8e"

    SECTION_INFO = {
        "workspaces": ("Workspaces", "", "Projects by marker files"),
        "worktrees": ("Worktrees", "", "Linked git worktrees"),
        "temporary": ("Temporary", "/private/tmp", "Your items ≥ 10 MiB"),
        "caches": ("Caches", "", "Known developer caches"),
        "home": ("Home", "~", "Full scan covers every region"),
    }

    def refresh_section(self):
        title, where, summary = self.SECTION_INFO[self.active_category]
        root = self.root_by_kind(self.active_category)
        meta = self.cache.region_meta(self.active_category)

        parts = [summary]
        if root is not None:
            parts.append(f"{len(root.children)} items")
            parts.append(self.region_size_text(self.active_category))
        if meta is None:
            parts.append("not scanned")
        else:
            age = self.cache.describe_age(meta["age"])
            parts.append(f"scanned {age} ago" + (" (stale)" if meta["stale"] else ""))

        self.main_widget("#section_title", Static).update(
            Text.assemble((title, "bold"), (f"  {where}" if where else "", "#6b778c"))
        )
        self.main_widget("#section_subtitle", Static).update(" · ".join(parts))
        self.main_widget("#rescan_region", Button).label = (
            "↻ Full scan" if self.active_category == "home" else "↻ Rescan"
        )

    def refresh_ui(self):
        self.refresh_nav()
        self.refresh_section()
        self.refresh_table()

    # ---------- details ----------

    def show_details(self, node: ScanNode):
        self.selected_path = node.path
        self.main_widget("#details_title", Static).update(Text(node.name, style="bold"))
        self.main_widget("#details_path", Static).update(
            "Virtual group" if node.metadata.get("virtual") else node.path
        )

        t = Text()

        def row(key: str, value: str, style: str = "#e6ebf4"):
            t.append(f"{key:<11}", style="#6b778c")
            t.append(f"{value}\n", style=style)

        def heading(text: str):
            t.append(f"\n{text}\n", style="bold")

        row("Size", self.display_size(node))
        row("Kind", node.category)
        mtime = node.metadata.get("mtime")
        if mtime:
            row("Modified", time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)))
        row("Scanned", f"{self.cache.describe_age(max(0, time.time() - node.scanned_at))} ago")

        in_use = node.metadata.get("in_use_by") or []
        if in_use:
            row("In use", ", ".join(in_use[:3]), "#e5534b")

        worktree = node.metadata.get("worktree")
        if worktree:
            row("Branch", worktree.get("branch") or "?")
            changes = worktree.get("changes")
            row(
                "Changes",
                "unknown" if changes is None else ("none" if changes == 0 else str(changes)),
                "#e6ebf4" if changes == 0 else "#f0b357",
            )
            row("Repo", str(Path(worktree.get("common_dir", "")).parent))

        project = node.metadata.get("project")
        if project:
            heading("Project")
            row("Type", str(project.get("project_type")))
            row("Detected", str(project.get("detected_by")))
            row("Clean", str(project.get("label")))
            if project.get("note"):
                t.append(f"{project.get('note')}\n", style="#8b97ab")

        heading({
            "delete_cache": "Why deletable",
            "delete_manual": "Manual delete",
            "worktree_remove": "Why removable",
            "project_clean": "Project clean",
        }.get(node.metadata.get("action_type", "none"), "Inspect only"))
        t.append(f"{node.reason}\n", style="#c9d3e3")

        largest = sorted(
            (c for c in node.children if not c.metadata.get("size_pending", False)),
            key=lambda c: c.size,
            reverse=True,
        )[:5]
        if largest and not node.metadata.get("container"):
            heading("Largest inside")
            for child in largest:
                name = child.name + ("/" if child.metadata.get("is_dir") else "")
                t.append(f"  {name[:22]:<23}", style="#c9d3e3")
                t.append(f"{human_size(child.size):>8}\n", style="bold")
        elif self.can_expand(node) and not node.metadata.get("children_loaded"):
            t.append("\nPress ↵ to list the next level.\n", style="#6b778c")

        self.main_widget("#details_body", Static).update(t)
        self.update_action_buttons(node)

    def clear_details(self):
        self.main_widget("#details_title", Static).update("No items")
        self.main_widget("#details_path", Static).update("")
        self.main_widget("#details_body", Static).update(
            Text("Nothing found in this region.", style="#6b778c")
        )
        self.update_action_buttons(None)

    def update_action_buttons(self, node):
        action_btn = self.main_widget("#run_action", Button)
        action_btn.remove_class("danger", "safe")

        if self._operation_running:
            # Browsing stays available, but a second clean/delete is blocked.
            action_btn.display = True
            action_btn.disabled = True
            action_btn.label = "Operation running…"
            return

        action = node.metadata.get("action_type", "none") if node else "none"
        if action == "none":
            action_btn.display = False
            return

        action_btn.display = True
        size_text = self.display_size(node)

        # Scan-time in-use data may be stale; the runner re-checks open
        # files right before deleting, so the button stays enabled.
        if action == "project_clean":
            project = node.metadata.get("project") or {}
            action_btn.disabled = False
            action_btn.add_class("safe")
            action_btn.label = project.get("label", "Run project clean")
        elif action == "worktree_remove":
            action_btn.disabled = False
            action_btn.add_class("danger")
            action_btn.label = f"Remove worktree  {size_text}"
        else:
            action_btn.disabled = False
            action_btn.add_class("danger")
            action_btn.label = f"Delete  {size_text}"

    # ---------- navigation ----------

    def set_default_expansion(self):
        self.expanded.clear()
        for root in self.category_roots():
            self.expanded.add(root.path)

    def switch_category(self, category: str):
        if category not in self.NAV:
            return
        self.active_category = category
        self.selected_path = None
        for key in self.NAV:
            btn = self.main_widget(f"#nav_{key}", Button)
            if key == category:
                btn.add_class("active")
            else:
                btn.remove_class("active")
        self.set_default_expansion()
        self.refresh_ui()

    # ---------- lazy expand ----------

    async def lazy_expand(self, node: ScanNode):
        # The directory currently being modified should not be freshly scanned
        # until the clean/delete operation finishes. Other directories remain
        # fully browsable.
        if self.is_under_active_operation(node.path):
            self.set_status(
                f"{node.name} is currently being modified. "
                "It will refresh automatically when the operation finishes; "
                "you can keep browsing other directories."
            )
            return

        operation_in_background = self._operation_running

        if not operation_in_background:
            self.set_progress(
                5,
                f"Listing direct children: {node.name}",
            )
            self.set_status(
                "Reading only the real next filesystem level. "
                "Recursive size calculation is skipped."
            )

        # If cargo clean/delete is running, don't let this incidental directory
        # scan overwrite the operation progress indicator.
        scanner = (
            Scanner()
            if operation_in_background
            else self.make_progress_scanner()
        )

        try:
            fresh = await asyncio.to_thread(
                scanner.scan_children,
                node,
            )
        except Exception as exc:
            if not operation_in_background:
                self.set_progress(100, "Scan failed.")
            self.set_status(
                f"Scan failed: {exc}"
            )
            return

        self.replace_node(node.path, fresh)
        self.expanded.add(node.path)
        self.selected_path = node.path
        self.save_cache()
        self.refresh_ui()

        # Expand first so the UI is immediate, then calculate the exact
        # direct-child sizes in the background. This is one GDU depth-1 traversal
        # for the expanded directory, not one scan per child.
        self.schedule_child_sizing(node.path)

        if not operation_in_background:
            self.set_progress(
                100,
                f"Expanded: {node.name}",
            )
            self.set_status(
                "Expanded immediately. Exact child sizes are now being "
                "calculated in the background."
            )

    def toggle_expand(self, node: Optional[ScanNode] = None):
        node = node or self.current_node()
        if not node:
            return
        if node.metadata.get("lazy_expandable") and not node.metadata.get("children_loaded"):
            asyncio.create_task(self.lazy_expand(node))
            return
        if not node.children:
            return
        self.selected_path = node.path
        if node.path in self.expanded:
            self.expanded.remove(node.path)
        else:
            self.expanded.add(node.path)
            self.schedule_child_sizing(node.path)
        self.refresh_table()

    def action_measure_sizes(self):
        node = self.current_node()

        if not node:
            return

        if self._operation_running:
            self.set_status(
                "A clean/delete operation is running; exact sizing is unavailable."
            )
            return

        if (
            not node.metadata.get("is_dir", False)
            or not node.metadata.get("children_loaded", False)
            or not node.children
        ):
            self.set_status(
                "Expand this directory first, then measure its direct children."
            )
            return

        self.schedule_child_sizing(node.path)

    # ---------- tree mutation ----------

    def find_parent(self, target_path: str):
        for root in self.all_region_roots():
            for node in root.walk():
                for idx, child in enumerate(node.children):
                    if child.path == target_path:
                        return node, idx
        return None, None

    def replace_node(self, old_path: str, fresh: Optional[ScanNode]):
        for region, root in self.region_roots.items():
            if root is None:
                continue
            if root.path == old_path:
                self.region_roots[region] = fresh
                return

        parent, idx = self.find_parent(old_path)
        if parent is None or idx is None:
            return

        old = parent.children[idx]
        old_size = old.size if not old.metadata.get("size_pending") else 0
        new_size = (
            fresh.size if fresh and not fresh.metadata.get("size_pending") else 0
        )
        delta = new_size - old_size

        if fresh is None:
            parent.children.pop(idx)
        else:
            parent.children[idx] = fresh

        current = parent
        while current:
            if current.metadata.get("virtual"):
                current.size = sum(
                    x.size for x in current.children
                    if not x.metadata.get("size_pending")
                )
            elif not current.metadata.get("size_pending"):
                current.size = max(0, current.size + delta)
            current = self.find_node(current.parent_path)

    def save_cache(self):
        # In-place edits keep each region's original scan time.
        for region, root in self.region_roots.items():
            if root is not None:
                self.cache.save_region(region, [root])

    # ---------- scan / refresh ----------

    def build_full_scan_plan(self):
        """Return (to_scan, skip); skip holds fresh (region, meta) pairs."""
        to_scan = []
        skip = []
        for region in REGIONS:
            meta = self.cache.region_meta(region)
            if meta and not meta["stale"]:
                skip.append((region, meta))
            else:
                to_scan.append(region)
        return to_scan, skip

    def full_scan_message(self):
        to_scan, skip = self.build_full_scan_plan()
        labels = {
            "workspaces": "开发工作区（重新发现）",
            "worktrees": "Git worktrees",
            "temporary": "临时目录 /private/tmp",
            "caches": "缓存目录 Caches",
            "home": "Home 全量（大目录 + 精确大小）",
        }
        lines = ["将扫描:"]
        if to_scan:
            lines += [f"• {labels[r]}" for r in to_scan]
        else:
            lines.append("• （所有区域均为最新，无需扫描）")
        if skip:
            lines += ["", "跳过（30 分钟内已扫）:"]
            lines += [
                f"• {labels[r]}（{self.cache.describe_age(meta['age'])} 前）"
                for r, meta in skip
            ]
        return "\n".join(lines)

    @work(exclusive=True, group="filesystem_action")
    async def run_full_scan(self):
        if self._scanning_regions:
            self.set_status(
                "A region scan is already running."
            )
            return

        # Old background measurements refer to the previous cached tree.
        for task in list(self._sizing_parents.values()):
            task.cancel()
        self._sizing_parents.clear()

        to_scan, skip = self.build_full_scan_plan()

        if not to_scan:
            ok = await self.push_screen_wait(
                ConfirmScreen(
                    "全盘扫描",
                    "所有区域均为最新（30 分钟内已扫），当前无需扫描。\n\n"
                    "仍要强制全部重扫吗？",
                    destructive=False,
                )
            )
            if not ok:
                self.set_progress(100, "All regions are fresh; nothing to do.")
                self.set_status(
                    "All regions are fresh; full scan not needed."
                )
                return
            to_scan = list(REGIONS)
            skip = []

        ok = await self.push_screen_wait(
            ConfirmScreen(
                "全盘扫描（所有区域）",
                self.full_scan_message(),
                destructive=False,
            )
        )
        if not ok:
            self.set_progress(0, "Full scan cancelled.", busy=False)
            self.set_status("Full scan cancelled.")
            return

        notes = [
            f"跳过 {region}（{self.cache.describe_age(meta['age'])} 前）"
            for region, meta in skip
        ]
        self._scanning_regions = to_scan
        self.set_progress(5, "Starting full scan…")
        self.set_status(
            "; ".join(notes) if notes else "Full scan started."
        )

        scanner = self.make_progress_scanner()
        total = len(to_scan)

        for idx, region in enumerate(to_scan):
            method_name, label = self.SCAN_METHODS[region]
            self.set_progress(5 + int(70 * idx / total), f"{label}…")
            try:
                root = await asyncio.to_thread(
                    getattr(scanner, method_name)
                )
            except Exception as exc:
                self.set_progress(100, f"{label} failed: {exc}")
                self.set_status(
                    f"{label} failed: {exc}"
                )
                continue
            self.region_roots[region] = root
            self.cache.save_region(
                region, [root] if root else [], created_at=time.time()
            )
            self.refresh_nav()

        self._scanning_regions = None
        self.selected_path = None
        self.set_default_expansion()
        self.refresh_ui()
        self.update_disk_card()
        self.schedule_workspace_sizing()
        self.set_progress(100, "Full scan complete.")
        self.set_status(
            "Full scan complete; fresh regions were skipped."
        )

        if self.region_roots.get("home") is not None:
            self.switch_category("home")

    async def scan_regions(self, regions):
        """
        Scan the given regions sequentially. Each region root is replaced
        in place and cached independently; other regions stay untouched.
        """
        if self._scanning_regions:
            self.set_status(
                "A region scan is already running."
            )
            return

        self._scanning_regions = list(regions)
        scanner = self.make_progress_scanner()
        total = len(regions)

        for idx, region in enumerate(regions):
            method_name, label = self.SCAN_METHODS[region]
            self.set_progress(5 + int(70 * idx / total), f"{label}…")
            try:
                root = await asyncio.to_thread(
                    getattr(scanner, method_name)
                )
            except Exception as exc:
                self.set_progress(100, f"{label} failed: {exc}")
                self.set_status(
                    f"{label} failed: {exc}"
                )
                continue
            self.region_roots[region] = root
            self.cache.save_region(
                region, [root] if root else [], created_at=time.time()
            )
            self.refresh_nav()

        self._scanning_regions = None
        self.selected_path = None
        self.set_default_expansion()
        self.refresh_ui()
        self.schedule_workspace_sizing()
        self.set_progress(100, "Scan complete.")
        self.set_status(
            f"Scanned: {', '.join(regions)}."
        )

    def action_force_full_scan(self):
        if self._operation_running:
            self.set_status(
                "A clean/delete operation is still running."
            )
            return
        self.run_full_scan()

    # ---------- workspace size measurement ----------

    def schedule_workspace_sizing(self):
        """
        Measure pending workspace project sizes in the background, one gdu
        pass at a time, updating the UI as each result arrives.
        """
        if self._operation_running:
            return
        root = self.region_roots.get("workspaces")
        if not root:
            return
        pending = [
            c for c in root.children
            if c.metadata.get("size_pending", False)
        ]
        if not pending:
            return
        task = getattr(self, "_ws_sizing_task", None)
        if task and not task.done():
            return
        self._ws_sizing_task = asyncio.create_task(
            self._measure_workspaces(pending)
        )

    async def _measure_workspaces(self, pending):
        done = 0
        async with self._size_semaphore:
            for node in pending:
                live = self.find_node(node.path)
                if live is None:
                    continue
                if not live.metadata.get("size_pending", False):
                    continue
                try:
                    size = await asyncio.to_thread(
                        gdu_size,
                        Path(live.path),
                    )
                except Exception:
                    continue
                live.size = size
                live.metadata["size_pending"] = False
                live.scanned_at = time.time()
                done += 1
                if done % 3 == 0:
                    self.save_cache()
                    self.refresh_ui()
        self.save_cache()
        self.refresh_ui()
        self.set_progress(
            100,
            f"Workspace sizes measured: {done}/{len(pending)}",
        )
        self.set_status(
            f"Workspace project sizes measured in the background "
            f"({done}/{len(pending)})."
        )

    async def refresh_selected(self, node: ScanNode):
        self.set_progress(10, f"Refreshing / measuring selected item: {node.name}")
        self.set_status(
            "Refresh Selected measures this exact directory. A very large directory may take time."
        )
        scanner = self.make_progress_scanner()
        fresh = await asyncio.to_thread(scanner.rescan_node, node)
        self.replace_node(node.path, fresh)
        self.save_cache()
        self.refresh_ui()
        self.update_disk_card()
        self.set_progress(100, f"Refreshed: {node.name}")

    def action_refresh_selected(self):
        if self._operation_running:
            self.set_status(
                "A clean/delete operation is still running."
            )
            return
        node = self.current_node()
        if node:
            asyncio.create_task(self.refresh_selected(node))

    # ---------- actions ----------

    @work(exclusive=True, group="filesystem_action")
    async def run_current_action(self):
        if self._operation_running:
            self.set_status(
                "An operation is already running."
            )
            return

        node = self.current_node()
        if not node:
            return

        action = node.metadata.get("action_type", "none")
        if action == "none":
            return

        project = node.metadata.get("project") or {}
        action_label = ""
        if action == "project_clean":
            action_label = project.get("label", "Project clean")
        elif action == "worktree_remove":
            action_label = f"Remove worktree {self.display_size(node)}"
        elif action in ("delete_cache", "delete_manual"):
            action_label = f"Delete {self.display_size(node)}"

        self.set_status(
            f"Action requested: {action_label} — waiting for confirmation."
        )
        self.set_progress(
            5,
            f"Waiting for confirmation: {action_label}",
        )

        if action == "project_clean":
            title = project.get("label", "Project clean")
            message = (
                f"Project:\n{node.path}\n\n"
                f"Detected by: {project.get('detected_by')}\n"
                f"Command: {' '.join(project.get('command') or [])}\n\n"
                "The standard project clean command will run in this exact directory.\n"
                "After it finishes, this project is refreshed automatically."
            )
            destructive = False
        elif action == "delete_cache":
            title = "Delete cache / temporary directory"
            message = (
                f"{node.path}\n\n"
                f"Current size: {self.display_size(node)}\n"
                f"{node.reason}\n\n"
                "This permanently removes the directory. Open files are checked "
                "first; deletion is refused if a process is using it."
            )
            destructive = True
        elif action == "worktree_remove":
            worktree = node.metadata.get("worktree") or {}
            title = "Remove git worktree"
            message = (
                f"{node.path}\n\n"
                f"Branch: {worktree.get('branch')} (kept)\n"
                f"Current size: {self.display_size(node)}\n\n"
                "Runs git worktree remove without --force: git refuses if the "
                "worktree has uncommitted or untracked files. Ignored build "
                "output is deleted with it."
            )
            destructive = True
        else:
            title = "Delete selected path permanently"
            message = (
                f"{node.path}\n\n"
                f"Current size: {self.display_size(node)}\n\n"
                "No standard clean action was detected for this item.\n"
                "This will permanently delete exactly this file/directory."
            )
            destructive = True

        ok = await self.push_screen_wait(
            ConfirmScreen(
                title,
                message,
                destructive=destructive,
            )
        )
        if not ok:
            self.set_progress(0, "Action cancelled.", busy=False)
            self.set_status("Action cancelled.")
            return

        # Snapshot UI state before the filesystem changes.
        node_path = node.path
        node_name = node.name
        was_expanded = node_path in self.expanded
        was_children_loaded = bool(
            node.metadata.get("children_loaded")
        )
        before_size = (
            node.size
            if not node.metadata.get("size_pending", False)
            else None
        )

        self.cancel_sizing_under(node_path)
        self._active_operation_path = node_path
        self.set_operation_controls(True, f"Running {title}…")
        self.set_progress(20, f"Starting: {title}")
        self.set_status(
            f"Starting {title} in {node_path}…"
        )

        action_progress = self.make_action_progress_callback()

        try:
            reported_freed, log = await asyncio.to_thread(
                self.runner.execute,
                node,
                action_progress,
            )
        except Exception as exc:
            error_text = str(exc) or exc.__class__.__name__
            self.set_progress(
                100,
                f"Action failed: {error_text}",
            )
            self.set_status(
                f"Action failed: {error_text}"
            )

            # Persist a plain-text diagnostic so failures aren't lost when
            # the terminal window is narrow.
            try:
                log_dir = Path.home() / ".cache" / "mac-disk-scanner"
                log_dir.mkdir(parents=True, exist_ok=True)
                with (log_dir / "action-errors.log").open(
                    "a",
                    encoding="utf-8",
                ) as fh:
                    fh.write(
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                        f"{node.path}: {error_text}\n"
                    )
            except Exception:
                pass

            self._active_operation_path = None
            self.set_operation_controls(False)
            return

        if action == "project_clean":
            # Mandatory automatic refresh of the modified project. Do not wait
            # for the user to press Refresh Selected.
            self.set_progress(
                75,
                f"{title} finished — refreshing {node_name}…",
            )
            self.set_status(
                "Clean command finished. Refreshing the cleaned project now…"
            )

            scanner = self.make_progress_scanner()

            try:
                fresh = await asyncio.to_thread(
                    scanner.rescan_node,
                    node,
                )
            except Exception as exc:
                self.set_progress(100, "Clean succeeded; refresh failed.")
                self.set_status(
                    f"{title} succeeded, but automatic refresh failed: {exc}"
                )
                self._active_operation_path = None
                self.set_operation_controls(False)
                return

            if fresh is None:
                self.remove_path_everywhere(node_path)
                self.selected_path = None
            else:
                self.replace_node(node_path, fresh)
                self.selected_path = node_path

                # If the user had this project open, keep it open and refresh
                # direct-child sizes automatically after the fresh listing.
                if was_expanded:
                    self.expanded.add(node_path)

                refreshed = self.find_node(node_path)
                # Do not auto-run an expensive exact child-size scan here.
                # The refreshed tree stays interactive; exact sizes are on-demand.

            actual_freed = reported_freed
            refreshed = self.find_node(node_path)

            if (
                before_size is not None
                and refreshed is not None
                and not refreshed.metadata.get("size_pending", False)
            ):
                actual_freed = max(
                    actual_freed,
                    max(0, before_size - refreshed.size),
                )

            self.save_cache()
            self.refresh_ui()
            self.update_disk_card()
            self.set_progress(
                100,
                f"{title} complete; project refreshed automatically.",
            )
            self.set_status(
                f"{log} Refreshed automatically. "
                f"Freed about {human_size(actual_freed)}."
            )

        else:
            # ActionRunner verifies that the path is actually gone.
            self.set_progress(
                80,
                f"Delete finished — updating tree for {node_name}…",
            )

            self.remove_path_everywhere(node_path)
            self.expanded.discard(node_path)
            self.save_cache()
            self.refresh_ui()
            self.update_disk_card()

            self.set_progress(
                100,
                f"Deleted {node_name} and updated the tree.",
            )
            self.set_status(
                f"{log} Freed about {human_size(reported_freed)}."
            )

        self._active_operation_path = None
        self.set_operation_controls(False)

    # ---------- events ----------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted):
        if event.data_table.id != "tree":
            return
        try:
            idx = int(str(event.row_key.value))
        except Exception:
            return
        if 0 <= idx < len(self.visible_nodes):
            self.show_details(self.visible_nodes[idx])

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        if event.data_table.id != "tree":
            return
        try:
            idx = int(str(event.row_key.value))
        except Exception:
            return
        if 0 <= idx < len(self.visible_nodes):
            node = self.visible_nodes[idx]
            self.show_details(node)
            if self.can_expand(node):
                self.toggle_expand(node)

    def on_button_pressed(self, event: Button.Pressed):
        bid = event.button.id or ""
        if bid.startswith("nav_"):
            self.switch_category(bid.removeprefix("nav_"))
        elif bid == "rescan_region":
            if self.active_category in REGIONS:
                if self.active_category == "home":
                    # Home rescan IS the full scan across all regions,
                    # with the same confirm/plan dialog as the toolbar.
                    self.action_force_full_scan()
                else:
                    asyncio.create_task(
                        self.scan_regions([self.active_category])
                    )
        elif bid == "run_action":
            self.run_current_action()
        elif bid == "finder":
            self.open_finder()
        elif bid == "terminal":
            self.open_terminal()

    def action_run_action(self):
        node = self.current_node()
        if node and node.metadata.get("action_type", "none") != "none":
            self.run_current_action()

    def action_toggle_expand(self):
        self.toggle_expand()

    def action_expand(self):
        node = self.current_node()
        if not node:
            return
        if node.metadata.get("lazy_expandable") and not node.metadata.get("children_loaded"):
            self.toggle_expand(node)
        elif node.children and node.path not in self.expanded:
            self.expanded.add(node.path)
            self.selected_path = node.path
            self.schedule_child_sizing(node.path)
            self.refresh_table()

    def action_collapse(self):
        node = self.current_node()
        if node and node.children and node.path in self.expanded:
            self.expanded.remove(node.path)
            self.selected_path = node.path
            self.refresh_table()

    # ---------- Finder / Terminal ----------

    def current_real_path(self):
        node = self.current_node()
        if not node or node.metadata.get("virtual"):
            return None
        path = Path(node.path)
        return path if path.exists() else None

    def open_finder(self):
        path = self.current_real_path()
        if path:
            subprocess.Popen(
                ["open", "-R", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def open_terminal(self):
        path = self.current_real_path()
        if path:
            target = path if path.is_dir() else path.parent
            subprocess.Popen(
                ["open", "-a", "Terminal", str(target)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
