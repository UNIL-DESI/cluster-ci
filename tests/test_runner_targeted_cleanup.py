"""Run the real Bash cleanup body with recorded commands, never real signals."""
from pathlib import Path
import subprocess
import pytest

SCRIPT=Path(__file__).resolve().parents[1]/'src/runner/run_research_pipeline.sh'


@pytest.mark.parametrize('parallel',['0','1'])
def test_cleanup_only_targets_this_jobs_recorded_pids(tmp_path,parallel):
    text=SCRIPT.read_text()
    definition=text.split('function cleanup_job_resources() {',1)[1].split('\n}\n# Trap',1)[0]
    cleanup='function cleanup_job_resources() {'+definition+'\n}\n'
    trace=tmp_path/'commands'
    stubs=r'''
log_info() { :; }
docker() { :; }
python3() { :; }
rm() { :; }
kill() { printf 'kill %s\n' "$*" >> "$TRACE"; }
pkill() { printf 'UNSCOPED pkill %s\n' "$*" >> "$TRACE"; }
pgrep() { printf 'UNSCOPED pgrep %s\n' "$*" >> "$TRACE"; echo 99999; }
'''
    env=dict(TRACE=str(trace),JOB_ID='synthetic',SAFE_JOB_ID='synthetic',
             CLUSTER_CI_PARALLEL_MODE=parallel,MAIN_CONTAINER_NAME='own-main',
             VIEWER_CONTAINER_NAME='own-viewer',VIEWER_PORT='12345',
             DVC_VIEWER_PID='201',WATCHDOG_PID='202',GPU_WATCHDOG_PID='203',
             BASE_DIR=str(tmp_path),REPO_WORK_DIR='synthetic',WORKSPACE_KEY='synthetic')
    subprocess.run(['bash','-c',stubs+cleanup+'cleanup_job_resources'],env=env,check=True)
    assert trace.read_text().splitlines()==['kill -9 201','kill -9 202','kill -9 203']


def test_classic_startup_does_not_purge_other_viewers(tmp_path):
    text=SCRIPT.read_text()
    startup=text.split('# 2. Preventive Purge & Git State Management',1)[1].split('if [ "$IS_LOCAL" = "1" ]; then',1)[0]
    trace=tmp_path/'commands'
    stubs='log_info(){ :; }; docker(){ :; }; kill(){ echo BROAD >> "$TRACE"; }; pgrep(){ echo 99999; };\n'
    subprocess.run(['bash','-c',stubs+startup],env=dict(TRACE=str(trace),CLUSTER_CI_PARALLEL_MODE='0'),check=True)
    assert not trace.exists()


def test_watchdog_pid_tracks_watchdog_instead_of_tee(tmp_path):
    text=SCRIPT.read_text()
    launch=text.split('log_info "Starting GPU VRAM Watchdog (limit: ${VRAM_LIMIT}GB)..."',1)[1].split('\nfi',1)[0]
    (tmp_path/'src/runner').mkdir(parents=True)
    # exec keeps the watchdog's PID and avoids leaving descendants in this test.
    (tmp_path/'src/runner/gpu_watchdog.sh').write_text('echo watchdog-started\nexec sleep 30\n')
    command=launch+'\nprintf "%s" "$GPU_WATCHDOG_PID" > pid\n'
    command+='if [ "$(cat /proc/$GPU_WATCHDOG_PID/comm)" = tee ]; then kill "$GPU_WATCHDOG_PID"; exit 1; fi\n'
    command+='kill "$GPU_WATCHDOG_PID"; wait "$GPU_WATCHDOG_PID" 2>/dev/null || true\n'
    subprocess.run(['bash','-c',command],cwd=tmp_path,env=dict(BASE_DIR=str(tmp_path),MAIN_CONTAINER_NAME='synthetic',VRAM_LIMIT='1'),check=True,timeout=10)
