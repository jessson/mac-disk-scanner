from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Optional

from .detectors import detect_project_action
from .models import ScanNode
from .utils import MIB, gdu_size, gdu_top_level_sizes, gdu_json_depth1_sizes, file_size, run

HOME = Path.home()
MIN_ROOT_SIZE = 10 * MIB
MIN_LIST_FILE_SIZE = 10 * MIB
Progress = Optional[Callable[[int, str], None]]

# Workspace discovery walk.
MAX_WORKSPACE_DEPTH = 12

# Pruned at ANY depth during workspace discovery: dependency/build caches
# and VCS internals are never project content and can contain millions of
# files. They are handled by the Caches region or the delete actions.
NOISE_DIRS = {
    "node_modules", "target", "dist", "build", "out", ".next", ".nuxt",
    ".turbo", "coverage", ".venv", "venv", "env", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".git", ".svn", ".hg", ".Trash",
}

# Subdirectories whose children may still be projects.
EXPAND_BLOCKED_NOISE = {
    "node_modules", ".venv", "venv", "env", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".git", ".svn", ".hg", ".Trash",
}

# Top-level home entries that never contain a project workspace:
# the OS/app areas, tool caches, VM filesystems (OrbStack), and AI
# tool plugin directories.
SKIP_ROOT_NAMES = {
    "Library", ".cache", ".npm", ".bun", ".cargo", ".rustup", ".local",
    ".codex", ".oh-my-zsh", ".fzf", ".iterm2", ".Trash", "Applications",
    "OrbStack", "go",
    ".opencode", ".grok", ".kimi-code", ".cc-switch", ".trae-cn",
    ".vscode", ".cursor", ".claude",
}

# Marker files that identify a development workspace.
PROJECT_MARKERS = (
    "Cargo.toml", "package.json", "go.mod", "pom.xml",
    "build.gradle", "build.gradle.kts", "Makefile",
    "pyproject.toml", "setup.py",
)


def _norm(path: Path) -> str:
    try:
        return str(path.expanduser().resolve())
    except Exception:
        return str(path.expanduser())


KNOWN_CACHE_PATHS = {
    _norm(HOME / ".npm"): "npm cache",
    _norm(HOME / ".bun" / "install" / "cache"): "Bun cache",
    _norm(HOME / "Library" / "pnpm" / "store"): "pnpm store",
    _norm(HOME / ".cargo" / "registry" / "cache"): "Cargo registry cache",
    _norm(HOME / ".cargo" / "git"): "Cargo git cache",
    _norm(HOME / "Library" / "Developer" / "Xcode" / "DerivedData"): "Xcode DerivedData",
    _norm(HOME / ".codex" / "cache"): "application cache",
    _norm(HOME / ".codex" / ".tmp"): "application temp",
}

TMP_PREFIXES = ("cdtrader-", "codex-")
TMP_CONTAINS = ("gocache", "benchmark", "venv")
TMP_SUFFIXES = ("-target", "-build")


