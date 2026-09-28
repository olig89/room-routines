"""The version is written in three places; they must agree."""

import json
import re
from pathlib import Path

from custom_components.room_routines.const import VERSION

ROOT = Path(__file__).parents[2] / "custom_components" / "room_routines"


def test_versions_agree():
    manifest = json.loads((ROOT / "manifest.json").read_text())["version"]
    js = (ROOT / "www" / "room-routines-panel.js").read_text(encoding="utf-8")
    panel = re.search(r'const PANEL_VERSION = "([^"]+)"', js).group(1)
    assert manifest == VERSION == panel
