from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .utils import makefile_has_clean


@dataclass
class ProjectAction:
    project_type: str
    command: Optional[list[str]]
    label: str
    detected_by: str
    note: str = ""


def _node_clean_action(path: Path) -> ProjectAction | None:
    package = path / "package.json"
    if not package.exists():
        return None

    try:
        data = json.loads(package.read_text(encoding="utf-8"))
    except Exception:
        data = {}

    scripts = data.get("scripts") or {}
    clean_script = scripts.get("clean") if isinstance(scripts, dict) else None

    if not clean_script:
        return ProjectAction(
            project_type="Node",
            command=None,
            label="No standard clean command",
            detected_by="package.json",
            note="package.json has no scripts.clean entry.",
        )

    if (path / "pnpm-lock.yaml").exists():
        command = ["pnpm", "run", "clean"]
        manager = "pnpm"
    elif (path / "yarn.lock").exists():
        command = ["yarn", "run", "clean"]
        manager = "yarn"
    elif (path / "bun.lock").exists() or (path / "bun.lockb").exists():
        command = ["bun", "run", "clean"]
        manager = "bun"
    else:
        command = ["npm", "run", "clean"]
        manager = "npm"

    return ProjectAction(
        project_type="Node",
        command=command,
        label=f"{manager} run clean",
        detected_by="package.json",
    )


def detect_project_action(path: Path) -> ProjectAction | None:
    if not path.is_dir():
        return None

    if (path / "Cargo.toml").exists():
        return ProjectAction(
            project_type="Rust",
            command=["cargo", "clean"],
            label="cargo clean",
            detected_by="Cargo.toml",
        )

    node = _node_clean_action(path)
    if node:
        return node

    if (path / "go.mod").exists():
        return ProjectAction(
            project_type="Go",
            command=["go", "clean", "./..."],
            label="go clean ./...",
            detected_by="go.mod",
            note="Project-scoped go clean. It does not purge the global Go build cache.",
        )

    if (path / "pom.xml").exists():
        if (path / "mvnw").exists():
            cmd = ["./mvnw", "clean"]
            label = "./mvnw clean"
        else:
            cmd = ["mvn", "clean"]
            label = "mvn clean"

        return ProjectAction(
            project_type="Maven",
            command=cmd,
            label=label,
            detected_by="pom.xml",
        )

    if (path / "build.gradle").exists() or (path / "build.gradle.kts").exists():
        if (path / "gradlew").exists():
            cmd = ["./gradlew", "clean"]
            label = "./gradlew clean"
        else:
            cmd = ["gradle", "clean"]
            label = "gradle clean"

        return ProjectAction(
            project_type="Gradle",
            command=cmd,
            label=label,
            detected_by="build.gradle*",
        )

    if makefile_has_clean(path):
        return ProjectAction(
            project_type="Make",
            command=["make", "clean"],
            label="make clean",
            detected_by="Makefile clean:",
        )

    if (path / "pyproject.toml").exists() or (path / "setup.py").exists():
        return ProjectAction(
            project_type="Python",
            command=None,
            label="No standard clean command",
            detected_by="pyproject.toml/setup.py",
            note="Python projects do not have one universal safe clean command.",
        )

    return None
