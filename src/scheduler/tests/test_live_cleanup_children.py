from unittest.mock import MagicMock, patch
import psutil
from src.scheduler import worker_agent as w


def process(pid, parents=(), env=None):
    p = MagicMock();p.pid=pid;p.parents.return_value=[process(i) for i in parents];p.environ.return_value=env or {}
    return p


def test_child_created_after_snapshot_is_protected():
    # The old snapshot has only root 101; child 303 appeared afterwards.
    with patch.object(w,'active_executors',{'r':{'process':process(101),'job_id':'live'}}):
        assert w.process_belongs_to_live_executor(process(303,[202,101]))


def test_unpublished_startup_child_is_protected_by_job_identity():
    with patch.object(w,'active_executors',{'r':{'process':None,'job_id':'live'}}):
        assert w.process_belongs_to_live_executor(process(303,env={'JOB_ID':'live'}))
        assert w.process_belongs_to_live_executor(process(304,env={'CLUSTER_CI_JOB_ID':'live'}))


def test_current_startup_is_protected_before_registration():
    with patch.object(w,'active_executors',{}):
        assert w.process_belongs_to_live_executor(process(303,env={'JOB_ID':'starting'}),'starting')


def test_true_orphans_remain_eligible():
    with patch.object(w,'active_executors',{'r':{'process':process(101),'job_id':'live'}}):
        assert not w.process_belongs_to_live_executor(process(303,[202],{'JOB_ID':'finished'}))
        assert not w.process_belongs_to_live_executor(process(304,[202]))


def test_unreadable_ownership_fails_safe():
    for error in [psutil.AccessDenied(303),psutil.NoSuchProcess(303),psutil.ZombieProcess(303)]:
        p=process(303);p.parents.side_effect=error
        assert w.process_belongs_to_live_executor(p)


def test_purge_rechecks_late_child_and_still_kills_orphan():
    root=process(101);root.children.return_value=[]
    late=process(303,[101]);late.info={'pid':303,'name':'python3','cmdline':['python3','gc_orchestrator.py','run-gc']}
    orphan=process(404);orphan.info={'pid':404,'name':'python3','cmdline':['python3','gc_orchestrator.py','run-gc']}
    with patch.object(w,'active_executors',{'r':{'process':root,'job_id':'live'}}),patch.object(w,'purge_ollama_vram_on_host'),patch.object(w,'prune_all_git_worktrees'),patch.object(w.subprocess,'run',return_value=MagicMock(returncode=0,stdout='')),patch.object(w.psutil,'Process',return_value=root),patch.object(w.psutil,'process_iter',return_value=[late,orphan]):
        w.purge_orphan_runners_and_containers('live')
    late.kill.assert_not_called();orphan.kill.assert_called_once()
