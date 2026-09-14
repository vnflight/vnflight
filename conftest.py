"""Repository-wide pytest import guard.

The repo contains both ``src/vnflight`` (the package under test) and the
root-level ``vnflight.py`` single-file build output.  Put ``src`` first
for tests so package imports stay deterministic across test directories.
"""

import os
import sys

_root = os.path.dirname(os.path.abspath(__file__))
_src = os.path.join(_root, "src")

try:
    sys.path.remove(_src)
except ValueError:
    pass
sys.path.insert(0, _src)
if _root not in sys.path:
    sys.path.insert(1, _root)

if "vnflight" in sys.modules and not hasattr(sys.modules["vnflight"], "__path__"):
    del sys.modules["vnflight"]


# Keep the hub's log tree out of the repo during tests.  harness.hub computes
# its events/runners directories at import time from HARNESS_LOGS_DIR, and
# subprocess hubs started by tests inherit the environment, so this has to be
# set before any test module imports the hub.  Without it, every in-process
# AgentSlot and every test hub wrote into harness/logs/events (about ten
# thousand residue files by Sep 2026), which the live hub's /sessions listing
# then globbed.
if not (os.environ.get("HARNESS_LOGS_DIR") or "").strip():
    import tempfile
    os.environ["HARNESS_LOGS_DIR"] = tempfile.mkdtemp(prefix="vnflight-test-logs-")
