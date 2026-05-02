from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

for path in (PROJECT_ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

os.environ.setdefault("DASHBOARD_TOKEN", "test-dashboard-token")

SRC_MODULES = [
    "cerebellum",
    "cerebellum.arbiter",
    "cerebellum.cortex",
    "cerebellum.events",
    "cerebellum.hippocampus",
    "cerebellum.http_safe",
    "cerebellum.observatory_main",
    "cerebellum.models",
    "cerebellum.ui",
    "cerebellum.ui.cortex_routes",
    "cerebellum.ui.dashboard",
    "cerebellum.instruments",
    "cerebellum.instruments.cron_instrument",
]

SCRIPT_MODULES = {
    "scripts.arbiter_loop": SCRIPTS_DIR / "arbiter_loop.py",
    "scripts.generate_hypotheses": SCRIPTS_DIR / "generate_hypotheses.py",
    "scripts.cluster_episodes": SCRIPTS_DIR / "cluster_episodes.py",
    "scripts.mine_causal_edges": SCRIPTS_DIR / "mine_causal_edges.py",
}


def _import_script(module_name: str, module_path: Path) -> object:
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_import_all_modules_smoke() -> None:
    for module_name in SRC_MODULES:
        assert importlib.import_module(module_name) is not None

    for module_name, module_path in SCRIPT_MODULES.items():
        assert _import_script(module_name, module_path) is not None
