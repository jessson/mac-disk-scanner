from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Optional

MIB = 1024 * 1024


def human_size(size: int) -> str:
    units = ["B", "K", "M", "G", "T"]
    value = float(max(0, size))
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return str(size)


def run(
    cmd: list[str],
    cwd: Optional[Path] = None,
    timeout: int = 1800,
) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip()
    except Exception as exc:
        return 1, str(exc)


def _is_dundee_gdu(candidate: str) -> bool:
    """
    Validate Dundee GDU by capabilities, not version-string formatting.
    """
    path = Path(candidate)

    if not path.exists() or not os.access(path, os.X_OK):
        return False

    rc, output = run(
        [str(path), "--help"],
        timeout=10,
    )

    if rc != 0:
        return False

    return (
        "--non-interactive" in output
        and "--no-prefix" in output
    )


@lru_cache(maxsize=1)
def gdu_binary() -> str:
    """
    Resolve the exact Dundee GDU binary.

    setup.sh writes `.gdu-bin` in the project root so the application doesn't
    accidentally pick GNU coreutils `gdu`.
    """
    candidates: list[str] = []

    override = os.environ.get("GDU_BIN")
    if override:
        candidate = (
            shutil.which(override)
            if "/" not in override
            else override
        )
        if candidate:
            candidates.append(str(candidate))

    # Exact path persisted by setup.sh.
    project_root = Path(__file__).resolve().parent.parent
    saved_path_file = project_root / ".gdu-bin"

    if saved_path_file.exists():
        try:
            saved = saved_path_file.read_text(
                encoding="utf-8"
            ).strip()
            if saved:
                candidates.append(saved)
        except OSError:
            pass

    # Canonical command.
    candidate = shutil.which("gdu-go")
    if candidate:
        candidates.append(candidate)

    # Standard Homebrew locations.
    candidates.extend([
        "/opt/homebrew/bin/gdu-go",
        "/usr/local/bin/gdu-go",
        "/opt/homebrew/opt/gdu/bin/gdu-go",
        "/usr/local/opt/gdu/bin/gdu-go",
    ])

    # Ask Homebrew directly.
    brew = shutil.which("brew")

    if brew:
        rc, listing = run(
            [brew, "list", "gdu"],
            timeout=15,
        )

        if rc == 0:
            for line in listing.splitlines():
                line = line.strip()
                if line.endswith("/bin/gdu-go"):
                    candidates.append(line)

        rc, prefix = run(
            [brew, "--prefix", "gdu"],
            timeout=15,
        )

        if rc == 0 and prefix.strip():
            candidates.append(
                str(
                    Path(prefix.strip())
                    / "bin"
                    / "gdu-go"
                )
            )

    seen = set()

    for candidate in candidates:
        if not candidate or candidate in seen:
            continue

        seen.add(candidate)

        if _is_dundee_gdu(candidate):
            return candidate

    raise RuntimeError(
        "Dundee GDU could not be found. Run ./setup.sh. "
        "On macOS the expected Homebrew executable is gdu-go."
    )

def default_gdu_ignore_dirs(scan_root: Path) -> list[Path]:
    """
    Directories to skip when they are descendants of scan_root.

    OrbStack exposes VM/container filesystems below ~/OrbStack. Traversing
    those from a host disk-usage scan is both slow and misleading, and can
    produce permission/cycle warnings.

    Extra comma-separated paths can be supplied with GDU_IGNORE_DIRS.
    """
    candidates: list[Path] = []

    orbstack = Path.home() / "OrbStack"
    if orbstack.exists():
        candidates.append(orbstack)

    extra = os.environ.get("GDU_IGNORE_DIRS", "")
    for raw in extra.split(","):
        raw = raw.strip()
        if raw:
            candidates.append(Path(raw).expanduser())

    try:
        root = scan_root.expanduser().resolve()
    except Exception:
        root = scan_root.expanduser()

    result: list[Path] = []

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate

        # Ignore only descendants, never the requested root itself.
        try:
            if resolved != root and resolved.is_relative_to(root):
                result.append(resolved)
        except AttributeError:
            # Python < 3.9 compatibility fallback.
            try:
                resolved.relative_to(root)
                if resolved != root:
                    result.append(resolved)
            except ValueError:
                pass
        except ValueError:
            pass

    return result


def _gdu_common_scan_args(path: Path) -> list[str]:
    args = [
        gdu_binary(),
        "--non-interactive",
        "--no-progress",
        "--no-color",
        "--no-unicode",
        "--no-cross",
    ]

    ignored = default_gdu_ignore_dirs(path)
    if ignored:
        args += [
            "--ignore-dirs",
            ",".join(str(p) for p in ignored),
        ]

    return args


