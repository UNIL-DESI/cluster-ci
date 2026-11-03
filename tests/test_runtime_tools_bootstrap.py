"""Regression coverage for tools disappearing during dependency invalidation."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "src/runner/bootstrap_runtime_tools.sh"
INSTALLER = ROOT / "src/runner/smart_install.sh"
ENV_SCRIPT = ROOT / "src/runner/runtime_tools_env.sh"


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


def test_custom_prefix_resolves_all_tools_after_cleanup(tools_environment):
    work, base, tools, calls, env = tools_environment
    run(BOOTSTRAP, work, env)
    run(INSTALLER, work, env)
    result = subprocess.run(
        ['bash', '-c', f'source {shlex.quote(str(ENV_SCRIPT))}; command -v uv; command -v dvc; command -v dvc-viewer'],
        cwd=work, env=env, capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == [str(tools / 'bin' / n) for n in ('uv', 'dvc', 'dvc-viewer')]


def test_generated_uv_shim_uses_configured_prefix(tools_environment):
    work, base, tools, calls, env = tools_environment
    run(BOOTSTRAP, work, env)
    source = (ROOT / 'src/runner/run_research_pipeline.sh').read_text()
    body = re.search(r"# UV Shim\n[^\n]*\n(.*?)\nSHIMEOF", source, re.S)
    assert body, 'Runner uv shim was not found'
    shim = executable(work / 'shim/uv', body[1].removeprefix('#!/bin/bash\n').replace(
        '/cluster-ci/src/runner/runtime_tools_env.sh', str(ENV_SCRIPT)))
    # This is a bash shim; the shared executable helper otherwise writes /bin/sh.
    shim.write_text('#!/bin/bash\n' + shim.read_text().split('\n', 1)[1])
    result = subprocess.run([str(shim), '--version'], cwd=work, env=env,
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == 'uv test'


def test_parallel_images_use_durable_tools_and_preserve_local_mode(tools_environment, monkeypatch):
    from types import SimpleNamespace
    from src.runner import branch_executor as branch
    work, base, tools, calls, env = tools_environment
    monkeypatch.setenv('IS_LOCAL', '1')
    monkeypatch.delenv('CLUSTER_CI_SECRETS_FILE', raising=False)
    monkeypatch.setattr(branch, 'subprocess', SimpleNamespace(Popen=lambda *a, **k: None))
    # Legacy bootstrap must never accidentally invoke a real network installer.
    shutil.copy2(tools / 'bin/uv', work / 'fake-bin/uv')
    project = work / 'project'; project.mkdir()
    second_base = work / 'second-image-site'
    second_tools = second_base / 'share/cluster-ci-tools'
    (second_tools / 'bin').mkdir(parents=True)
    shutil.copy2(tools / 'bin/uv', second_tools / 'bin/uv')

    class LocalShellDocker:
        """Real unprivileged bootstrap/cleanup; synthetic Docker and tool binaries."""
        def __init__(self):self.envs = {}; self.commands = []
        def create_volume(self, name):return 0
        def run_container(self, **kwargs):
            second = kwargs['image'] == 'synthetic:second'
            user_base, prefix = (second_base, second_tools) if second else (base, tools)
            self.envs[kwargs['container_name']] = dict(env, PYTHONUSERBASE=str(user_base),
                CLUSTER_CI_TOOLS_ROOT=str(prefix), CLUSTER_CI_HASH_FILE=str(user_base / 'hash'),
                CLUSTER_CI_LOCK_FILE=str(user_base / 'lock'), IS_LOCAL=kwargs['env']['IS_LOCAL'])
            return 0
        def exec_in_container(self, container_name, command, env=None, user=None, **kwargs):
            if user == 'root':return 0, 'Synthetic Docker: host root initialization is not executed'
            effective = dict(self.envs[container_name], **(env or {}))
            command = command.replace('/cluster-ci', str(ROOT)).replace('/home/user/.local', effective['PYTHONUSERBASE'])
            self.commands.append((command, effective))
            result = subprocess.run(['bash', '-c', command], cwd=project, env=effective,
                                    capture_output=True, text=True, timeout=60)
            return result.returncode, result.stdout + result.stderr
        def stop_container(self, *a):pass
        def remove_container(self, *a):pass

    docker = LocalShellDocker()
    executor = branch.BranchExecutor.__new__(branch.BranchExecutor)
    executor.__dict__.update(target_repo='lab/review', runner_id='runner-review', safe_job_id='review',
        job_id='review', worker_id='worker-review', headnode_url='http://head.invalid', cluster_token='synthetic',
        container_prefix='synthetic-', repo_dir=str(project), main_repo_dir=str(project), base_dir=str(ROOT),
        worktree_dir=None, ram_limit=1, vram_limit=0, user_id=os.getuid(), group_id=os.getgid(),
        current_container=None, current_image=None, current_pythonpath='', total_containers_started=0, docker=docker)
    executor.compute_docker_resource_args = lambda resources: ['--memory', '1g']
    for image, prefix in [('synthetic:first', tools), ('synthetic:second', second_tools)]:
        executor.start_container_for_image(image)
        code, output = docker.exec_in_container(executor.current_container, 'bash /cluster-ci/src/runner/smart_install.sh')
        assert code == 0, output
        code, output = executor.execute_node_in_container('review', env_vars={'IS_LOCAL': '0', 'HEADNODE_URL': 'https://external.invalid'})
        assert code == 0 and 'dvc test' in output, output
        assert docker.commands[-1][1]['IS_LOCAL'] == '1'
        assert docker.commands[-1][1]['HEADNODE_URL'] == 'http://head.invalid'
        for name in ('uv', 'dvc', 'dvc-viewer'):assert (prefix / 'bin' / name).exists()
        executor.stop_current_container()
    assert executor.total_containers_started == 2
