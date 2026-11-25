from __future__ import annotations

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _python_files(root: Path) -> list[Path]:
    blocked = {"__pycache__", "venv", ".tmp", ".pytest_cache"}
    return sorted(path for path in root.rglob("*.py") if not blocked.intersection(path.parts))


def test_features_do_not_import_sqlite3():
    for path in _python_files(PROJECT_ROOT / "features"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = {alias.name for alias in node.names}
                assert "sqlite3" not in imported, f"{path} imports sqlite3"
            if isinstance(node, ast.ImportFrom):
                assert node.module != "sqlite3", f"{path} imports sqlite3"


