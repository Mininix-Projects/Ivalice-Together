"""Prepare paths used by the test suite outside the publishable directory."""

import sys
from pathlib import Path

DEVELOPMENT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DEVELOPMENT / "source"))
sys.path.insert(0, str(DEVELOPMENT / "ferramentas"))

# Tests must represent Start with All even when the local launcher setting is
# disabled.
import coop_roster  # noqa: E402
import penelo_story_guard  # noqa: E402

penelo_story_guard.PROTECTED_MEMBER_IDS = coop_roster.SELECTABLE_MEMBER_IDS
