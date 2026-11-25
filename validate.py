"""Self-contained validation entrypoint for OneChain."""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


PROJECT_ROOT = Path(__file__).resolve().parent


def _python_files(root: Path) -> list[Path]:
    blocked = {"__pycache__", "venv", ".tmp", ".pytest_cache"}
    return sorted(path for path in root.rglob("*.py") if not blocked.intersection(path.parts))


def run_architecture_checks() -> dict[str, object]:
    python_files = _python_files(PROJECT_ROOT)
    sqlite_feature_violations: list[str] = []

    for path in python_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = {alias.name for alias in node.names}
                if path.is_relative_to(PROJECT_ROOT / "features") and "sqlite3" in imported:
                    sqlite_feature_violations.append(str(path))
            elif isinstance(node, ast.ImportFrom):
                if path.is_relative_to(PROJECT_ROOT / "features") and node.module == "sqlite3":
                    sqlite_feature_violations.append(str(path))

    return {
        "python_file_count": len(python_files),
        "sqlite_feature_violations": sorted(set(sqlite_feature_violations)),
        "ok": not sqlite_feature_violations,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate OneChain readiness.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if the runtime readiness report is degraded.",
    )
    args = parser.parse_args(argv)

    architecture = run_architecture_checks()
    try:
        from config import get_app_config
        from health import build_readiness_report
        from services.runtime import build_runtime_services

        config = get_app_config()
        services = build_runtime_services(config)
        readiness = build_readiness_report(config, services).to_dict()
    except ModuleNotFoundError as exc:
        readiness = {
            "app": "OneChain",
            "ready": False,
            "status": "degraded",
            "issues": [f"Missing dependency: {exc.name}"],
        }

    payload = {
        "architecture": architecture,
        "readiness": readiness,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))

    if not architecture["ok"]:
        return 1
    if args.strict and not readiness.get("ready", False):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
