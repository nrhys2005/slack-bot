from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ProjectConfig:
    name: str
    path: str
    commands: list[str] = field(default_factory=list)
    wiki: bool = False
    db_backend: bool = False


def load_projects(config_path: str | None = None) -> dict[str, ProjectConfig]:
    if config_path is None:
        config_path = os.environ.get(
            "PROJECTS_CONFIG",
            str(Path(__file__).resolve().parent.parent / "projects.yaml"),
        )

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    projects: dict[str, ProjectConfig] = {}
    for name, cfg in raw.get("projects", {}).items():
        projects[name] = ProjectConfig(
            name=name,
            path=cfg["path"],
            commands=cfg.get("commands", []),
            wiki=cfg.get("wiki", False),
            db_backend=cfg.get("db_backend", False),
        )
    return projects
