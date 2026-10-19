from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Optional

from .detectors import detect_project_action
from .models import ScanNode
from .utils import (
    MIB,
    file_size,
    gdu_json_depth1_sizes,
    gdu_size,
    has_cache_tag,
    open_file_users,
    run,
)

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

PRIVATE_TMP = Path("/private/tmp")

# Common build outputs of a recognised project. With a standard clean command
# available, the project clean owns them instead of a direct Delete.
BUILD_OUTPUT_NAMES = {
    "target", "build", "dist", "out", ".next", ".nuxt", ".turbo", "coverage",
}

_UNSET = object()


def _contains_cache_tag(path: Path, depth: int = 2) -> int:
    """Count CACHEDIR.TAG-marked directories at `path` and up to `depth` below."""
    if has_cache_tag(path):
        return 1
    if depth <= 0:
        return 0
    count = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    count += _contains_cache_tag(Path(entry.path), depth - 1)
    except OSError:
        pass
    return count


class Scanner:
    """
    Generic filesystem scanner:
    - no Codex/Claude/Cursor special project logic
    - exact real hierarchy only
    - project type comes only from standard marker files
    - lazy expand never runs recursive du first
    """

    def __init__(self, progress: Progress = None, tmp_base: Path = PRIVATE_TMP):
        self.progress = progress or (lambda _p, _m: None)
        self.tmp_base = tmp_base

    def _emit(self, pct: int, msg: str):
        self.progress(max(0, min(100, pct)), msg)

    def known_cache_label(self, path: Path):
        return KNOWN_CACHE_PATHS.get(_norm(path))

    def is_private_tmp_candidate(self, path: Path) -> bool:
        """
        Any direct child of the temp base owned by the current user. Names are
        not trusted: tools pick arbitrary temp names, and a name whitelist
        hid a 98 GB build directory.
        """
        try:
            if path.parent.resolve() != self.tmp_base.resolve():
                return False
            return path.lstat().st_uid == os.getuid()
        except OSError:
            return False

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
        parent_project=_UNSET,
    ) -> ScanNode:
        is_dir = path.is_dir() and not path.is_symlink()
        try:
            mtime = path.lstat().st_mtime
        except OSError:
            mtime = None

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

        cache_tagged = is_dir and not noise_blocked and has_cache_tag(path)

        # Keep build outputs owned by a parent project's standard clean
        # command under that project clean rather than exposing direct Delete.
        # A CACHEDIR.TAG directory inside such a project (e.g. a configured
        # Cargo target-dir) is also a build output of that project.
        parent_clean_owns_item = False

        if (
            action_type == "none"
            and not noise_blocked
            and parent_path
            and (path.name in BUILD_OUTPUT_NAMES or cache_tagged)
        ):
            if parent_project is _UNSET:
                try:
                    parent_project = detect_project_action(Path(parent_path))
                except Exception:
                    parent_project = None

            if parent_project and parent_project.command:
                parent_clean_owns_item = True
                reason = (
                    f"Build output of parent {parent_project.project_type} "
                    f"project. Prefer {parent_project.label} at the project root."
                )

        if cache_tagged and action_type == "none" and not parent_clean_owns_item:
            node_category = "Build cache"
            reason = (
                "Marked with CACHEDIR.TAG: the tool that created it declares "
                "the contents regenerable."
            )
            action_type = "delete_cache"
            project_data = None

        if is_dir and not noise_blocked and self.is_private_tmp_candidate(path):
            tagged = _contains_cache_tag(path)
            if tagged:
                node_category = "Build cache"
                reason = (
                    f"Temporary directory owned by you with {tagged} "
                    "CACHEDIR.TAG build cache(s) inside."
                )
            else:
                node_category = "Temporary"
                reason = "Temporary directory owned by you under /private/tmp."
            action_type = "delete_cache"
            project_data = None

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
            "lazy_expandable": bool(is_dir and not noise_blocked),
            "action_type": action_type,
            "size_pending": size_pending,
            "mtime": mtime,
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
        parent_project = detect_project_action(path)

        for idx, entry in enumerate(entries, 1):
            self._emit(int(idx / total * 100), f"Listing {path.name}: {entry.name}")
            try:
                if entry.is_symlink():
                    continue
                child = self.make_real_node(
                    entry,
                    parent_path=str(path),
                    measure_size=False,
                    parent_project=parent_project,
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
        Measure direct children with ONE parallel GDU traversal of the parent.
        Results match `du -x -k -d 1` while running 1.4-5x faster.
        """
        if parent.metadata.get("virtual"):
            return {}

        path = Path(parent.path)

        if not path.exists() or not path.is_dir():
            return {}

        self._emit(20, f"Exact child sizing: {path.name}")
        _total, result = gdu_json_depth1_sizes(path)
        self._emit(95, f"Exact child sizing complete: {path.name}")
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

    def discover_projects(self) -> list[ScanNode]:
        """
        Walk the home directory and return a node per directory holding a
        project marker file. Sizes are NOT measured here.

        Dependency/build-cache directories (NOISE_DIRS) and known cache roots
        are pruned so multi-million-file directories are never traversed.
        """
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
        return found

    def scan_workspaces(self) -> Optional[ScanNode]:
        """Development workspaces discovered by project marker files."""
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

        found = self.discover_projects()
        found.sort(key=lambda n: n.name.lower())
        root.children = found
        root.size = 0
        self._emit(100, f"Workspace discovery complete: {len(found)} projects")
        return root if found else None

    def git_common_dirs(self, project_paths: list[Path]) -> list[Path]:
        """Unique git common dirs (the shared .git) of the given directories."""
        common: dict[str, Path] = {}
        for path in project_paths:
            rc, output = run(
                ["git", "-C", str(path), "rev-parse",
                 "--path-format=absolute", "--git-common-dir"],
                timeout=30,
            )
            if rc != 0 or not output.startswith("/"):
                continue
            resolved = Path(output.splitlines()[-1]).resolve()
            common.setdefault(str(resolved), resolved)
        return list(common.values())

    def linked_worktrees(self, common_dir: Path) -> list[dict]:
        """
        Linked (non-main) worktrees of one repository from
        `git worktree list --porcelain`.
        """
        rc, output = run(
            ["git", f"--git-dir={common_dir}", "worktree", "list", "--porcelain"],
            timeout=30,
        )
        if rc != 0:
            return []

        blocks = [b for b in output.split("\n\n") if b.strip()]
        result = []
        # The first block is always the main worktree.
        for block in blocks[1:]:
            info = {"common_dir": str(common_dir), "branch": "detached HEAD",
                    "locked": False, "path": None}
            for line in block.splitlines():
                key, _, value = line.partition(" ")
                if key == "worktree":
                    info["path"] = value
                elif key == "branch":
                    info["branch"] = value.removeprefix("refs/heads/")
                elif key == "locked":
                    info["locked"] = True
            if info["path"] and Path(info["path"]).is_dir():
                result.append(info)
        return result

    def worktree_changes(self, path: Path) -> int | None:
        """Count of modified + untracked entries, or None when unknown."""
        rc, output = run(
            ["git", "-C", str(path), "status", "--porcelain"],
            timeout=300,
        )
        if rc != 0:
            return None
        return len([line for line in output.splitlines() if line.strip()])

    def scan_worktrees(self, project_paths: list[Path] | None = None) -> Optional[ScanNode]:
        """
        Linked git worktrees of every discovered repository, wherever they
        live (inside the repo, under a tool's home directory, in /tmp…).
        """
        root = ScanNode(
            path="__worktrees__",
            name="Worktrees",
            category="Worktrees",
            reason="Linked git worktrees of the discovered repositories.",
            metadata={
                "virtual": True,
                "root_kind": "worktrees",
                "container": True,
                "children_loaded": True,
                "lazy_expandable": True,
                "action_type": "none",
                "size_pending": False,
            },
        )

        if project_paths is None:
            self._emit(5, "Worktrees: discovering repositories…")
            project_paths = [Path(n.path) for n in self.discover_projects()]

        self._emit(30, "Worktrees: listing git worktrees…")
        infos = []
        seen = set()
        for common_dir in self.git_common_dirs(project_paths):
            for info in self.linked_worktrees(common_dir):
                key = str(Path(info["path"]).resolve())
                if key not in seen:
                    seen.add(key)
                    infos.append(info)

        users = open_file_users([Path(i["path"]) for i in infos])
        total = max(1, len(infos))
        children = []

        for idx, info in enumerate(infos, 1):
            path = Path(info["path"])
            self._emit(35 + int(60 * idx / total), f"Worktree {idx}/{total}: {path}")
            node = self.make_real_node(path, root.path, measure_size=True)
            changes = self.worktree_changes(path)
            node.category = "Git worktree"
            node.metadata.pop("project", None)
            node.metadata["worktree"] = {
                "common_dir": info["common_dir"],
                "branch": info["branch"],
                "locked": info["locked"],
                "changes": changes,
            }
            node.metadata["in_use_by"] = users.get(str(path), [])

            if info["locked"]:
                node.metadata["action_type"] = "none"
                node.reason = "Worktree is locked (git worktree lock)."
            elif changes is None:
                node.metadata["action_type"] = "none"
                node.reason = "git status failed; cannot tell whether work would be lost."
            elif changes:
                node.metadata["action_type"] = "none"
                node.reason = (
                    f"{changes} uncommitted or untracked change(s). "
                    "Commit or discard them before removing the worktree."
                )
            else:
                node.metadata["action_type"] = "worktree_remove"
                node.reason = (
                    "Clean worktree. git worktree remove deletes the checkout "
                    f"and its ignored build output; branch {info['branch']} is kept."
                )
            children.append(node)

        children.sort(key=lambda x: x.size, reverse=True)
        root.children = children
        root.size = sum(x.size for x in children)
        self._emit(100, f"Worktrees: {len(children)} found")
        return root if children else None

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
        base = self.tmp_base
        if not base.exists():
            return None

        root = ScanNode(
            path=str(base),
            name=str(base),
            category="Temporary",
            reason="Items of yours under the system temp directory.",
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

        # ONE GDU traversal sizes every direct child (files included).
        self._emit(10, f"GDU scanning {base}…")
        base_total, sizes = gdu_json_depth1_sizes(base)

        candidates = []
        for key, size in sizes.items():
            path = Path(key)
            if size < MIN_ROOT_SIZE or path.is_symlink():
                continue
            if path.is_dir() and not self.is_private_tmp_candidate(path):
                continue
            if not path.is_dir():
                try:
                    if path.lstat().st_uid != os.getuid():
                        continue
                except OSError:
                    continue
            candidates.append((path, size))

        users = open_file_users([p for p, _ in candidates])
        children = []
        total = max(1, len(candidates))

        for idx, (path, size) in enumerate(candidates, 1):
            self._emit(70 + int(25 * idx / total), f"Temporary {idx}/{total}: {path.name}")
            node = self.make_real_node(path, str(base), known_size=size)
            if not node.metadata["is_dir"]:
                node.category = "Temporary file"
                node.metadata["action_type"] = "delete_manual"
                node.reason = "File owned by you under the system temp directory."
            node.metadata["in_use_by"] = users.get(str(path), [])
            children.append(node)

        children.sort(key=lambda x: x.size, reverse=True)
        root.children = children
        # The whole temp directory, not just the listed items, so the storage
        # card can attribute it.
        root.size = base_total if base_total is not None else sum(x.size for x in children)
        return root

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
        if kind == "worktrees":
            return self.scan_worktrees()
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
