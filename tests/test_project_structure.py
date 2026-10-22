from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]


def test_required_project_files_exist():
    required = [
        ROOT / "README.md",
        ROOT / "pyproject.toml",
        ROOT / "configs" / "default.yaml",
        ROOT / "docs" / "方案说明.md",
        ROOT / "agent" / "main.py",
        ROOT / "tools" / "package_agent.py",
    ]
    missing = [str(p.relative_to(ROOT)) for p in required if not p.exists()]
    assert not missing


def test_validation_metadata_is_json():
    path = ROOT / "VALIDATION.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)


def test_scheme_doc_is_neutral_and_descriptive():
    text = (ROOT / "docs" / "方案说明.md").read_text(encoding="utf-8")
    assert "方案描述" in text
    forbidden = ["排名", "作者", "Notebook", "leaderboard", "rank"]
    for word in forbidden:
        assert word not in text
