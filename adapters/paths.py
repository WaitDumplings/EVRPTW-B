"""Repository-relative locations shared by runtime adapters and build tools."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPS = Path(os.environ.get("EVB_DEPS", ROOT / ".deps")).expanduser().resolve()
EV_DEPS = DEPS / "ev"
