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


@dataclass
class SecurityConfig:
    allowed_users: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class AppConfig:
    projects: dict[str, ProjectConfig]
    security: SecurityConfig


def load_projects(config_path: str | None = None) -> AppConfig:
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

    security_raw = raw.get("security", {})
    security = SecurityConfig(
        allowed_users=security_raw.get("allowed_users", {}),
    )

    return AppConfig(projects=projects, security=security)
