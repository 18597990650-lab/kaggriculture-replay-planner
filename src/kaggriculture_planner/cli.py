from __future__ import annotations

import argparse
import json
from .metadata import get_project_info, load_validation_summary


def cmd_info() -> None:
    info = get_project_info()
    validation = load_validation_summary()
    payload = {
        "root": str(info.root),
        "agent_entrypoint": str(info.agent_entrypoint.relative_to(info.root)),
        "scheme_doc": str(info.scheme_doc.relative_to(info.root)),
        "validation_available": bool(validation),
        "structural_status": {
            "agent_entrypoint_exists": info.agent_entrypoint.exists(),
            "scheme_doc_exists": info.scheme_doc.exists(),
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Kaggriculture Replay Planner project utilities")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("info", help="Show project paths and basic status")
    args = parser.parse_args()
    if args.command in (None, "info"):
        cmd_info()


if __name__ == "__main__":
    main()
