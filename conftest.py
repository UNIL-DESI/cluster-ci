import sys
import types
import os
import pytest

_root = os.path.dirname(os.path.abspath(__file__))
for _p in [_root, os.path.join(_root, "src"), os.path.join(_root, "src", "scheduler"), os.path.join(_root, "src", "runner")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Ensure tests run with clean environment variables by default
if "HEADNODE_URL" not in os.environ:
    os.environ["HEADNODE_URL"] = "http://localhost:5000"

os.environ["CLUSTER_TOKEN"] = ""

# Provide a mock fcntl module on Windows for test collection compatibility
if sys.platform == "win32" and "fcntl" not in sys.modules:
    mock_fcntl = types.ModuleType("fcntl")
    mock_fcntl.LOCK_EX = 2
    mock_fcntl.LOCK_SH = 1
    mock_fcntl.LOCK_NB = 4
    mock_fcntl.LOCK_UN = 8
    mock_fcntl.flock = lambda fd, op: None
    sys.modules["fcntl"] = mock_fcntl


@pytest.fixture(autouse=True)
def clean_cluster_token_for_tests(monkeypatch):
    """Ensure headnode_service.CLUSTER_TOKEN is unset by default unless test explicitly configures it."""
    try:
        import headnode_service as hs
        if os.environ.get("CLUSTER_TOKEN", "") == "":
            monkeypatch.setattr(hs, "CLUSTER_TOKEN", None)
    except Exception:
        pass
