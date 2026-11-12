"""Write openapi.json for the iOS app thread: python scripts/export_openapi.py"""

import json
import sys
import pathlib
import secrets
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from hub.config import Config  # noqa: E402
from hub.db import DB  # noqa: E402
from hub.main import build_app  # noqa: E402
from hub.state import Hub  # noqa: E402
from hub.vault import Vault  # noqa: E402

d = pathlib.Path(tempfile.mkdtemp())
app = build_app(Hub(Config(), Vault(d / "v.json", secrets.token_bytes(32)), DB(d / "h.db")), start_background=False)
out = pathlib.Path(__file__).resolve().parent.parent / "openapi.json"
out.write_text(json.dumps(app.openapi(), indent=1) + "\n")
print(f"wrote {out}")
