from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


TMP_ROOT = Path(__file__).parent.parent / ".tmp"
TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("TMPDIR", str(TMP_ROOT))
tempfile.tempdir = str(TMP_ROOT)

# HTTP Basic Auth is fail-closed: create_app() refuses to start without DASHBOARD_USER /
# DASHBOARD_PASSWORD (app._apply_dashboard_auth). Tests that build the app must supply them.
os.environ.setdefault("DASHBOARD_USER", "test-user")
os.environ.setdefault("DASHBOARD_PASSWORD", "test-pass")

sys.path.insert(0, str(Path(__file__).parent.parent))

