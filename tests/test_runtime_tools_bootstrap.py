"""Regression coverage for tools disappearing during dependency invalidation."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "src/runner/bootstrap_runtime_tools.sh"
INSTALLER = ROOT / "src/runner/smart_install.sh"


def executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


@pytest.fixture
def tools_environment(tmp_path):
    base = tmp_path / "user-site"
    tools = base / "share/cluster-ci-tools"
    fake_bin = tmp_path / "fake-bin"
    calls = tmp_path / "uv-calls.jsonl"
    driver = tmp_path / "fake_uv.py"
    driver.write_text("""
import json, os
from pathlib import Path
import sys
args = sys.argv[1:]
if args == ['--version']:
    print('uv test')
    sys.exit(0)
with open(os.environ['TEST_UV_CALLS'], 'a') as f:
    f.write(json.dumps(args) + '\\n')
assert args[:3] == ['tool', 'install', '--force'], args
name = 'dvc-viewer' if 'dvc-viewer' in args[3] else 'dvc'
if os.environ.get('TEST_FAIL_TOOL') == name:
    sys.exit(42)
target = Path(os.environ['UV_TOOL_DIR']) / name / 'bin' / name
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text('#!/bin/sh\\necho ' + name + ' test\\n')
target.chmod(0o755)
link = Path(os.environ['UV_TOOL_BIN_DIR']) / name
link.unlink(missing_ok=True)
link.symlink_to(target)
""")
    uv = executable(tools / "bin/uv",
                    f"exec {shlex.quote(sys.executable)} {shlex.quote(str(driver))} \"$@\"\n")
    # Any unexpected download or package installation must fail this test.
    executable(fake_bin / "curl", "exit 91\n")
    executable(fake_bin / "pip",
               'if [ "$1" = freeze ]; then echo packaging==26.3; else exit 92; fi\n')
    env = {
        **os.environ,
        "CLUSTER_CI_TOOLS_ROOT": str(tools),
        "PYTHONUSERBASE": str(base),
        "CLUSTER_CI_HASH_FILE": str(tmp_path / "dependency-hash"),
        "CLUSTER_CI_LOCK_FILE": str(tmp_path / "dependency-lock"),
        "STAGE_PYTHON": sys.executable,
        "TEST_UV_CALLS": str(calls),
        "PATH": f"{fake_bin}{os.pathsep}{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
    }
    return tmp_path, base, tools, calls, env


def run(script, cwd, env, check=True):
    return subprocess.run(["bash", str(script)], cwd=cwd, env=env, check=check,
                          capture_output=True, text=True, timeout=60)


def test_tools_survive_real_smart_install_cleanup(tools_environment):
    work, base, tools, calls, env = tools_environment
    run(BOOTSTRAP, work, env)
    # Include a stale project console script and dependency: these MUST disappear.
    stale_command = executable(base / "bin/old-research-tool", "exit 0\n")
    stale_package = base / "lib/python3.12/site-packages/old_package.py"
    stale_package.parent.mkdir(parents=True, exist_ok=True)
    stale_package.write_text("OLD = True\n")
    run(INSTALLER, work, env)
    assert not stale_command.exists()
    assert not stale_package.exists()
    for name in ("uv", "dvc", "dvc-viewer"):
        result = subprocess.run([str(tools / "bin" / name), "--version"],
                                env=env, capture_output=True, text=True, check=True)
        assert name in result.stdout
    assert (base / "bin/dvc").resolve() == (tools / "bin/dvc").resolve()
    # The installer cache-hit path must also leave the tools usable.
    cached = run(INSTALLER, work, env)
    assert "Dependencies unchanged" in cached.stdout
    assert len(calls.read_text().splitlines()) == 2


def test_cached_tools_with_deleted_entrypoints_are_repaired(tools_environment):
    work, base, tools, calls, env = tools_environment
    run(BOOTSTRAP, work, env)
    for name in ("dvc", "dvc-viewer"):
        (tools / "bin" / name).unlink()
    run(BOOTSTRAP, work, env)
    assert all((tools / "bin" / name).is_file() for name in ("dvc", "dvc-viewer"))
    assert all(json.loads(row)[:3] == ["tool", "install", "--force"]
               for row in calls.read_text().splitlines())


def test_standalone_install_uses_durable_prefix(tools_environment):
    work, base, tools, calls, env = tools_environment
    seed = work / "uv-seed"
    shutil.move(tools / "bin/uv", seed)
    executable(work / "fake-bin/curl", """cat <<'INSTALL'
test "$UV_NO_MODIFY_PATH" = 1 || exit 93
cp "$TEST_UV_SEED" "$UV_INSTALL_DIR/uv"
INSTALL
""")
    run(BOOTSTRAP, work, {**env, "TEST_UV_SEED": str(seed)})
    assert (tools / "bin/uv").is_file()
    assert not (base / "bin/uv").exists()


def test_pip_fallback_keeps_binary_outside_disposable_user_site(tools_environment):
    work, base, tools, calls, env = tools_environment
    seed = work / "uv-seed"
    shutil.move(tools / "bin/uv", seed)
    executable(work / "fake-bin/python3", """
test "$1" = -m && test "$2" = pip || exit 94
while [ "$1" != --target ]; do shift; done
target="$2"
mkdir -p "$target/bin"
cp "$TEST_UV_SEED" "$target/bin/uv"
cp "$TEST_UV_SEED" "$target/bin/uvx"
""")
    run(BOOTSTRAP, work, {**env, "TEST_UV_SEED": str(seed)})
    assert (tools / "bin/uv").resolve() == tools / "uv-python/bin/uv"
    assert (tools / "bin/uvx").resolve() == tools / "uv-python/bin/uvx"
    assert not (base / "bin/uv").exists()


@pytest.mark.parametrize("failed_tool", ["dvc", "dvc-viewer"])
def test_bootstrap_propagates_tool_install_failure(tools_environment, failed_tool):
    work, base, tools, calls, env = tools_environment
    result = run(BOOTSTRAP, work, {**env, "TEST_FAIL_TOOL": failed_tool}, check=False)
    assert result.returncode == 42
