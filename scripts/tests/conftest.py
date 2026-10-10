"""Configure imports for duplication-gate script tests."""

import sys
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parents[1]
# Append the helper directory so the repository's regular `tests` package
# retains import precedence while the isolated suite resolves gate modules.
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.append(str(SCRIPT_DIRECTORY))
