"""Runtime environment and executable resolution helpers.

Ensures deterministic resolution of binaries installed in the project's virtual environment
or host system, avoiding silent fallbacks when running in headless daemon environments
(such as systemd services with restricted PATH).
"""

import os
import shutil
import sys


def resolve_venv_executable(name: str) -> str:
    """Resolve an executable binary path, prioritizing the virtualenv running the current process.

    Resolution order:
    1. Direct file check if 'name' is already an absolute executable path.
    2. The directory containing sys.executable (the active venv's bin/ or Scripts/ directory).
    3. The system PATH (via shutil.which).

    Raises:
        FileNotFoundError: If the executable is not found in either the venv or system PATH.
                           No silent fallback is permitted.
    """
    if os.path.isabs(name) and os.path.isfile(name) and os.access(name, os.X_OK):
        return os.path.abspath(name)

    venv_dir = os.path.dirname(sys.executable) if sys.executable else ""
    if venv_dir:
        cand = shutil.which(name, path=venv_dir)
        if cand and os.path.isfile(cand):
            return os.path.abspath(cand)
        direct = os.path.join(venv_dir, name)
        if os.path.isfile(direct) and os.access(direct, os.X_OK):
            return os.path.abspath(direct)

    cand_path = shutil.which(name)
    if cand_path and os.path.isfile(cand_path):
        return os.path.abspath(cand_path)

    path_env = os.environ.get("PATH", "")
    raise FileNotFoundError(
        f"Executable '{name}' not found. Searched active venv directory ('{venv_dir}') "
        f"and system PATH ('{path_env}')."
    )
