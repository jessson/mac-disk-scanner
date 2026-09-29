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
from textual.widgets import Button, DataTable, Footer, Header, Label, ProgressBar, Static

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
    SUB_TITLE = "Generic filesystem scanner"

    CSS = """
    Screen { background: #0a0d14; color: #e8edf7; }
    Header { background: #10141d; color: #f4f7fb; }

    #body { height: 1fr; }
    #sidebar { width: 28; background: #0d1119; border-right: solid #232b3d; padding: 1; }
    .side-label { height: 2; text-style: bold; color: #5d6a80; padding-left: 1; }
    .nav { width: 100%; height: 3; margin-bottom: 1; background: #0d1119;
           color: #9aa5b8; border: none; content-align: left middle; padding: 0 1; }
    .nav:hover { background: #151a26; }
    .nav.active { background: #151a26; color: #e8edf7; text-style: bold; }
    #spacer { height: 1fr; }

    #disk_card { height: auto; min-height: 4; padding: 1; margin-top: 1;
                              background: #151a26; border: round #232b3d; color: #9aa5b8; }

    #center { width: 1fr; min-width: 66; }
    #section { height: 5; padding: 1 2; }
    #section_title { text-style: bold; color: #e8edf7; }
    #section_subtitle { color: #9aa5b8; }
    #rescan_region { width: 22; }

    #tree { height: 1fr; margin: 0 2; background: #151a26; border: round #232b3d; }
    DataTable > .datatable--header { background: #131826; color: #e8edf7; text-style: bold; }
    DataTable > .datatable--cursor { background: #243650; color: white; }

    #right { width: 34; min-width: 32; background: #0d1119;
             border-left: solid #232b3d; padding: 1 2; }
    #details_title { height: auto; min-height: 2; text-style: bold; color: #e8edf7; }
    #details_path { height: auto; max-height: 4; color: #5d6a80; margin-bottom: 1; }
    #details_body { height: 1fr; color: #c5cfdb; }
    #actions { height: auto; min-height: 18; border-top: solid #232b3d; padding-top: 1; }
    #actions Button { width: 100%; height: 3; margin-bottom: 1; }

    #progress_area { height: 5; padding: 0 2; background: #10141d;
                     border-top: solid #232b3d; }
    #progress_text { height: 1; color: #9aa5b8; }
    ProgressBar { height: 2; }
    #status { height: 2; padding: 0 2; background: #0d1119; color: #9aa5b8; }
    """

    BINDINGS = [
        ("enter", "toggle_expand", "Expand / scan"),
        ("left", "collapse", "Collapse"),
        ("right", "expand", "Expand"),
        ("r", "refresh_selected", "Refresh selected"),
        ("m", "measure_sizes", "Measure exact sizes"),
        ("shift+r", "force_full_scan", "Full scan"),
        ("q", "quit", "Quit"),
    ]

    NAV = ("workspaces", "temporary", "caches", "home")

    # region -> (scanner method name, display label)
    SCAN_METHODS = {
        "workspaces": ("scan_workspaces", "Scanning workspaces"),
        "temporary": ("scan_private_tmp", "Scanning /private/tmp"),
        "caches": ("scan_known_caches", "Scanning known caches"),
        "home": ("scan_home", "Scanning Home (full)"),
    }

    # Regions scanned automatically at startup (Home stays manual-only).
    QUICK_REGIONS = ("workspaces", "temporary", "caches")

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
        # Only one du runs at a time to avoid hammering the SSD.
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

    def compose(self) -> ComposeResult:
        yield Header()

        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("扫描区域", classes="side-label")
                yield Button("Workspaces", id="nav_workspaces", classes="nav active")
                yield Button("Temporary", id="nav_temporary", classes="nav")
                yield Button("Caches", id="nav_caches", classes="nav")
                yield Button("Home", id="nav_home", classes="nav")
                yield Static("", id="spacer")
                yield Static("Disk", id="disk_card")

            with Vertical(id="center"):
                with Horizontal(id="section"):
                    with Vertical():
                        yield Static("Workspaces", id="section_title")
                        yield Static(
                            "Development workspaces found by project marker files. "
                            "Sizes are measured on demand.",
                            id="section_subtitle",
                        )
                    yield Button("重新扫描", id="rescan_region")
                table = DataTable(id="tree", cursor_type="row", zebra_stripes=True)
                table.add_columns("Name", "Size", "Type", "Path")
                yield table

            with Vertical(id="right"):
                yield Static("Select an item", id="details_title")
                yield Static("", id="details_path")
                yield Static("Click a directory to inspect its next real filesystem level.",
                             id="details_body")
                with Vertical(id="actions"):
                    yield Button("No Action", id="run_action")
                    yield Button("Open in Finder", id="finder")
                    yield Button("Open in Terminal", id="terminal")

        with Vertical(id="progress_area"):
            yield Static("Ready.", id="progress_text")
            yield ProgressBar(total=100, id="progress")

        yield Static(
            "Unknown directories are inspect-only. Projects expose standard clean commands; "
            "explicit cache/temp paths expose delete.",
            id="status",
        )
        yield Footer()

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

        if pending:
            self.set_progress(
                5,
                f"Scanning quick regions: {', '.join(pending)}…",
            )
            self.query_one("#status", Static).update(
                "Quick region scans are running in the background. "
                "Home (full scan) is manual only."
            )
            asyncio.create_task(self.scan_regions(pending))
        elif stale:
            self.set_progress(100, "Loaded cached results (some regions stale).")
            self.query_one("#status", Static).update(
                "Some regions are older than 30 minutes. "
                "Use per-region 重新扫描 or the full scan to refresh them."
            )
        else:
            self.set_progress(100, "Loaded cached results.")
            self.query_one("#status", Static).update(
                "All regions are fresh. Home full scan stays manual."
            )
        self.schedule_workspace_sizing()

    # ---------- lookup ----------

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
        table = self.query_one("#tree", DataTable)
        row = table.cursor_row
        if row < 0 or row >= len(self.visible_nodes):
            return None
        return self.visible_nodes[row]

    # ---------- progress / size ----------

    def set_progress(self, pct: int, text: str):
        self.query_one("#progress", ProgressBar).update(progress=max(0, min(100, pct)))
        self.query_one("#progress_text", Static).update(text)

    def make_progress_scanner(self):
        def progress(pct: int, message: str):
            self.call_from_thread(self.set_progress, pct, message)
        return Scanner(progress=progress)

    def display_size(self, node: ScanNode):
        # "…" means discovered but background size calculation has not
        # completed yet. It is not zero and it is not unknown forever.
        return "…" if node.metadata.get("size_pending", False) else human_size(node.size)

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
            self.query_one("#status", Static).update(
                "No real direct children to measure."
            )
            return

        self.set_progress(
            20,
            f"Exact child sizing {parent.name} "
            f"({len(pending_paths)} direct children)…",
        )
        self.query_one("#status", Static).update(
            "One background du -d 1 traversal is calculating all direct-child sizes. "
            "It does not run du separately for every child."
        )

        scanner = Scanner()

        try:
            async with self._size_semaphore:
                scan_task = asyncio.create_task(
                    asyncio.to_thread(
                        scanner.measure_children_sizes,
                        parent,
                    )
                )

                # GDU does not expose a reliable machine-readable percentage
                # for this parsing mode. Show elapsed time instead of a fake
                # 1/N percentage, so the UI clearly remains alive.
                started = time.monotonic()

                while not scan_task.done():
                    elapsed = time.monotonic() - started

                    self.set_progress(
                        35,
                        f"Exact child sizing {parent.name} — "
                        f"{elapsed:.1f}s elapsed",
                    )
                    await asyncio.sleep(0.5)

                sizes = await scan_task

        except asyncio.CancelledError:
            return
        except Exception as exc:
            self.set_progress(
                100,
                f"Exact child size scan failed: {parent.name}",
            )
            self.query_one("#status", Static).update(
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
        self.query_one("#status", Static).update(
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

        action_btn = self.query_one("#run_action", Button)

        if running:
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
        self.query_one("#status", Static).update(message)

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
                if node.metadata.get("container"):
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

    def update_disk_card(self):
        """
        Update the compact Storage card.

        This callback may fire during startup/shutdown or between screen
        refreshes. If the widget is not mounted yet, simply skip this tick.
        """
        try:
            disk_card = self.query_one(
                "#disk_card",
                Static,
            )
        except Exception:
            return

        try:
            (
                total,
                physical_used,
                physical_free,
                important_available,
            ) = self._filesystem_usage()

            available = (
                important_available
                if important_available is not None
                else physical_free
            )

            available = max(
                0,
                min(total, available),
            )
            used = max(
                0,
                total - available,
            )

            disk_card.update(
                "[b]Storage[/b]\n"
                f"Used: {human_size(used)}\n"
                f"Available: {human_size(available)}"
            )

        except Exception:
            # The widget exists, so showing a compact fallback is now safe.
            try:
                disk_card.update(
                    "[b]Storage[/b]\nUnavailable"
                )
            except Exception:
                pass

    def can_expand(self, node: ScanNode):
        return bool(
            node.children
            or (
                node.metadata.get("lazy_expandable")
                and not node.metadata.get("children_loaded")
            )
        )

    def name_text(self, node: ScanNode, depth: int):
        t = Text()
        t.append("  " * depth)
        if self.can_expand(node):
            if node.path in self.expanded and node.metadata.get("children_loaded"):
                t.append("▼ ", style="cyan")
            else:
                t.append("▶ ", style="cyan")
        else:
            t.append("  ")

        if node.metadata.get("project"):
            t.append("▣ ", style="bright_blue")
        elif node.metadata.get("action_type") == "delete_cache":
            t.append("■ ", style="magenta")
        else:
            t.append("■ ", style="bright_black")
        t.append(node.name)
        return t

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

    def refresh_table(self):
        table = self.query_one("#tree", DataTable)
        table.clear()
        rows = self.flatten_visible()

        for idx, (depth, node) in enumerate(rows):
            table.add_row(
                self.name_text(node, depth),
                self.display_size(node),
                node.category,
                node.path
                if not node.metadata.get("virtual", False)
                else "(virtual)",
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

    def refresh_nav(self):
        labels = {
            "workspaces": "Workspaces",
            "temporary": "Temporary",
            "caches": "Caches",
            "home": "Home",
        }
        icons = {
            "workspaces": "▣",
            "temporary": "◇",
            "caches": "◈",
            "home": "⌂",
        }

        for key, label in labels.items():
            if key == "workspaces":
                root = self.root_by_kind("workspaces")
                if root is None:
                    size_text = "—"
                else:
                    size_sum = sum(
                        c.size for c in root.children
                        if not c.metadata.get("size_pending", False)
                    )
                    pending = sum(
                        1 for c in root.children
                        if c.metadata.get("size_pending", False)
                    )
                    if pending:
                        size_text = (
                            f"{human_size(size_sum)}…"
                            if size_sum
                            else "…"
                        )
                    else:
                        size_text = (
                            human_size(size_sum)
                            if size_sum
                            else "0"
                        )
            else:
                root = self.root_by_kind(key)
                pending = bool(
                    root
                    and root.metadata.get(
                        "size_pending",
                        False,
                    )
                )
                size = (
                    root.size
                    if root
                    and not pending
                    else 0
                )
                size_text = (
                    "…"
                    if pending
                    else (
                        human_size(size)
                        if size
                        else "—"
                    )
                )

            dot = self._nav_status_dot(key)

            self.query_one(
                f"#nav_{key}",
                Button,
            ).label = Text.from_markup(
                f"{dot} {icons[key]} {label}   [#9aa5b8]{size_text}[/]"
            )

    def _nav_status_dot(self, region: str) -> str:
        root = self.region_roots.get(region)
        meta = self.cache.region_meta(region)
        if root is None:
            return "[dim]○[/]"
        if meta and meta["stale"]:
            return "[yellow]◐[/]"
        return "[green]●[/]"

    def refresh_section(self):
        data = {
            "workspaces": (
                "Workspaces",
                "Development workspaces found by project marker files. Sizes are measured on demand.",
            ),
            "temporary": (
                "Temporary",
                "Explicit /private/tmp development-temp candidates.",
            ),
            "caches": (
                "Caches",
                "Explicit known reproducible developer caches.",
            ),
            "home": (
                "Home",
                "全盘扫描结果：家目录大目录 + 精确大小。\"全盘扫描\"覆盖全部四个区域"
                "（工作区 + 临时 + 缓存 + 本视图），入口在本视图的重新扫描或工具栏 Full Scan。",
            ),
        }
        title, subtitle = data[self.active_category]
        self.query_one("#section_title", Static).update(title)
        self.query_one("#section_subtitle", Static).update(subtitle)
        self.query_one("#rescan_region", Button).disabled = (
            self.active_category not in REGIONS
        )

    def refresh_ui(self):
        self.refresh_nav()
        self.refresh_section()
        self.refresh_table()

    # ---------- details ----------

    def show_details(self, node: ScanNode):
        self.selected_path = node.path
        self.query_one("#details_title", Static).update(node.name)
        self.query_one("#details_path", Static).update(
            "Virtual group" if node.metadata.get("virtual") else node.path
        )

        lines = [
            "[b]Details[/b]",
            "",
            f"Size             [b]{self.display_size(node)}[/b]",
            f"Type             {node.category}",
            f"Scanned          {self.cache.describe_age(max(0, time.time() - node.scanned_at))} ago",
        ]

        project = node.metadata.get("project")
        if project:
            lines += [
                "",
                "[b]Project[/b]",
                f"Type             {project.get('project_type')}",
                f"Detected by      {project.get('detected_by')}",
                f"Command          {project.get('label')}",
            ]
            if project.get("note"):
                lines.append(f"Note             {project.get('note')}")

        action = node.metadata.get("action_type", "none")
        lines += ["", "[b]Available action[/b]"]
        if action == "project_clean":
            lines += [
                f"[green]{project.get('label')}[/green]",
                "Runs in this exact project directory.",
            ]
        elif action == "delete_cache":
            lines += [
                "[red]Delete explicit cache/temp directory[/red]",
                node.reason,
            ]
        elif action == "delete_manual":
            lines += [
                "[red]Delete permanently…[/red]",
                "This removes exactly the selected file/directory.",
                node.reason,
            ]
        else:
            lines += ["Inspect only", node.reason]

        if self.can_expand(node) and not node.metadata.get("children_loaded"):
            lines += [
                "",
                "[cyan]Contents not scanned yet.[/cyan]",
                "Scan & Expand reads only the next real filesystem level.",
                "No recursive size scan is run first.",
            ]

        self.query_one("#details_body", Static).update("\n".join(lines))
        self.update_action_buttons(node)

    def clear_details(self):
        self.query_one("#details_title", Static).update("No items")
        self.query_one("#details_path", Static).update("")
        self.query_one("#details_body", Static).update("Nothing indexed here.")
        self.update_action_buttons(None)

    def update_action_buttons(self, node):
        action_btn = self.query_one("#run_action", Button)

        if not node:
            action_btn.disabled = True
            action_btn.label = "No Action"
            return

        action = node.metadata.get("action_type", "none")

        # A filesystem operation may continue in the background while the user
        # browses the tree, but starting a second clean/delete is blocked.
        if self._operation_running:
            action_btn.disabled = True
            action_btn.label = "Operation running…"
            return

        if action == "project_clean":
            project = node.metadata.get("project") or {}
            action_btn.disabled = False
            action_btn.variant = "success"
            action_btn.label = project.get("label", "Run project clean")
        elif action in ("delete_cache", "delete_manual"):
            action_btn.disabled = False
            action_btn.variant = "error"
            size_text = (
                "…"
                if node.metadata.get("size_pending")
                else human_size(node.size)
            )
            action_btn.label = f"Delete {size_text}"
        else:
            action_btn.disabled = True
            action_btn.label = "No Action"

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
            btn = self.query_one(f"#nav_{key}", Button)
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
            self.query_one("#status", Static).update(
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
            self.query_one("#status", Static).update(
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
            self.query_one("#status", Static).update(
                f"Scan failed: {exc}"
            )
            return

        self.replace_node(node.path, fresh)
        self.expanded.add(node.path)
        self.selected_path = node.path
        self.save_cache()
        self.refresh_ui()

        # Expand first so the UI is immediate, then calculate the exact
        # direct-child sizes in the background. This is one `du -d 1` traversal
        # for the expanded directory, not one scan per child.
        self.schedule_child_sizing(node.path)

        if not operation_in_background:
            self.set_progress(
                100,
                f"Expanded: {node.name}",
            )
            self.query_one("#status", Static).update(
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
            self.query_one("#status", Static).update(
                "A clean/delete operation is running; exact sizing is unavailable."
            )
            return

        if (
            not node.metadata.get("is_dir", False)
            or not node.metadata.get("children_loaded", False)
            or not node.children
        ):
            self.query_one("#status", Static).update(
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
            if current.metadata.get("container"):
                current.size = sum(
                    x.size for x in current.children
                    if not x.metadata.get("size_pending")
                )
            elif not current.metadata.get("size_pending"):
                current.size = max(0, current.size + delta)
            current = self.find_node(current.parent_path)

    def save_cache(self):
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
            "temporary": "临时目录 /private/tmp",
            "caches": "缓存目录 Caches",
            "home": "Home 全量（大目录 + 精确大小）",
        }
        lines = ["将扫描:"]
        if to_scan:
            lines += [f"• {labels[r]}" for r in to_scan]
        else:
            lines.append("• （4 个区域均为最新，无需扫描）")
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
            self.query_one("#status", Static).update(
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
                    "4 个区域均为最新（30 分钟内已扫），当前无需扫描。\n\n"
                    "仍要强制全部重扫吗？",
                    destructive=False,
                )
            )
            if not ok:
                self.set_progress(100, "All regions are fresh; nothing to do.")
                self.query_one("#status", Static).update(
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
            self.set_progress(0, "Full scan cancelled.")
            self.query_one("#status", Static).update("Full scan cancelled.")
            return

        notes = [
            f"跳过 {region}（{self.cache.describe_age(meta['age'])} 前）"
            for region, meta in skip
        ]
        self._scanning_regions = to_scan
        self.set_progress(5, "Starting full scan…")
        self.query_one("#status", Static).update(
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
                self.query_one("#status", Static).update(
                    f"{label} failed: {exc}"
                )
                continue
            self.region_roots[region] = root
            self.cache.save_region(region, [root] if root else [])

        self._scanning_regions = None
        self.selected_path = None
        self.set_default_expansion()
        self.refresh_ui()
        self.update_disk_card()
        self.schedule_workspace_sizing()
        self.set_progress(100, "Full scan complete.")
        self.query_one("#status", Static).update(
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
            self.query_one("#status", Static).update(
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
                self.query_one("#status", Static).update(
                    f"{label} failed: {exc}"
                )
                continue
            self.region_roots[region] = root
            self.cache.save_region(region, [root] if root else [])

        self._scanning_regions = None
        self.selected_path = None
        self.set_default_expansion()
        self.refresh_ui()
        self.schedule_workspace_sizing()
        self.set_progress(100, "Scan complete.")
        self.query_one("#status", Static).update(
            f"Scanned: {', '.join(regions)}."
        )

    def action_force_full_scan(self):
        if self._operation_running:
            self.query_one("#status", Static).update(
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
        self.query_one("#status", Static).update(
            f"Workspace project sizes measured in the background "
            f"({done}/{len(pending)})."
        )

    async def refresh_selected(self, node: ScanNode):
        self.set_progress(10, f"Refreshing / measuring selected item: {node.name}")
        self.query_one("#status", Static).update(
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
            self.query_one("#status", Static).update(
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
            self.query_one("#status", Static).update(
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
        elif action in ("delete_cache", "delete_manual"):
            action_label = f"Delete {self.display_size(node)}"

        self.query_one("#status", Static).update(
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
                f"Current size: {self.display_size(node)}\n\n"
                "This permanently removes this explicit known cache/temp directory.\n"
                "The tree is updated automatically after deletion."
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
            self.set_progress(0, "Action cancelled.")
            self.query_one("#status", Static).update("Action cancelled.")
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
        self.query_one("#status", Static).update(
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
            self.query_one("#status", Static).update(
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
            self.query_one("#status", Static).update(
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
                self.query_one("#status", Static).update(
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
            self.query_one("#status", Static).update(
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
            self.query_one("#status", Static).update(
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
