from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class DBConfig:
    """프로젝트별 DB 접속 설정."""

    db_type: str = "postgresql"  # "postgresql" or "sqlite"
    env_file: str = ""  # PostgreSQL: 프로젝트 루트 기준 상대경로 (e.g. "app/.env")
    env_prefix: dict[str, str] = field(
        default_factory=dict
    )  # PostgreSQL: {논리명: 환경변수 접두사} e.g. {"ra": "POSTGRESQL_RA"}
    model_paths: list[str] = field(
        default_factory=list
    )  # 스키마 파악용 모델 경로
    db_path: str = ""  # SQLite: DB 파일 경로 (프로젝트 루트 기준 상대경로)


@dataclass
class ProjectConfig:
    name: str
    path: str
    commands: list[str] = field(default_factory=list)
    description: str = ""
    wiki: bool = False
    db: DBConfig | None = None
    mcp_tools: list[str] = field(default_factory=list)
    status_paths: list[str] = field(default_factory=list)


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

    _DEFAULT_MCP_TOOLS = [
        "jira_*",
        "linear_*",
        "notion_*",
        "slack_*",
    ]

    projects: dict[str, ProjectConfig] = {}
    for name, cfg in raw.get("projects", {}).items():
        # DB 설정: 새 포맷(db: {...}) 우선, 구 포맷(db_backend: true) 폴백
        db_config: DBConfig | None = None
        if "db" in cfg and isinstance(cfg["db"], dict):
            db_raw = cfg["db"]
            db_config = DBConfig(
                db_type=db_raw.get("db_type", "postgresql"),
                env_file=db_raw.get("env_file", ""),
                env_prefix=db_raw.get("env_prefix", {}),
                model_paths=db_raw.get("model_paths", []),
                db_path=db_raw.get("db_path", ""),
            )
        elif cfg.get("db_backend", False):
            # 하위호환: db_backend: true → 기존 ra/core 기본값
            db_config = DBConfig(
                env_file="app/.env",
                env_prefix={"ra": "POSTGRESQL_RA", "core": "POSTGRESQL_CORE"},
                model_paths=["app/models/ra", "app/models/core"],
            )

        # MCP 도구: 명시적 설정 우선, 없으면 commands가 있는 프로젝트는 기본 MCP 제공
        mcp_tools = cfg.get("mcp_tools", [])
        if not mcp_tools and cfg.get("commands", []):
            mcp_tools = list(_DEFAULT_MCP_TOOLS)

        projects[name] = ProjectConfig(
            name=name,
            path=cfg["path"],
            commands=cfg.get("commands", []),
            description=cfg.get("description", ""),
            wiki=cfg.get("wiki", False),
            db=db_config,
            mcp_tools=mcp_tools,
            status_paths=cfg.get("status_paths", []),
        )

    security_raw = raw.get("security", {})
    security = SecurityConfig(
        allowed_users=security_raw.get("allowed_users", {}),
    )

    return AppConfig(projects=projects, security=security)
