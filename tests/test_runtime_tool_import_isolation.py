"""Research imports must not discover nested orchestration-tool environments."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest
from src.runner.branch_executor import BranchExecutor
from src.runner.runtime_env import research_site_packages

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('project_provides_dependency', [False, True])
def test_only_research_sites_enter_stage_imports(tmp_path, project_provides_dependency):
    base = tmp_path / 'user site'
    research = [base / prefix / version / kind
                for prefix, version in [('lib', 'python3.12'), ('local/lib', 'python3.11')]
                for kind in ('site-packages', 'dist-packages')]
    tools = [base / 'share/uv/tools/dvc/lib/python3.12/site-packages',
             base / 'share/cluster-ci-tools/venvs/dvc/lib/python3.12/site-packages',
             base / 'custom-platform-prefix/venvs/viewer/lib/python3.12/site-packages']
    image = tmp_path / 'image-site'
    module = 'synthetic_review_dependency'
    for p in research + tools + [image]:p.mkdir(parents=True)
    for p in tools:(p / (module + '.py')).write_text('ORIGIN="tool environment"\n')
    (image / (module + '.py')).write_text('ORIGIN="image environment"\n')
    if project_provides_dependency:
        (research[0] / (module + '.py')).write_text('ORIGIN="research environment"\n')

    class ShellDocker:
        def exec_in_container(self, container, command):
            command = command.replace('/cluster-ci', shlex.quote(str(ROOT))).replace('/home/user/.local', shlex.quote(str(base)))
            r = subprocess.run(['bash', '-c', command], capture_output=True, text=True, check=True)
            return r.returncode, r.stdout

    executor = BranchExecutor.__new__(BranchExecutor)
    executor.current_container = 'synthetic'
    executor.docker = ShellDocker()
    discovered = executor.discover_project_pythonpath()
    assert set(discovered.split(':')) == {str(p) for p in research}
    # Exercise the resulting import environment, rather than checking paths alone.
    code = f'import sys;sys.path.append({str(image)!r});import {module};print({module}.ORIGIN)'
    r = subprocess.run([sys.executable, '-c', code], env=dict(os.environ, PYTHONPATH=discovered),
                       capture_output=True, text=True, check=True)
    expected = 'research environment' if project_provides_dependency else 'image environment'
    assert r.stdout.strip() == expected


def test_empty_user_site_has_no_research_import_paths(tmp_path):
    assert research_site_packages(str(tmp_path / 'absent')) == []


def test_package_verifier_ignores_isolated_tool_dependencies(tmp_path, monkeypatch):
    from src.runner.verify_packages import discover_project_paths, verify_packages
    base = tmp_path / 'user-site'
    research = base / 'lib/python3.12/site-packages'
    tool = base / 'share/cluster-ci-tools/venvs/viewer/lib/python3.12/site-packages'
    for path, name in [(research, 'synthetic_research_check'), (tool, 'synthetic_platform_only')]:
        metadata = path / (name + '-1.0.dist-info')
        metadata.mkdir(parents=True)
        (metadata / 'METADATA').write_text(f'Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n')
        (metadata / 'top_level.txt').write_text(name + '\n')
        (path / (name + '.py')).write_text('__version__ = "1.0"\n')
    monkeypatch.syspath_prepend(str(research))
    monkeypatch.delenv('PYTHONPATH', raising=False)
    assert verify_packages(pythonpath='', base_dir=str(base), workspace_dir=str(tmp_path),
                           check_subprocesses=False) == 0
    assert discover_project_paths(str(base)) == [str(research)]


def test_verifier_script_entrypoint_excludes_nested_tool_environment(tmp_path):
    tool = tmp_path / 'user/share/cluster-ci-tools/venvs/viewer/lib/python3.12/site-packages'
    metadata = tool / 'synthetic_platform_only-1.0.dist-info'
    metadata.mkdir(parents=True)
    (metadata / 'METADATA').write_text('Metadata-Version: 2.1\nName: synthetic_platform_only\nVersion: 1.0\n')
    result = subprocess.run([sys.executable, str(ROOT / 'src/runner/verify_packages.py'),
                             '--base-dir', str(tmp_path / 'user'), '--workspace', str(tmp_path),
                             '--no-subprocesses', '--no-import-modules'],
                            cwd=tmp_path, env=dict(os.environ, PYTHONPATH=''),
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
