import sys
import types
import os

# Set dummy HEADNODE_URL if not set for testing
if "HEADNODE_URL" not in os.environ:
    os.environ["HEADNODE_URL"] = "http://localhost:5000"

# Provide a mock fcntl module on Windows for test collection compatibility
if sys.platform == "win32" and "fcntl" not in sys.modules:
    mock_fcntl = types.ModuleType("fcntl")
    mock_fcntl.LOCK_EX = 2
    mock_fcntl.LOCK_SH = 1
    mock_fcntl.LOCK_NB = 4
    mock_fcntl.LOCK_UN = 8
    mock_fcntl.flock = lambda fd, op: None
    sys.modules["fcntl"] = mock_fcntl
