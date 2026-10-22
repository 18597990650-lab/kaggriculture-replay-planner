from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json


ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ProjectInfo:
    root: Path
    agent_entrypoint: Path
    scheme_doc: Path
    validation_file: Path


def get_project_info() -> ProjectInfo:
    return ProjectInfo(
        root=ROOT,
        agent_entrypoint=ROOT / "agent" / "main.py",
        scheme_doc=ROOT / "docs" / "方案说明.md",
        validation_file=ROOT / "VALIDATION.json",
    )


def load_validation_summary() -> dict:
    path = get_project_info().validation_file
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))
