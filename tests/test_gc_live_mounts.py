"""Old live workspaces must survive GC even when Docker's display truncates mounts."""
import os
import time
from types import SimpleNamespace
from src.runner import gc_orchestrator as gc


def test_live_mount_survives_real_ephemeral_sweep(tmp_path, monkeypatch):
    live = tmp_path / 'cluster-ci-wt-long-job-identity-long-runner-identity'
    dead = tmp_path / 'cluster-ci-wt-terminal-job'
    for path in (live, dead):
        path.mkdir()
        (path / 'result.txt').write_text('Synthetic result')
        os.utime(path, (time.time() - 10800,) * 2)

    original_run = gc.subprocess.run

    def docker(args, **kwargs):
        if args[0] != 'docker':return original_run(args, **kwargs)
        assert args[:2] == ['docker', 'ps']
        mount = str(live) if '--no-trunc' in args else str(live)[:14] + '…'
        return SimpleNamespace(returncode=0, stdout='id\tcluster-job-live\t' + mount, stderr='')

    monkeypatch.setattr(gc.subprocess, 'run', docker)
    monkeypatch.setattr(gc.tempfile, 'gettempdir', lambda: str(tmp_path))
    gc._sweep_ephemeral_system_litter(tmp_path / 'repositories', gc.get_active_docker_info(), time.time())
    assert (live / 'result.txt').read_text() == 'Synthetic result'
    assert not dead.exists(), 'Real orphan cleanup must remain enabled'


def test_docker_failure_prevents_unsafe_sweep(monkeypatch):
    import pytest
    monkeypatch.setattr(gc.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stderr='unavailable'))
    with pytest.raises(RuntimeError):gc.get_active_docker_info()