def _parse_gdu_total(output: str, path: Path) -> int:
    candidates = []

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        match = re.match(r"^[!.@He]?\s*(\d+)(?:\s+|$)", line)
        if match:
            candidates.append(int(match.group(1)))

    if candidates:
        return candidates[-1]

    raise RuntimeError(
        f"Could not parse gdu output for {path}: {output[-1000:]!r}"
    )


def gdu_size(path: Path, timeout: int = 900) -> int:
    """
    Return disk usage in bytes using GDU's parallel non-interactive analyzer.
    """
    path = path.expanduser()

    if not path.exists() or path.is_symlink():
        return 0

    cmd = _gdu_common_scan_args(path) + [
        "--summarize",
        "--no-prefix",
        str(path),
    ]

    rc, output = run(cmd, timeout=timeout)
    if rc != 0:
        raise RuntimeError(
            f"gdu failed for {path} (exit {rc}): {output[-1200:]}"
        )

    return _parse_gdu_total(output, path)


def _parse_gdu_top_level(
    output: str,
    parent: Path,
) -> dict[str, int]:
    """
    Parse GDU non-interactive top-level output into:
        absolute child path -> disk usage bytes

    GDU may prefix an item with a one-character status flag:
    ! . @ H e
    """
    try:
        parent_abs = parent.expanduser().resolve()
    except Exception:
        parent_abs = parent.expanduser()

    result: dict[str, int] = {}

    for raw_line in output.splitlines():
        line = raw_line.strip()

        # The line may carry leading alignment whitespace before the size.
        # GDU may also prefix an item with a one-character status flag.
        match = re.match(
            r"^\s*(?:[!.@He]\s*)?(\d+)\s+(.+?)\s*$",
            line,
        )
        if not match:
            continue

        size = int(match.group(1))
        raw_name = match.group(2).strip()

        if not raw_name:
            continue

        candidate = Path(raw_name).expanduser()
        if not candidate.is_absolute():
            candidate = parent_abs / candidate

        try:
            candidate_abs = candidate.resolve()
        except Exception:
            candidate_abs = candidate

        # We only want REAL direct children of the scanned directory.
        try:
            direct_child = candidate_abs.parent == parent_abs
        except Exception:
            direct_child = False

        if not direct_child:
            # GDU may render the scanned root as "/" so a direct child
            # appears as "/name". Resolve that against the real parent.
            fallback = (parent_abs / raw_name.lstrip("/")).resolve()

            try:
                direct_child = fallback.parent == parent_abs
            except Exception:
                direct_child = False

            if direct_child:
                candidate_abs = fallback

        if not direct_child:
            continue

        result[str(candidate_abs)] = size

    return result


def gdu_top_level_sizes(
    path: Path,
    timeout: int = 1800,
) -> dict[str, int]:
    """
    Scan `path` ONCE and return sizes for its direct children.

    This is the key performance path for the cleaner. GDU's default
    non-interactive analyzer tracks top-level totals without building the
    complete tree in memory, so Python must NOT call gdu once per child.
    """
    path = path.expanduser()

    if not path.exists() or not path.is_dir():
        return {}

    cmd = _gdu_common_scan_args(path) + [
        "--no-prefix",
        str(path),
    ]

    rc, output = run(cmd, timeout=timeout)
    if rc != 0:
        raise RuntimeError(
            f"gdu top-level scan failed for {path} "
            f"(exit {rc}): {output[-1200:]}"
        )

    result = _parse_gdu_top_level(output, path)

    if not result:
        # Empty directories are valid, but a non-empty directory with no
        # parseable entries means our parser/output assumptions do not match.
        try:
            has_entries = any(path.iterdir())
        except OSError:
            has_entries = False

        if has_entries:
            raise RuntimeError(
                "gdu completed but no top-level entries could be parsed. "
                f"Output tail: {output[-1200:]!r}"
            )

    return result


