from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from .models import ScanNode
from .utils import MIB, gdu_size, run, safe_remove_directory, safe_remove_path

ProgressCallback = Optional[Callable[[str], None]]
VERIFY_REMAINING_THRESHOLD = 10 * MIB


class ActionRunner:
    def _emit(
        self,
        progress: ProgressCallback,
        message: str,
    ) -> None:
        if progress:
            progress(message)

    def execute(
        self,
        node: ScanNode,
        progress: ProgressCallback = None,
    ) -> tuple[int, str]:
        action_type = node.metadata.get("action_type", "none")

        if action_type == "project_clean":
            return self.run_project_clean(node, progress)

        if action_type == "delete_cache":
            return self.delete_cache(node, progress)

        if action_type == "delete_manual":
            return self.delete_manual(node, progress)

        raise RuntimeError("This item has no executable action.")

    # -----------------------------------------------------------------
    # Project clean
    # -----------------------------------------------------------------

    def _cargo_target_directory(
        self,
        project_path: Path,
        progress: ProgressCallback,
    ) -> Path | None:
        self._emit(progress, "Reading Cargo target directory…")

        rc, output = run(
            [
                "cargo",
                "metadata",
                "--no-deps",
                "--format-version",
                "1",
            ],
            cwd=project_path,
            timeout=120,
        )

        if rc != 0:
            return None

        try:
            data = json.loads(output)
            raw = data.get("target_directory")
            return Path(raw) if raw else None
        except Exception:
            return None

    def _run_standard_command(
        self,
        command: list[str],
        cwd: Path,
        label: str,
        progress: ProgressCallback,
    ) -> str:
        command_text = " ".join(str(x) for x in command)
        self._emit(
            progress,
            f"Running {command_text}…",
        )

        rc, output = run(
            list(command),
            cwd=cwd,
            timeout=1800,
        )

        if rc != 0:
            self._emit(
                progress,
                f"{label} failed with exit code {rc}.",
            )
            raise RuntimeError(
                f"{label} failed ({rc}): {output[-1200:]}"
            )

        self._emit(
            progress,
            f"{label} exited successfully. Verifying cleanup…",
        )
        return output

    def _run_rust_clean(
        self,
        node: ScanNode,
        command: list[str],
        label: str,
        progress: ProgressCallback,
    ) -> tuple[int, str]:
        project_path = Path(node.path)

        target_dir = self._cargo_target_directory(
            project_path,
            progress,
        )

        # If cargo metadata is unavailable, a normal project/target is still
        # useful for verification. We do NOT delete it ourselves.
        direct_target = project_path / "target"

        verify_paths: list[Path] = []
        for candidate in (target_dir, direct_target):
            if candidate is None:
                continue
            try:
                normalized = candidate.resolve()
            except Exception:
                normalized = candidate

            if all(
                str(normalized) != str(existing)
                for existing in verify_paths
            ):
                verify_paths.append(normalized)

        before_sizes: dict[str, int] = {}
        for target in verify_paths:
            if target.exists():
                self._emit(
                    progress,
                    f"Measuring Cargo output before clean: {target}…",
                )
                before_sizes[str(target)] = gdu_size(target)

        output = self._run_standard_command(
            command,
            project_path,
            label,
            progress,
        )

        # Cargo may be configured today to use a different target_directory
        # while an older ./target is still present. It is still safer to use
        # Cargo itself, not rm -rf, to clean that standard Cargo target path.
        direct_target_exists = direct_target.exists()
        same_as_metadata = False
        if target_dir is not None:
            try:
                same_as_metadata = (
                    direct_target.resolve()
                    == target_dir.resolve()
                )
            except Exception:
                same_as_metadata = (
                    str(direct_target)
                    == str(target_dir)
                )

        if direct_target_exists and not same_as_metadata:
            remaining_direct = gdu_size(direct_target)
            if remaining_direct >= VERIFY_REMAINING_THRESHOLD:
                self._emit(
                    progress,
                    "A local ./target still exists; running "
                    "cargo clean --target-dir ./target…",
                )
                rc, fallback_output = run(
                    [
                        "cargo",
                        "clean",
                        "--target-dir",
                        str(direct_target),
                    ],
                    cwd=project_path,
                    timeout=1800,
                )
                if rc != 0:
                    raise RuntimeError(
                        "cargo clean succeeded for the configured target, "
                        "but cargo clean --target-dir ./target failed: "
                        f"{fallback_output[-1200:]}"
                    )
                if fallback_output:
                    output = (
                        (output + "\n" + fallback_output)
                        if output
                        else fallback_output
                    )

        freed = 0
        remaining: list[str] = []

        for target in verify_paths:
            before = before_sizes.get(str(target), 0)
            after = gdu_size(target) if target.exists() else 0
            freed += max(0, before - after)

            if after >= VERIFY_REMAINING_THRESHOLD:
                remaining.append(
                    f"{target} ({after} bytes still present)"
                )

        if remaining:
            # Do not silently claim success when the user still has a large
            # Cargo output directory after Cargo itself reported success.
            raise RuntimeError(
                f"{label} exited 0, but Cargo output is still large: "
                + "; ".join(remaining)
            )

        self._emit(
            progress,
            f"{label} verified successfully.",
        )

        log = f"{label} succeeded and Cargo output was verified."
        if output:
            log += " " + output[-800:]

        return freed, log

    def run_project_clean(
        self,
        node: ScanNode,
        progress: ProgressCallback = None,
    ) -> tuple[int, str]:
        project = node.metadata.get("project") or {}
        command = project.get("command")
        label = project.get("label", "project clean")
        project_type = project.get("project_type", "")

        if not command:
            raise RuntimeError(
                "No standard project clean command was detected."
            )

        path = Path(node.path)
        if not path.exists():
            raise RuntimeError(
                "Project directory no longer exists."
            )

        if project_type == "Rust":
            return self._run_rust_clean(
                node,
                list(command),
                label,
                progress,
            )

        self._emit(
            progress,
            f"Measuring project before {label}…",
        )
        before = gdu_size(path)

        output = self._run_standard_command(
            list(command),
            path,
            label,
            progress,
        )

        self._emit(
            progress,
            f"Measuring project after {label}…",
        )
        after = gdu_size(path)
        freed = max(0, before - after)

        self._emit(
            progress,
            f"{label} completed. Freed about {freed} bytes.",
        )

        log = (
            f"{label} succeeded. "
            f"{before} -> {after} bytes."
        )
        if output:
            log += " " + output[-800:]

        return freed, log

    # -----------------------------------------------------------------
    # Explicit cache/temp deletion
    # -----------------------------------------------------------------

    def delete_cache(
        self,
        node: ScanNode,
        progress: ProgressCallback = None,
    ) -> tuple[int, str]:
        """
        Delete must not depend on GDU succeeding.

        The node already has a cached scan size. Use that for the UI estimate,
        perform the filesystem deletion immediately, then verify the path is
        actually gone.
        """
        path = Path(node.path)

        if not path.exists():
            return 0, "Already absent."

        # Cached size is an estimate only; deletion correctness does not depend
        # on measuring the directory again.
        before = (
            node.size
            if not node.metadata.get("size_pending", False)
            else 0
        )

        self._emit(
            progress,
            f"Deleting {path}…",
        )

        safe_remove_directory(path)

        self._emit(
            progress,
            "Verifying deletion…",
        )

        if path.exists():
            raise RuntimeError(
                f"Delete returned but directory still exists: {path}"
            )

        self._emit(
            progress,
            f"Deleted {path.name} successfully.",
        )

        return before, f"Deleted {path}"

    def delete_manual(
        self,
        node: ScanNode,
        progress: ProgressCallback = None,
    ) -> tuple[int, str]:
        path = Path(node.path)

        if not path.exists() and not path.is_symlink():
            return 0, "Already absent."

        before = (
            node.size
            if not node.metadata.get("size_pending", False)
            else 0
        )

        self._emit(
            progress,
            f"Permanently deleting {path}…",
        )

        safe_remove_path(path)

        self._emit(
            progress,
            "Verifying deletion…",
        )

        if path.exists() or path.is_symlink():
            raise RuntimeError(
                f"Delete returned but path still exists: {path}"
            )

        self._emit(
            progress,
            f"Deleted {path.name} successfully.",
        )

        return before, f"Deleted {path}"

