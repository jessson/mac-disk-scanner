from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Label,
    ProgressBar,
    Static,
)

from .actions import ActionRunner
from .cache import ScanCache
from .models import ScanNode
from .scanner import Scanner
from .utils import human_size


class ConfirmScreen(ModalScreen[bool]):
    CSS = """
    ConfirmScreen {
        align: center middle;
        background: rgba(0,0,0,0.78);
    }

    #dialog {
        width: 84;
        height: auto;
        max-height: 26;
        background: #111722;
        border: round #4b5b73;
        padding: 1 2;
    }

    #title {
        text-style: bold;
        margin-bottom: 1;
    }

    #buttons {
        height: 3;
        margin-top: 1;
        align-horizontal: right;
    }

    #buttons Button {
        min-width: 12;
        margin-left: 1;
    }
    """

    def __init__(
        self,
        title: str,
        message: str,
        destructive: bool = False,
    ):
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
                    "Confirm",
                    id="confirm",
                    variant="error" if self.destructive else "success",
                )

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "cancel":
            self.dismiss(False)
        elif event.button.id == "confirm":
            self.dismiss(True)


class CleanerApp(App):
    TITLE = "MacDiskScanner v2"
    SUB_TITLE = "Real filesystem • standard project clean commands"

    CSS = """
    Screen {
        background: #0a0e14;
        color: #dce3ee;
    }

    Header {
        background: #121824;
        color: #f4f7fb;
    }

    #toolbar {
        height: 5;
        padding: 1 2;
        background: #101621;
        border-bottom: solid #253043;
        align-vertical: middle;
    }

    #toolbar_title {
        width: 1fr;
        text-style: bold;
        content-align: left middle;
    }

    #cache_info {
        width: 28;
        content-align: right middle;
        color: #98a6ba;
        margin-right: 1;
    }

    #toolbar Button {
        min-width: 18;
        margin-left: 1;
    }

    #body {
        height: 1fr;
    }

    #sidebar {
        width: 25;
        background: #0f151f;
        border-right: solid #253043;
        padding: 1;
    }

    #sidebar_title {
        height: 2;
        text-style: bold;
        color: #8998ac;
        padding-left: 1;
    }

    .nav {
        width: 100%;
        height: 3;
        margin-bottom: 1;
        background: #0f151f;
        color: #cbd4e0;
        border: none;
        content-align: left middle;
    }

    .nav:hover {
        background: #182233;
    }

    .nav.active {
        background: #1e6fd9;
        color: white;
        text-style: bold;
    }

    #spacer {
        height: 1fr;
    }

    #disk_card, #cache_card {
        height: auto;
        min-height: 5;
        padding: 1;
        margin-top: 1;
        background: #121a26;
        border: round #27354a;
        color: #aeb9c8;
    }

    #center {
        width: 1fr;
        min-width: 66;
    }

    #section {
        height: 5;
        padding: 1 2;
    }

    #section_title {
        text-style: bold;
        color: #f3f6fa;
    }

    #section_subtitle {
        color: #8e9baf;
    }

    #tree {
        height: 1fr;
        margin: 0 2;
        background: #0b1017;
        border: round #253043;
    }

    DataTable > .datatable--header {
        background: #192231;
        color: #f0f4fa;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: #243650;
        color: white;
    }

    #right {
        width: 43;
        min-width: 38;
        background: #0f151f;
        border-left: solid #253043;
        padding: 1 2;
    }

    #details_title {
        height: auto;
        min-height: 2;
        text-style: bold;
        color: #f4f7fb;
    }

    #details_path {
        height: auto;
        max-height: 4;
        color: #8391a4;
        margin-bottom: 1;
    }

    #details_body {
        height: 1fr;
        color: #c5cfdb;
    }

    #actions {
        height: auto;
        min-height: 15;
        border-top: solid #253043;
        padding-top: 1;
    }

    #actions Button {
        width: 100%;
        height: 3;
        margin-bottom: 1;
    }

    #progress_area {
        height: 5;
        padding: 0 2;
        background: #101621;
        border-top: solid #253043;
    }

    #progress_text {
        height: 1;
        color: #9ba9bc;
    }

    ProgressBar {
        height: 2;
    }

    #status {
        height: 2;
        padding: 0 2;
        background: #0c1119;
        color: #a7b2c2;
    }
    """

    BINDINGS = [
        ("enter", "toggle_expand", "Expand / scan"),
        ("left", "collapse", "Collapse"),
        ("right", "expand", "Expand"),
        ("r", "refresh_selected", "Refresh selected"),
        ("shift+r", "force_full_scan", "Full scan"),
        ("q", "quit", "Quit"),
    ]

    NAV = ("overview", "codex", "projects", "temporary", "caches")

    def __init__(self):
        super().__init__()
        self.cache = ScanCache()
        self.runner = ActionRunner()

        self.roots: list[ScanNode] = []
        self.visible_nodes: list[ScanNode] = []

        self.active_category = "overview"
        self.expanded: set[str] = set()
        self.selected_path: Optional[str] = None
        self.cache_created_at: Optional[float] = None

    # --------------------------------------------------------------
    # Compose
    # --------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()

        with Horizontal(id="toolbar"):
            yield Static("MacDiskScanner v2", id="toolbar_title")
            yield Static("Cache: -", id="cache_info")
            yield Button("Refresh Selected", id="refresh", variant="primary")
            yield Button("Force Full Scan", id="fullscan")

        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("STORAGE", id="sidebar_title")
                yield Button("Overview", id="nav_overview", classes="nav active")
                yield Button("Codex", id="nav_codex", classes="nav")
                yield Button("Projects", id="nav_projects", classes="nav")
                yield Button("Temporary", id="nav_temporary", classes="nav")
                yield Button("Caches", id="nav_caches", classes="nav")
                yield Static("", id="spacer")
                yield Static("Disk", id="disk_card")
                yield Static("Cache", id="cache_card")

            with Vertical(id="center"):
                with Vertical(id="section"):
                    yield Static("Overview", id="section_title")
                    yield Static(
                        "Only real filesystem nodes are shown.",
                        id="section_subtitle",
                    )

                table = DataTable(id="tree", cursor_type="row", zebra_stripes=True)
                table.add_columns("Name", "Size", "Type", "Available Action")
                yield table

            with Vertical(id="right"):
                yield Static("Select an item", id="details_title")
                yield Static("", id="details_path")
                yield Static(
                    "Click a directory to scan or expand it.",
                    id="details_body",
                )

                with Vertical(id="actions"):
                    yield Button("Scan & Expand", id="expand")
                    yield Button("No Automatic Action", id="run_action")
                    yield Button("Open in Finder", id="finder")
                    yield Button("Open in Terminal", id="terminal")

        with Vertical(id="progress_area"):
            yield Static("Ready.", id="progress_text")
            yield ProgressBar(total=100, id="progress")

        yield Static(
            "Project cleanup uses only detected standard project commands. "
            "Worktrees and unknown project data are not deleted.",
            id="status",
        )

        yield Footer()

    def on_mount(self):
        self.update_disk_card()

        cached = self.cache.load()
        if cached:
            self.roots, meta = cached
            self.cache_created_at = meta["created_at"]
            self.set_default_expansion()
            self.update_cache_info(meta)
            self.set_progress(100, "Loaded cached scan result.")
            self.refresh_ui()
            return

        stale = self.cache.load(allow_stale=True)
        if stale:
            self.roots, meta = stale
            self.cache_created_at = meta["created_at"]
            self.set_default_expansion()
            self.update_cache_info(meta)
            self.refresh_ui()

        self.action_force_full_scan()

    # --------------------------------------------------------------
    # Basic lookup
    # --------------------------------------------------------------

    def all_nodes(self):
        for root in self.roots:
            yield from root.walk()

    def find_node(self, path: Optional[str]) -> Optional[ScanNode]:
        if not path:
            return None
        for node in self.all_nodes():
            if node.path == path:
                return node
        return None

    def root_by_kind(self, kind: str) -> Optional[ScanNode]:
        for root in self.roots:
            if root.metadata.get("root_kind") == kind:
                return root
        return None

    def category_roots(self) -> list[ScanNode]:
        if self.active_category == "overview":
            return list(self.roots)

        mapping = {
            "codex": "codex",
            "projects": "projects",
            "temporary": "temporary",
            "caches": "caches",
        }

        root = self.root_by_kind(mapping[self.active_category])
        return [root] if root else []

    def current_node(self) -> Optional[ScanNode]:
        table = self.query_one("#tree", DataTable)
        row = table.cursor_row
        if row < 0 or row >= len(self.visible_nodes):
            return None
        return self.visible_nodes[row]

    # --------------------------------------------------------------
    # UI helpers
    # --------------------------------------------------------------

    def set_progress(self, pct: int, text: str):
        self.query_one("#progress", ProgressBar).update(
            progress=max(0, min(100, pct))
        )
        self.query_one("#progress_text", Static).update(text)

    def make_progress_scanner(self) -> Scanner:
        """
        Scanner work runs in asyncio.to_thread(), so scanner progress events
        occur on a worker thread. Route them safely back to Textual's UI thread.
        """
        def progress(pct: int, message: str):
            self.call_from_thread(
                self.set_progress,
                pct,
                message,
            )

        return Scanner(progress=progress)

    def update_disk_card(self):
        try:
            usage = shutil.disk_usage("/")
            used = usage.total - usage.free
            pct = used / usage.total * 100 if usage.total else 0

            self.query_one("#disk_card", Static).update(
                "[b]Disk Usage[/b]\n"
                f"{human_size(used)} / {human_size(usage.total)} ({pct:.0f}%)\n"
                f"Free: {human_size(usage.free)}"
            )
        except Exception:
            self.query_one("#disk_card", Static).update(
                "[b]Disk Usage[/b]\nUnavailable"
            )

    def update_cache_info(self, meta: dict):
        age = meta["age"]
        stale = meta.get("stale", False)
        text = self.cache.describe_age(age)
        suffix = " • stale" if stale else ""

        self.query_one("#cache_info", Static).update(
            f"Cache: {text}{suffix}"
        )

        ttl_left = max(0, meta["ttl"] - age)
        self.query_one("#cache_card", Static).update(
            "[b]Scan Cache[/b]\n"
            f"Age: {text}{suffix}\n"
            f"TTL left: {self.cache.describe_age(ttl_left)}"
        )

    def action_text(self, node: ScanNode) -> Text:
        action_type = node.metadata.get("action_type", "none")

        if action_type == "project_clean":
            project = node.metadata.get("project") or {}
            return Text(
                project.get("label", "Project clean"),
                style="bold green",
            )

        if action_type == "delete_cache":
            return Text("Delete cache/temp", style="bold red")

        project = node.metadata.get("project")
        if project and not project.get("command"):
            return Text(
                "No standard clean command",
                style="yellow",
            )

        return Text("Inspect only", style="dim")

    def can_expand(self, node: ScanNode) -> bool:
        return bool(
            node.children
            or (
                node.metadata.get("lazy_expandable")
                and not node.metadata.get("children_loaded")
            )
        )

    def name_text(self, node: ScanNode, depth: int) -> Text:
        text = Text()
        text.append("  " * depth)

        if self.can_expand(node):
            if (
                node.path in self.expanded
                and node.metadata.get("children_loaded")
            ):
                text.append("▼ ", style="cyan")
            else:
                text.append("▶ ", style="cyan")
        else:
            text.append("  ")

        project = node.metadata.get("project")
        if project:
            text.append("▣ ", style="bright_blue")
        elif node.category == "Codex worktree":
            text.append("▣ ", style="blue")
        elif node.metadata.get("action_type") == "delete_cache":
            text.append("■ ", style="magenta")
        else:
            text.append("■ ", style="bright_black")

        text.append(node.name)
        return text

    def flatten_visible(self) -> list[tuple[int, ScanNode]]:
        rows: list[tuple[int, ScanNode]] = []

        def visit(node: ScanNode, depth: int):
            rows.append((depth, node))
            if node.children and node.path in self.expanded:
                for child in sorted(
                    node.children,
                    key=lambda item: item.size,
                    reverse=True,
                ):
                    visit(child, depth + 1)

        for root in sorted(
            self.category_roots(),
            key=lambda item: item.size,
            reverse=True,
        ):
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
                human_size(node.size),
                node.category,
                self.action_text(node),
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
            "overview": "Overview",
            "codex": "Codex",
            "projects": "Projects",
            "temporary": "Temporary",
            "caches": "Caches",
        }

        for key, label in labels.items():
            if key == "overview":
                size = sum(root.size for root in self.roots)
            else:
                mapping = {
                    "codex": "codex",
                    "projects": "projects",
                    "temporary": "temporary",
                    "caches": "caches",
                }
                root = self.root_by_kind(mapping[key])
                size = root.size if root else 0

            button = self.query_one(f"#nav_{key}", Button)
            button.label = f"{label}   {human_size(size) if size else '—'}"

    def refresh_section(self):
        titles = {
            "overview": (
                "Overview",
                "Only real filesystem nodes are shown.",
            ),
            "codex": (
                "Codex",
                "Worktrees are inspect-only. Expand to find actual project directories.",
            ),
            "projects": (
                "Projects",
                "Detected project roots expose only standard project clean commands.",
            ),
            "temporary": (
                "Temporary",
                "Only explicit development-temp candidates are deletable.",
            ),
            "caches": (
                "Caches",
                "Only known reproducible caches are deletable.",
            ),
        }

        title, subtitle = titles[self.active_category]
        self.query_one("#section_title", Static).update(title)
        self.query_one("#section_subtitle", Static).update(subtitle)

    def refresh_ui(self):
        self.refresh_nav()
        self.refresh_section()
        self.refresh_table()

    # --------------------------------------------------------------
    # Details
    # --------------------------------------------------------------

    def show_details(self, node: ScanNode):
        self.selected_path = node.path

        self.query_one("#details_title", Static).update(node.name)
        self.query_one("#details_path", Static).update(
            "Virtual group" if node.metadata.get("virtual") else node.path
        )

        lines = [
            "[b]Details[/b]",
            "",
            f"Size             [b]{human_size(node.size)}[/b]",
            f"Type             {node.category}",
            f"Scanned          {self.cache.describe_age(max(0, time.time() - node.scanned_at))} ago",
        ]

        project = node.metadata.get("project")

        if project:
            lines += [
                "",
                "[b]Project detection[/b]",
                f"Type             {project.get('project_type')}",
                f"Detected by      {project.get('detected_by')}",
                f"Command          {project.get('label')}",
            ]

            if project.get("note"):
                lines.append(f"Note             {project.get('note')}")

        action_type = node.metadata.get("action_type", "none")

        lines += [
            "",
            "[b]Available action[/b]",
        ]

        if action_type == "project_clean":
            lines += [
                f"[green]{project.get('label')}[/green]",
                "Runs in the project root. The tool does not guess which build directories to delete.",
            ]
        elif action_type == "delete_cache":
            lines += [
                "[red]Delete this cache/temp directory[/red]",
                node.reason,
            ]
        else:
            lines += [
                "Inspect only",
                node.reason,
            ]

        if self.can_expand(node) and not node.metadata.get("children_loaded"):
            lines += [
                "",
                "[cyan]Contents not scanned yet.[/cyan]",
                "Scan & Expand reads only the next real filesystem level.",
            ]

        self.query_one("#details_body", Static).update(
            "\n".join(lines)
        )

        self.update_action_buttons(node)

    def clear_details(self):
        self.query_one("#details_title", Static).update("No items")
        self.query_one("#details_path", Static).update("")
        self.query_one("#details_body", Static).update(
            "Nothing indexed in this category."
        )
        self.update_action_buttons(None)

    def update_action_buttons(self, node: Optional[ScanNode]):
        expand_button = self.query_one("#expand", Button)
        action_button = self.query_one("#run_action", Button)

        if not node:
            expand_button.disabled = True
            action_button.disabled = True
            action_button.label = "No Automatic Action"
            return

        expand_button.disabled = not self.can_expand(node)

        if self.can_expand(node):
            if (
                node.metadata.get("lazy_expandable")
                and not node.metadata.get("children_loaded")
            ):
                expand_button.label = "Scan & Expand"
            elif node.path in self.expanded:
                expand_button.label = "Collapse"
            else:
                expand_button.label = "Expand"
        else:
            expand_button.label = "No Children"

        action_type = node.metadata.get("action_type", "none")

        if action_type == "project_clean":
            project = node.metadata.get("project") or {}
            action_button.disabled = False
            action_button.variant = "success"
            action_button.label = project.get("label", "Run project clean")
        elif action_type == "delete_cache":
            action_button.disabled = False
            action_button.variant = "error"
            action_button.label = f"Delete {human_size(node.size)}"
        else:
            action_button.disabled = True
            action_button.label = "No Automatic Action"

    # --------------------------------------------------------------
    # Navigation
    # --------------------------------------------------------------

    def set_default_expansion(self):
        self.expanded.clear()

        for root in self.category_roots():
            self.expanded.add(root.path)

            if self.active_category == "codex":
                for child in root.children:
                    if child.category == "Codex worktrees":
                        self.expanded.add(child.path)

    def switch_category(self, category: str):
        if category not in self.NAV:
            return

        self.active_category = category
        self.selected_path = None

        for key in self.NAV:
            button = self.query_one(f"#nav_{key}", Button)
            if key == category:
                button.add_class("active")
            else:
                button.remove_class("active")

        self.set_default_expansion()
        self.refresh_ui()

    # --------------------------------------------------------------
    # Lazy scan / expand
    # --------------------------------------------------------------

    async def lazy_expand(self, node: ScanNode):
        self.set_progress(
            5,
            f"Scanning one real level: {node.name}",
        )

        scanner = self.make_progress_scanner()

        try:
            fresh = await asyncio.to_thread(
                scanner.scan_children,
                node,
            )
        except Exception as exc:
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
        self.set_progress(
            100,
            f"Loaded direct children: {node.name}",
        )

    def toggle_expand(self, node: Optional[ScanNode] = None):
        node = node or self.current_node()
        if not node:
            return

        if (
            node.metadata.get("lazy_expandable")
            and not node.metadata.get("children_loaded")
        ):
            asyncio.create_task(self.lazy_expand(node))
            return

        if not node.children:
            return

        self.selected_path = node.path

        if node.path in self.expanded:
            self.expanded.remove(node.path)
        else:
            self.expanded.add(node.path)

        self.refresh_table()

    # --------------------------------------------------------------
    # Cache tree mutation
    # --------------------------------------------------------------

    def find_parent(self, target_path: str):
        for root in self.roots:
            for node in root.walk():
                for idx, child in enumerate(node.children):
                    if child.path == target_path:
                        return node, idx
        return None, None

    def replace_node(
        self,
        old_path: str,
        fresh: Optional[ScanNode],
    ):
        for idx, root in enumerate(self.roots):
            if root.path == old_path:
                if fresh is None:
                    self.roots.pop(idx)
                else:
                    self.roots[idx] = fresh
                return

        parent, idx = self.find_parent(old_path)
        if parent is None or idx is None:
            return

        old = parent.children[idx]
        old_size = old.size
        new_size = fresh.size if fresh else 0
        delta = new_size - old_size

        if fresh is None:
            parent.children.pop(idx)
        else:
            parent.children[idx] = fresh

        # Update measured aggregate containers upward.
        current = parent
        while current:
            if current.metadata.get("container"):
                current.size = sum(child.size for child in current.children)
            else:
                current.size = max(0, current.size + delta)

            current = self.find_node(current.parent_path)

    def save_cache(self):
        self.cache.save(
            self.roots,
            created_at=self.cache_created_at,
        )

    # --------------------------------------------------------------
    # Full / selected refresh
    # --------------------------------------------------------------

    async def full_scan(self):
        self.set_progress(
            0,
            "Full scan started — bypassing the 30-minute cache...",
        )
        self.query_one("#cache_info", Static).update(
            "Cache: bypassed for full scan"
        )
        self.query_one("#status", Static).update(
            "Scanning now. The current stage/item and percentage are shown below."
        )

        scanner = self.make_progress_scanner()
        roots = await asyncio.to_thread(scanner.full_scan)

        self.roots = roots
        self.cache_created_at = time.time()
        self.selected_path = None
        self.set_default_expansion()
        self.save_cache()

        self.refresh_ui()
        self.update_disk_card()

        self.update_cache_info(
            {
                "age": 0,
                "ttl": 30 * 60,
                "stale": False,
            }
        )

        self.set_progress(100, "Full scan complete.")
        self.query_one("#status", Static).update(
            "Full scan complete. Cache valid for 30 minutes."
        )

    def action_force_full_scan(self):
        asyncio.create_task(self.full_scan())

    async def refresh_selected(self, node: ScanNode):
        self.set_progress(
            10,
            f"Refreshing only: {node.name}",
        )

        scanner = self.make_progress_scanner()
        fresh = await asyncio.to_thread(
            scanner.rescan_node,
            node,
        )

        self.replace_node(node.path, fresh)
        self.save_cache()
        self.refresh_ui()
        self.update_disk_card()

        self.set_progress(
            100,
            f"Refreshed: {node.name}",
        )

    def action_refresh_selected(self):
        node = self.current_node()
        if node:
            asyncio.create_task(self.refresh_selected(node))

    # --------------------------------------------------------------
    # Action execution
    # --------------------------------------------------------------

    async def run_current_action(self):
        node = self.current_node()
        if not node:
            return

        action_type = node.metadata.get("action_type", "none")

        if action_type == "none":
            return

        project = node.metadata.get("project") or {}

        if action_type == "project_clean":
            title = project.get("label", "Project clean")
            message = (
                f"Project:\n{node.path}\n\n"
                f"Detected by: {project.get('detected_by')}\n"
                f"Command: {' '.join(project.get('command') or [])}\n\n"
                "The command will run in this project directory."
            )
            destructive = False
        else:
            title = "Delete cache / temporary directory"
            message = (
                f"{node.path}\n\n"
                f"Current size: {human_size(node.size)}\n\n"
                "This permanently removes this explicit cache/temp directory."
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
            return

        self.set_progress(
            10,
            f"Running: {title}",
        )

        try:
            freed, log = await asyncio.to_thread(
                self.runner.execute,
                node,
            )
        except Exception as exc:
            self.set_progress(100, "Action failed.")
            self.query_one("#status", Static).update(
                f"Action failed: {exc}"
            )
            return

        if action_type == "project_clean":
            # Re-read only this project, preserving expanded state.
            scanner = self.make_progress_scanner()
            fresh = await asyncio.to_thread(
                scanner.rescan_node,
                node,
            )
            self.replace_node(node.path, fresh)

            if node.path in self.expanded:
                fresh = self.find_node(node.path)
                if fresh and Path(fresh.path).is_dir():
                    fresh2 = await asyncio.to_thread(
                        scanner.scan_children,
                        fresh,
                    )
                    self.replace_node(node.path, fresh2)
        else:
            self.replace_node(node.path, None)
            self.selected_path = None

        self.save_cache()
        self.refresh_ui()
        self.update_disk_card()

        self.set_progress(
            100,
            f"Completed. Freed about {human_size(freed)}.",
        )

        self.query_one("#status", Static).update(
            f"{log} Freed about {human_size(freed)}."
        )

    # --------------------------------------------------------------
    # Events
    # --------------------------------------------------------------

    def on_data_table_row_highlighted(
        self,
        event: DataTable.RowHighlighted,
    ):
        if event.data_table.id != "tree":
            return

        try:
            idx = int(str(event.row_key.value))
        except Exception:
            return

        if 0 <= idx < len(self.visible_nodes):
            self.show_details(self.visible_nodes[idx])

    def on_data_table_row_selected(
        self,
        event: DataTable.RowSelected,
    ):
        if event.data_table.id != "tree":
            return

        try:
            idx = int(str(event.row_key.value))
        except Exception:
            return

        if 0 <= idx < len(self.visible_nodes):
            node = self.visible_nodes[idx]
            self.show_details(node)

            # Clicking a row only navigates/scans; it never deletes or cleans.
            if self.can_expand(node):
                self.toggle_expand(node)

    def on_button_pressed(self, event: Button.Pressed):
        bid = event.button.id or ""

        if bid.startswith("nav_"):
            self.switch_category(
                bid.removeprefix("nav_")
            )
        elif bid == "refresh":
            self.action_refresh_selected()
        elif bid == "fullscan":
            self.action_force_full_scan()
        elif bid == "expand":
            self.action_toggle_expand()
        elif bid == "run_action":
            asyncio.create_task(
                self.run_current_action()
            )
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

        if (
            node.metadata.get("lazy_expandable")
            and not node.metadata.get("children_loaded")
        ):
            self.toggle_expand(node)
        elif node.children and node.path not in self.expanded:
            self.expanded.add(node.path)
            self.selected_path = node.path
            self.refresh_table()

    def action_collapse(self):
        node = self.current_node()
        if (
            node
            and node.children
            and node.path in self.expanded
        ):
            self.expanded.remove(node.path)
            self.selected_path = node.path
            self.refresh_table()

    # --------------------------------------------------------------
    # Finder / terminal
    # --------------------------------------------------------------

    def current_real_path(self) -> Optional[Path]:
        node = self.current_node()

        if not node or node.metadata.get("virtual"):
            return None

        path = Path(node.path)
        return path if path.exists() else None

    def open_finder(self):
        path = self.current_real_path()
        if not path:
            return

        subprocess.Popen(
            ["open", "-R", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def open_terminal(self):
        path = self.current_real_path()
        if not path:
            return

        target = path if path.is_dir() else path.parent

        subprocess.Popen(
            ["open", "-a", "Terminal", str(target)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