def _walk_json_nodes(value):
    """Yield every dict node contained anywhere in GDU's JSON export."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _walk_json_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json_nodes(child)


def gdu_json_depth1_sizes(
    path: Path,
    timeout: int = 1800,
) -> tuple[int | None, dict[str, int]]:
    """
    Run ONE Dundee GDU JSON export at depth 1.

    Returns:
        (root_total_bytes_or_none, {absolute_direct_child_path: bytes})

    We deliberately use JSON rather than parsing GDU's terminal text.
    Exported dsize values are raw bytes.
    """
    path = path.expanduser()

    if not path.exists() or not path.is_dir():
        return None, {}

    try:
        root_abs = path.resolve()
    except Exception:
        root_abs = path

    try:
        direct_children = [
            child
            for child in path.iterdir()
            if not child.is_symlink()
        ]
    except OSError:
        direct_children = []

    ignored = set()
    for ignored_path in default_gdu_ignore_dirs(path):
        try:
            ignored.add(str(ignored_path.resolve()))
        except Exception:
            ignored.add(str(ignored_path))

    expected = {}
    basename_map = {}

    for child in direct_children:
        try:
            resolved = child.resolve()
        except Exception:
            resolved = child

        key = str(resolved)
        if key in ignored:
            continue

        expected[key] = child
        basename_map.setdefault(child.name, []).append(key)

    cmd = _gdu_common_scan_args(path) + [
        "--depth",
        "1",
        "--output-file",
        "-",
        "--output-attrs",
        "dsize",
        str(path),
    ]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:
        raise RuntimeError(
            f"GDU JSON scan failed for {path}: {exc}"
        ) from exc

    if proc.returncode != 0:
        raise RuntimeError(
            f"GDU JSON scan failed for {path} "
            f"(exit {proc.returncode}): "
            f"{proc.stderr[-1600:]}"
        )

    try:
        payload = json.loads(proc.stdout)
    except Exception as exc:
        raise RuntimeError(
            "GDU JSON output could not be decoded: "
            f"{proc.stdout[-1200:]!r}"
        ) from exc

    result: dict[str, int] = {}
    root_total: int | None = None

    for node in _walk_json_nodes(payload):
        if "name" not in node or "dsize" not in node:
            continue

        name = str(node.get("name") or "")
        raw_size = node.get("dsize")

        try:
            size = int(raw_size)
        except (TypeError, ValueError):
            continue

        if not name:
            continue

        candidate = Path(name).expanduser()

        # Root match.
        try:
            if candidate.is_absolute():
                resolved_candidate = candidate.resolve()
                if resolved_candidate == root_abs:
                    root_total = size
                    continue
        except Exception:
            pass

        normalized_name = name.rstrip("/")

        if normalized_name in {
            str(root_abs),
            str(path),
            path.name,
            ".",
        }:
            root_total = size
            continue

        matched_key = None

        # Absolute path.
        if candidate.is_absolute():
            try:
                key = str(candidate.resolve())
            except Exception:
                key = str(candidate)

            if key in expected:
                matched_key = key

        # Relative/basename depth-1 export.
        if matched_key is None:
            stripped = normalized_name
            if stripped.startswith("./"):
                stripped = stripped[2:]

            # A depth-1 child should have no remaining slash.
            if "/" not in stripped:
                keys = basename_map.get(stripped, [])
                if len(keys) == 1:
                    matched_key = keys[0]

        if matched_key is not None:
            result[matched_key] = size

    return root_total, result


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def makefile_has_clean(path: Path) -> bool:
    makefile = path / "Makefile"
    if not makefile.exists():
        return False

    try:
        text = makefile.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False

    return bool(re.search(r"(?m)^[ \t]*clean[ \t]*:", text))


def _protected_delete_paths() -> set[Path]:
    return {
        Path("/"),
        Path("/System"),
        Path("/Users"),
        Path.home(),
        Path.home() / "Library",
        Path.home() / ".codex",
        Path("/private"),
        Path("/private/tmp"),
    }


def _delete_with_macos_admin_prompt(path: Path) -> None:
    command = f"/bin/rm -rf -- {shlex.quote(str(path))}"
    script = (
        f"do shell script {json.dumps(command)} "
        f"with administrator privileges"
    )

    rc, output = run(
        ["/usr/bin/osascript", "-e", script],
        timeout=1800,
    )
    if rc != 0:
        raise RuntimeError(
            output or f"Administrator deletion failed for {path}"
        )


def safe_remove_path(path: Path) -> None:
    """
    Permanently remove one explicitly selected file or directory.
    """
    path = path.expanduser()

    protected = _protected_delete_paths()

    try:
        resolved = path.resolve()
    except Exception:
        resolved = path

    if path in protected or resolved in protected:
        raise RuntimeError(f"Protected path: {path}")

    if not path.exists() and not path.is_symlink():
        return

    first_error = None

    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except FileNotFoundError:
        return
    except (PermissionError, OSError) as exc:
        first_error = exc

    if not path.exists() and not path.is_symlink():
        return

    if platform.system() == "Darwin":
        _delete_with_macos_admin_prompt(path)
    else:
        raise RuntimeError(
            f"Could not delete {path}: {first_error}"
        )

    if path.exists() or path.is_symlink():
        raise RuntimeError(
            f"Deletion completed but path still exists: {path}"
        )


def safe_remove_directory(path: Path) -> None:
    safe_remove_path(path)

