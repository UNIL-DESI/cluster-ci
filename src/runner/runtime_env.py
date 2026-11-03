"""Runtime environment and executable resolution helpers.

Ensures deterministic resolution of binaries installed in the project's virtual environment
or host system, avoiding silent fallbacks when running in headless daemon environments
(such as systemd services with restricted PATH).
"""

import os
import glob
import shutil
import sys


def research_site_packages(user_base: str) -> list:
    """Only direct research user sites; never nested tool/virtual environments."""
    paths = []
    for prefix in ('lib', os.path.join('local', 'lib')):
        for kind in ('site-packages', 'dist-packages'):
            pattern = os.path.join(user_base, prefix, 'python3.*', kind)
            paths.extend(path for path in glob.glob(pattern) if os.path.isdir(path))
    return sorted(set(paths))


def resolve_venv_executable(name: str) -> str:
    """Resolve an executable binary path, prioritizing the virtualenv running the current process.

    Resolution order:
    1. Direct file check if 'name' is already an absolute executable path.
    2. The directory containing sys.executable (the active venv's bin/ or Scripts/ directory).
    3. User local bin directory (~/.local/bin).
    4. The system PATH (via shutil.which).

    Raises:
        FileNotFoundError: If the executable is not found in the venv, ~/.local/bin, or system PATH.
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

    # User local bin directory (~/.local/bin)
    user_local_bin = os.path.expanduser("~/.local/bin")
    if os.path.isdir(user_local_bin):
        cand_user = shutil.which(name, path=user_local_bin)
        if cand_user and os.path.isfile(cand_user):
            return os.path.abspath(cand_user)
        direct_user = os.path.join(user_local_bin, name)
        if os.path.isfile(direct_user) and os.access(direct_user, os.X_OK):
            return os.path.abspath(direct_user)

    cand_path = shutil.which(name)
    if cand_path and os.path.isfile(cand_path):
        return os.path.abspath(cand_path)

    path_env = os.environ.get("PATH", "")
    raise FileNotFoundError(
        f"Executable '{name}' not found. Searched active venv directory ('{venv_dir}'), "
        f"user local bin ('{user_local_bin}'), and system PATH ('{path_env}')."
    )


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Discover research Python package paths inside a runtime container')
    parser.add_argument('--research-pythonpath', metavar='USER_BASE', required=True)
    for path in research_site_packages(parser.parse_args().research_pythonpath):
        print(path)
