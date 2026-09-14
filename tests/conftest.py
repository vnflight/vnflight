"""Shared test configuration — ensure src/vnflight package is importable."""

import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_src = os.path.join(_root, "src")

# Insert src/ at the front of sys.path so the vnflight *package* is found
# first.  The single-file build lives in dist/ (not on sys.path), so it
# cannot shadow the package; the guards below stay for tests that load
# the artifact on purpose.
if _src not in sys.path:
    sys.path.insert(0, _src)
if _root not in sys.path:
    sys.path.insert(1, _root)

# Clear any cached single-file vnflight import so the package is discovered.
if "vnflight" in sys.modules and not hasattr(sys.modules["vnflight"], "__path__"):
    del sys.modules["vnflight"]


import pytest


@pytest.fixture(autouse=True)
def _vnflight_package_isolation():
    """Keep the src/vnflight PACKAGE discoverable for every test.

    Some test modules (test_spawners, test_cli_runner) insert the repo root
    ahead of src/ at import time and then trigger an ``import vnflight``, which
    resolves to the single-file vnflight.py and caches it in sys.modules —
    shadowing the package for any LATER test that lazily imports
    ``vnflight.format`` (e.g. test_vn_anomaly via _format_state_for_ui), whose
    output then silently changes (the import falls back to a different anomaly
    summary). After each test, drop a single-file cache and re-assert src-first
    so the package is rediscovered. Tests that need the single-file bind it at
    import time, so clearing the sys.modules cache here doesn't disturb their
    already-bound reference.
    """
    # Collection of another test tree can import the flat artifact after this
    # conftest was initialized but before the first test body runs. Repair both
    # sides of the test boundary, not only teardown from an earlier test.
    if "vnflight" in sys.modules and not hasattr(sys.modules["vnflight"], "__path__"):
        del sys.modules["vnflight"]
    try:
        sys.path.remove(_src)
    except ValueError:
        pass
    sys.path.insert(0, _src)
    yield
    if "vnflight" in sys.modules and not hasattr(sys.modules["vnflight"], "__path__"):
        del sys.modules["vnflight"]
    try:
        sys.path.remove(_src)
    except ValueError:
        pass
    sys.path.insert(0, _src)
