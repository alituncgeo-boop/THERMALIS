"""Proje (şehir/bölge çalışma alanı) kaydı.

Her proje veri kökünde ``projects/<project_id>/project.json`` olarak durur.
``project_id`` değişmezdir; görünen ad değiştirilebilir.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from .storage import read_json, write_json_atomic
from .text import match_key, slugify

PROJECT_SCHEMA_VERSION = "1.0"
PROJECT_ID_PATTERN = re.compile(r"^prj-[a-z0-9-]{1,60}$")


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)

    @field_validator("name", "description")
    @classmethod
    def _strip(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("name")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value:
            raise ValueError("name must not be blank")
        return value


class Project(BaseModel):
    schema_version: str = PROJECT_SCHEMA_VERSION
    id: str
    name: str
    description: str = ""
    created_at: datetime


class ProjectError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def projects_dir(data_root: Path) -> Path:
    return data_root / "projects"


def is_valid_project_id(project_id: str) -> bool:
    return bool(PROJECT_ID_PATTERN.match(project_id))


def list_projects(data_root: Path) -> list[Project]:
    root = projects_dir(data_root)
    if not root.is_dir():
        return []
    projects: list[Project] = []
    for entry in root.iterdir():
        manifest = entry / "project.json"
        if entry.is_dir() and is_valid_project_id(entry.name) and manifest.is_file():
            projects.append(Project.model_validate(read_json(manifest)))
    return sorted(projects, key=lambda p: p.created_at)


def get_project(data_root: Path, project_id: str) -> Project | None:
    if not is_valid_project_id(project_id):
        return None
    manifest = projects_dir(data_root) / project_id / "project.json"
    if not manifest.is_file():
        return None
    return Project.model_validate(read_json(manifest))


def create_project(data_root: Path, payload: ProjectCreate) -> Project:
    wanted = match_key(payload.name)
    if any(match_key(p.name) == wanted for p in list_projects(data_root)):
        raise ProjectError("project_name_exists")

    project_id = f"prj-{slugify(payload.name, fallback='project')}-{secrets.token_hex(3)}"
    project_dir = projects_dir(data_root) / project_id
    project_dir.mkdir(parents=True, exist_ok=False)

    project = Project(
        id=project_id,
        name=payload.name,
        description=payload.description,
        created_at=datetime.now(timezone.utc),
    )
    write_json_atomic(project_dir / "project.json", project.model_dump(mode="json"))
    return project