class Scanner:
    """
    Generic filesystem scanner:
    - no Codex/Claude/Cursor special project logic
    - exact real hierarchy only
    - project type comes only from standard marker files
    - lazy expand never runs recursive du first
    """

    def __init__(self, progress: Progress = None):
        self.progress = progress or (lambda _p, _m: None)

    def _emit(self, pct: int, msg: str):
        self.progress(max(0, min(100, pct)), msg)

    def known_cache_label(self, path: Path):
        return KNOWN_CACHE_PATHS.get(_norm(path))

    def is_private_tmp_candidate(self, path: Path) -> bool:
        try:
            parent = path.parent.resolve()
            if parent != Path("/private/tmp").resolve():
                return False
        except Exception:
            if str(path.parent) != "/private/tmp":
                return False

        name = path.name.lower()
        return (
            name.startswith(TMP_PREFIXES)
            or any(x in name for x in TMP_CONTAINS)
            or name.endswith(TMP_SUFFIXES)
        )

    def manual_delete_allowed(
        self,
        path: Path,
        parent_path: str | None,
    ) -> bool:
        """
        Offer manual Delete for ordinary inspected items, but not for the
        top-level Home entries or VCS metadata.
        """
        if parent_path is None:
            return False

        try:
            parent = Path(parent_path).expanduser().resolve()
            home = HOME.resolve()
        except Exception:
            parent = Path(parent_path).expanduser()
            home = HOME

        # Direct children of Home stay protected from manual delete.
        if parent == home:
            return False

        if path.name in {".git", ".svn", ".hg"}:
            return False

        return True

    def make_real_node(
        self,
        path: Path,
        parent_path: str | None,
        category: str | None = None,
        *,
        measure_size: bool = True,
        known_size: int | None = None,
    ) -> ScanNode:
        is_dir = path.is_dir() and not path.is_symlink()

        if is_dir:
            if known_size is not None:
                size = known_size
                size_pending = False
            elif measure_size:
                size = gdu_size(path)
                size_pending = False
            else:
                size = 0
                size_pending = True
        else:
            size = file_size(path)
            size_pending = False

        node_category = category or ("Directory" if is_dir else "File")
        reason = "Real filesystem item."
        action_type = "none"
        project_data = None

        # Dependency/build-cache/VCS directories are leaf nodes: not project
        # content, never expandable, and no automatic action. They are still
        # listed so their size is visible when a parent project is expanded.
        noise_blocked = is_dir and path.name in EXPAND_BLOCKED_NOISE

        if noise_blocked:
            node_category = "Dependency/cache"
            reason = (
                "Dependency, build-cache or VCS directory; not project content. "
                "Read-only here."
            )
        elif is_dir:
            project = detect_project_action(path)
            if project:
                project_data = {
                    "project_type": project.project_type,
                    "command": project.command,
                    "label": project.label,
                    "detected_by": project.detected_by,
                    "note": project.note,
                }
                node_category = f"{project.project_type} project"
                reason = (
                    f"Detected from {project.detected_by}. "
                    + (
                        f"Standard clean command: {project.label}."
                        if project.command
                        else project.note
                    )
                )
                if project.command:
                    action_type = "project_clean"

        cache_label = self.known_cache_label(path) if is_dir and not noise_blocked else None
        if cache_label:
            node_category = cache_label
            reason = "Explicit known reproducible cache/temp location."
            action_type = "delete_cache"
            project_data = None

        if is_dir and not noise_blocked and self.is_private_tmp_candidate(path):
            node_category = "Temporary directory"
            reason = "Explicit /private/tmp development temporary/build directory."
            action_type = "delete_cache"
            project_data = None

        # Keep common build outputs owned by a parent project's standard clean
        # command under that project clean rather than exposing direct Delete.
        parent_clean_owns_item = False

        if action_type == "none" and not noise_blocked and parent_path:
            try:
                parent_project = detect_project_action(
                    Path(parent_path)
                )
            except Exception:
                parent_project = None

            if (
                parent_project
                and parent_project.command
                and path.name in {
                    "target",
                    "build",
                    "dist",
                    "out",
                    ".next",
                    ".nuxt",
                    ".turbo",
                    "coverage",
                }
            ):
                parent_clean_owns_item = True
                reason = (
                    f"Build output of parent {parent_project.project_type} "
                    f"project. Prefer {parent_project.label} at the project root."
                )

        if (
            action_type == "none"
            and not parent_clean_owns_item
            and not noise_blocked
            and self.manual_delete_allowed(
                path,
                parent_path,
            )
        ):
            action_type = "delete_manual"
            reason = (
                "No standard clean action was detected. "
                "Manual permanent deletion is available."
            )

        metadata = {
            "real_path": True,
            "is_dir": is_dir,
            "children_loaded": False,
            "lazy_expandable": bool(
                is_dir
                and action_type != "delete_cache"
                and not noise_blocked
            ),
            "action_type": action_type,
            "size_pending": size_pending,
        }
        if project_data:
            metadata["project"] = project_data

        return ScanNode(
            path=str(path),
            name=path.name,
            category=node_category,
            size=size,
            parent_path=parent_path,
            reason=reason,
            metadata=metadata,
        )

    def scan_children(self, node: ScanNode) -> ScanNode:
        path = Path(node.path)
        if not path.exists() or not path.is_dir():
            fresh = ScanNode.from_dict(node.to_dict())
            fresh.children = []
            fresh.scanned_at = time.time()
            fresh.metadata["children_loaded"] = True
            fresh.metadata["lazy_expandable"] = False
            return fresh

        fresh = self.make_real_node(
            path,
            node.parent_path,
            category=node.category,
            measure_size=False,
            known_size=node.size if not node.metadata.get("size_pending", False) else None,
        )

        for key in ("root_kind", "container", "virtual"):
            if key in node.metadata:
                fresh.metadata[key] = node.metadata[key]

        fresh.children = []
        fresh.metadata["children_loaded"] = True
        fresh.metadata["lazy_expandable"] = True

        try:
            entries = list(path.iterdir())
        except OSError as exc:
            fresh.reason = f"Could not list directory: {exc}"
            return fresh

        children = []
        total = max(1, len(entries))

        for idx, entry in enumerate(entries, 1):
            self._emit(int(idx / total * 100), f"Listing {path.name}: {entry.name}")
            try:
                if entry.is_symlink():
                    continue
                child = self.make_real_node(
                    entry,
                    parent_path=str(path),
                    measure_size=False,
                )
                if entry.is_dir() or child.size >= MIN_LIST_FILE_SIZE:
                    children.append(child)
            except (OSError, PermissionError):
                continue

        children.sort(
            key=lambda x: (
                0 if x.metadata.get("size_pending") else 1,
                x.size,
                x.name.lower(),
            ),
            reverse=True,
        )
        fresh.children = children
        fresh.scanned_at = time.time()
        return fresh

    def measure_children_sizes(
        self,
        parent: ScanNode,
    ) -> dict[str, int]:
        """
        Measure direct children exactly with ONE system du traversal.

        Why not use GDU here?
        ---------------------
        GDU remains the fast backend for broad/top-level discovery, but its
        non-interactive top-level accounting may differ from the system du
        result for some trees (hard links, filesystem metadata/error flags,
        or output/accounting differences).

        The user-facing expanded-directory view should match macOS `du`.
        This command is equivalent in spirit to:

            /usr/bin/du -x -k -d 1 <parent>

        It scans the parent once — NOT once per child.
        """
        if parent.metadata.get("virtual"):
            return {}

        path = Path(parent.path)

        if not path.exists() or not path.is_dir():
            return {}

        self._emit(
            20,
            f"Exact child sizing: {path.name}",
        )

        rc, output = run(
            [
                "/usr/bin/du",
                "-x",
                "-k",
                "-d",
                "1",
                str(path),
            ],
            timeout=1800,
        )

        try:
            parent_abs = path.resolve()
        except Exception:
            parent_abs = path

        result: dict[str, int] = {}

        for raw_line in output.splitlines():
            line = raw_line.strip()

            if not line:
                continue

            parts = line.split(None, 1)

            if len(parts) != 2:
                continue

            size_kb, raw_path = parts

            try:
                size = int(size_kb) * 1024
            except ValueError:
                # stderr is merged into stdout by run(); skip error text.
                continue

            candidate = Path(raw_path)

            if not candidate.is_absolute():
                candidate = parent_abs / candidate

            try:
                candidate_abs = candidate.resolve()
            except Exception:
                candidate_abs = candidate

            # du -d 1 also prints the parent total; keep only direct children.
            try:
                if candidate_abs.parent != parent_abs:
                    continue
            except Exception:
                continue

            result[str(candidate_abs)] = size

        if not result and rc != 0:
            raise RuntimeError(
                f"du failed for {path} (exit {rc}): "
                f"{output[-1200:]}"
            )

        self._emit(
            95,
            f"Exact child sizing complete: {path.name}",
        )

        return result

    def measure_node_size(self, node: ScanNode) -> ScanNode | None:
        """
        Measure exactly this node with gdu.

        This is deliberately separate from lazy expansion:
        - lazy expansion lists real children immediately
        - this method may be slow because directory size uses gdu
        """
        if node.metadata.get("virtual"):
            return ScanNode.from_dict(node.to_dict())

        path = Path(node.path)
        if not path.exists():
            return None

        fresh = ScanNode.from_dict(node.to_dict())
        if path.is_dir():
            fresh.size = gdu_size(path)
        else:
            fresh.size = file_size(path)

        fresh.metadata["size_pending"] = False
        fresh.scanned_at = time.time()
        return fresh

    def scan_workspaces(self) -> Optional[ScanNode]:
        """
        Discover development workspaces by walking the home directory and
        matching project marker files.

        Sizes are NOT measured here: a hit is recorded as size_pending and
        exact sizes are calculated on demand when the user expands it.
        Dependency/build-cache directories (NOISE_DIRS) and known cache roots
        are pruned so multi-million-file directories are never traversed.
        """
        root = ScanNode(
            path="__workspaces__",
            name="Workspaces",
            category="Workspaces",
            reason="Development workspaces discovered by project marker files.",
            metadata={
                "virtual": True,
                "root_kind": "workspaces",
                "container": True,
                "children_loaded": True,
                "lazy_expandable": True,
                "action_type": "none",
                "size_pending": False,
            },
        )

        found = []
        home = HOME
        visited = 0

        def walk(path: Path, depth: int):
            nonlocal visited
            if depth >= MAX_WORKSPACE_DEPTH:
                return
            try:
                with os.scandir(path) as it:
                    entries = list(it)
            except OSError:
                return

            visited += 1
            if visited % 250 == 0:
                self._emit(
                    5 + min(75, visited // 100),
                    f"Workspace discovery: {visited} directories…",
                )

            # One scandir call provides every entry name; marker matching
            # below is pure set lookup, no extra stat calls.
            names = {e.name for e in entries}

            if self.known_cache_label(path):
                return

            if any(marker in names for marker in PROJECT_MARKERS):
                node = self.make_real_node(
                    path,
                    str(path.parent),
                    measure_size=False,
                )
                if node.metadata.get("project"):
                    found.append(node)
                return

            for e in entries:
                if e.is_symlink() or not e.is_dir():
                    continue
                if e.name in NOISE_DIRS:
                    continue
                if path == home and e.name in SKIP_ROOT_NAMES:
                    continue
                walk(Path(e.path), depth + 1)

        walk(home, 0)

        found.sort(key=lambda n: n.name.lower())
        root.children = found
        root.size = 0
        self._emit(100, f"Workspace discovery complete: {len(found)} projects")
        return root if found else None

    def scan_home(self):
        root = ScanNode(
            path=str(HOME),
            name="Home",
            category="Home",
            reason="Large direct children of the user home directory.",
            metadata={
                "real_path": True,
                "root_kind": "home",
                "container": True,
                "is_dir": True,
                "children_loaded": True,
                "lazy_expandable": True,
                "action_type": "none",
                "size_pending": False,
            },
        )

        self._emit(
            5,
            "GDU JSON scanning Home…",
        )

        # ONE GDU scan, machine-readable raw-byte output.
        root_total, size_map = gdu_json_depth1_sizes(HOME)

        self._emit(
            65,
            "GDU Home scan complete; classifying entries…",
        )

        try:
            entries = [
                p
                for p in HOME.iterdir()
                if not p.is_symlink()
            ]
        except OSError:
            entries = []

        children = []

        for path in entries:
            try:
                if path.is_dir():
                    try:
                        key = str(path.resolve())
                    except Exception:
                        key = str(path)

                    known_size = size_map.get(key)

                    node = self.make_real_node(
                        path,
                        str(HOME),
                        measure_size=False,
                        known_size=known_size,
                    )
                else:
                    node = self.make_real_node(
                        path,
                        str(HOME),
                        measure_size=False,
                    )
            except (OSError, PermissionError):
                continue

            measured = not node.metadata.get(
                "size_pending",
                False,
            )

            # Never hide a real directory because its size was unavailable.
            keep_unknown_directory = (
                path.is_dir()
                and not measured
            )

            if (
                keep_unknown_directory
                or (measured and node.size >= MIN_ROOT_SIZE)
                or node.metadata.get("project")
                or node.metadata.get("action_type") == "delete_cache"
            ):
                children.append(node)

        children.sort(
            key=lambda x: (
                0 if x.metadata.get("size_pending") else 1,
                x.size,
                x.name.lower(),
            ),
            reverse=True,
        )

        root.children = children

        pending_count = sum(
            1
            for child in children
            if child.metadata.get("size_pending", False)
        )

        # Critical correctness rule:
        # never fabricate Home total from the handful of successfully parsed
        # children. Prefer GDU's root dsize; otherwise mark Home as pending.
        if root_total is not None:
            root.size = int(root_total)
            root.metadata["size_pending"] = False
        else:
            root.size = 0
            root.metadata["size_pending"] = True

        root.metadata["pending_size_count"] = pending_count
        root.metadata["measured_size_count"] = (
            len(children) - pending_count
        )

        self._emit(
            70,
            (
                f"Home classified: {len(children)} entries"
                + (
                    f", {pending_count} pending"
                    if pending_count
                    else ""
                )
            ),
        )

        return root

    def scan_private_tmp(self):
        base = Path("/private/tmp")
        if not base.exists():
            return None

        root = ScanNode(
            path=str(base),
            name="/private/tmp",
            category="Temporary",
            reason="Explicit development temp/build candidates.",
            metadata={
                "real_path": True,
                "root_kind": "temporary",
                "container": True,
                "is_dir": True,
                "children_loaded": True,
                "lazy_expandable": False,
                "action_type": "none",
                "size_pending": False,
            },
        )

        try:
            entries = [p for p in base.iterdir() if p.is_dir() and not p.is_symlink()]
        except OSError:
            entries = []

        candidates = [p for p in entries if self.is_private_tmp_candidate(p)]
        total = max(1, len(candidates))
        children = []

        for idx, path in enumerate(candidates, 1):
            self._emit(72 + int(12 * idx / total), f"Temporary {idx}/{total}: {path.name}")
            node = self.make_real_node(path, str(base), measure_size=True)
            if node.size >= MIN_ROOT_SIZE:
                children.append(node)

        children.sort(key=lambda x: x.size, reverse=True)
        root.children = children
        root.size = sum(x.size for x in children)
        return root if children else None

    def scan_known_caches(self):
        root = ScanNode(
            path="__known_caches__",
            name="Known caches",
            category="Caches",
            reason="Explicit known reproducible developer caches.",
            metadata={
                "virtual": True,
                "root_kind": "caches",
                "container": True,
                "children_loaded": True,
                "lazy_expandable": True,
                "action_type": "none",
                "size_pending": False,
            },
        )

        paths = [Path(raw) for raw in KNOWN_CACHE_PATHS if Path(raw).is_dir()]
        total = max(1, len(paths))
        children = []

        for idx, path in enumerate(paths, 1):
            self._emit(88 + int(9 * idx / total), f"Cache {idx}/{total}: {path}")
            node = self.make_real_node(path, root.path, measure_size=True)
            if node.size >= MIN_ROOT_SIZE:
                children.append(node)

        unique = {x.path: x for x in children}
        children = sorted(unique.values(), key=lambda x: x.size, reverse=True)
        root.children = children
        root.size = sum(x.size for x in children)
        return root if children else None

    def rescan_node(self, node: ScanNode):
        kind = node.metadata.get("root_kind")
        if kind == "workspaces":
            return self.scan_workspaces()
        if kind == "home":
            return self.scan_home()
        if kind == "temporary":
            return self.scan_private_tmp()
        if kind == "caches":
            return self.scan_known_caches()

        if node.metadata.get("virtual"):
            return node

        path = Path(node.path)
        if not path.exists():
            return None

        fresh = self.make_real_node(
            path,
            node.parent_path,
            measure_size=True,
        )

        if node.metadata.get("children_loaded") and path.is_dir():
            fresh = self.scan_children(fresh)

        return fresh
