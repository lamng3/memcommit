"""Integration tests use sibling clones (../aegis, ../moira, ../kroma, ../ontodb) when they exist, else skip."""

import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]

for project in ("aegis", "moira", "kroma"):
    src = SOURCE / project / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.append(str(src))
