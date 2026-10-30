"""Classic jobs and DVC stages share priority and safe preemption semantics."""
import json
from unittest.mock import Mock
import pytest
from test_v3_scheduler import isolated_db, client
import persistence
import scheduler_loop as sched


def worker():
    with persistence.get_db_conn() as c:
        c.execute("""INSERT INTO workers(worker_id,hostname,service_url,status,last_seen,cpus,
            total_ram_gb,total_vram_gb,gpu_count,unified_memory,vram_per_gpu)
            VALUES ('w','HEC45803','http://worker:6000','online',CURRENT_TIMESTAMP,20,120,120,1,1,'[120]')""")
        c.commit()
        return dict(c.execute("SELECT * FROM workers WHERE worker_id='w'").fetchone())


def job(jid, mode, priority, status='pending', user=None, count=0, local=0, created='2026-01-01 00:00:00'):
    running=status=='running'
    with persistence.get_db_conn() as c:
        c.execute("""INSERT INTO jobs(job_id,repo,branch,username,parallel_mode,status,scheduling_priority,
            ram_required_gb,vram_required_gb,worker_id,home_worker,active_workers,gpu_ids,runner_id,
            preempt_count,allowed_workers,is_local,local_archive_path,created_at,started_at)
            VALUES (?,?, 'main',?,?,?,?,10,100,?,?,?,?,?,?, '["HEC45803"]',?,'/private/source.tar',?,?)""",
            (jid,'org/'+jid,user or jid,int(mode=='dvc'),'running' if running else status,priority,
             'w' if running else None,'w' if running and mode=='dvc' else None,
             '["w"]' if running and mode=='dvc' else '[]','[0]' if running else '[]',
             'runner-'+jid if running and mode=='classic' else None,count,local,created,created if running else None))
        if mode=='dvc':
            c.execute("""INSERT INTO job_nodes(job_id,node_name,status,scheduling_priority,resources,
                worker_id,runner_id,gpu_ids,preempt_count,started_at)
                VALUES (?,'train',?,?,?, ?,? ,?,?,?)""",
                (jid,'running' if running else 'ready',priority,
                 json.dumps(dict(cpus=4,ram_gb=10,vram_gb=100,gpus=1,workers=['HEC45803'])),
                 'w' if running else None,'runner-'+jid if running else None,
                 '[0]' if running else '[]',count,created if running else None))
        c.commit()


def preempt(w, monkeypatch):
    post=Mock(return_value=Mock(status_code=202))
    monkeypatch.setattr(sched.requests,'post',post)
    with persistence.get_db_conn() as c:
        result=sched.check_and_preempt_for_high_priority_nodes(c,[w],{'w':sched.get_worker_allocated_resources(c,'w')})
    return result,post


@pytest.mark.parametrize('requester',['classic','dvc'])
@pytest.mark.parametrize('victim',['classic','dvc'])
@pytest.mark.parametrize('local',[0,1])
def test_all_four_modes_preempt_and_requeue_only_after_shutdown(client,monkeypatch,requester,victim,local):
    w=worker();job('low',victim,'low','running',local=local);job('high',requester,'high')
    result,post=preempt(w,monkeypatch);assert result
    post.assert_called_once();assert post.call_args.kwargs['allow_redirects'] is False
    with persistence.get_db_conn() as c:
        assert sched.get_worker_allocated_resources(c,'w')['used_vram_gb']==100
        if victim=='classic':assert c.execute("SELECT preempting FROM jobs WHERE job_id='low'").fetchone()[0]==1
        else:assert c.execute("SELECT status FROM job_nodes WHERE job_id='low'").fetchone()[0]=='preempting'
    # No second victim or early release while the asynchronous stop is pending.
    assert preempt(w,monkeypatch)[0] is False
    data=dict(job_id='low',status='failed',exit_code=-15,failure_reason='JobPreempted',worker_id='w',runner_id='runner-low')
    wrong=client.post('/update_job_status',json=dict(data,runner_id='wrong'))
    assert wrong.status_code==409
    response=client.post('/update_job_status',json=data)
    assert response.status_code==200,response.get_json()
    assert client.post('/update_job_status',json=data).status_code==200
    with persistence.get_db_conn() as c:
        row=c.execute("SELECT * FROM jobs WHERE job_id='low'").fetchone()
        assert row['is_local']==local and row['local_archive_path']=='/private/source.tar'
        if victim=='classic':
            assert row['status']=='pending' and row['preempt_count']==1 and row['worker_id'] is None
        else:
            n=c.execute("SELECT * FROM job_nodes WHERE job_id='low'").fetchone()
            assert n['status']=='ready' and n['preempt_count']==1 and n['retry_count']==0
        assert sched.get_worker_allocated_resources(c,'w')['used_vram_gb']==0


@pytest.mark.parametrize('mode',['classic','dvc'])
@pytest.mark.parametrize('case',['normal','high','same_user','immune','wrong_worker','impossible'])
def test_preemption_protections(client,monkeypatch,mode,case):
    w=worker();job('low',mode,case if case in ('normal','high') else 'low','running',
                   user='same' if case=='same_user' else None,count=5 if case=='immune' else 0)
    job('high','classic','high',user='same' if case=='same_user' else None)
    with persistence.get_db_conn() as c:
        if case=='wrong_worker':c.execute("UPDATE jobs SET allowed_workers='[\"different\"]' WHERE job_id='high'")
        if case=='impossible':c.execute("UPDATE jobs SET ram_required_gb=200 WHERE job_id='high'")
        c.commit()
    result,post=preempt(w,monkeypatch);assert not result;post.assert_not_called()


@pytest.mark.parametrize('victim',['classic','dvc'])
def test_other_reservations_are_not_assumed_free(client,monkeypatch,victim):
    w=worker();job('low',victim,'low','running');job('normal','classic','normal','running');job('high','classic','high')
    result,post=preempt(w,monkeypatch);assert not result;post.assert_not_called()


@pytest.mark.parametrize('first_mode,second_mode',[('classic','dvc'),('dvc','classic')])
@pytest.mark.parametrize('first_priority,second_priority',[('high','normal'),('normal','low'),('high','low'),('normal','normal')])
def test_unified_queue_order_on_free_worker(client,monkeypatch,first_mode,second_mode,first_priority,second_priority):
    worker();job('first',first_mode,first_priority,created='2026-01-01 00:00:00' if first_priority==second_priority else '2026-01-03 00:00:00')
    job('second',second_mode,second_priority,created='2026-01-02 00:00:00')
    queues=client.get('/api/workers/w/queue').get_json()
    assert [q['job_id'] for q in queues]==['first','second']
    monkeypatch.setattr(sched.requests,'post',Mock(return_value=Mock(status_code=202)))
    sched.schedule_iteration()
    with persistence.get_db_conn() as c:
        rows={r['job_id']:dict(r) for r in c.execute('SELECT * FROM jobs')}
        assert rows['first']['status']=='assigned'
        assert rows['second']['status']=='pending'
    # Assigned DAG jobs remain in the dashboard while their node is still ready;
    # resource reservation, rather than queue disappearance, proves placement.


def test_classic_natural_success_wins_and_legacy_runner_identity(client,monkeypatch):
    w=worker();job('low','classic','low','running');job('high','classic','high')
    with persistence.get_db_conn() as c:c.execute("UPDATE jobs SET runner_id=NULL WHERE job_id='low'");c.commit()
    assert preempt(w,monkeypatch)[0]
    response=client.post('/update_job_status',json=dict(job_id='low',status='completed',exit_code=0,
                                                      runner_id='classic-low',worker_id='w'))
    assert response.status_code==200
    with persistence.get_db_conn() as c:
        row=c.execute("SELECT * FROM jobs WHERE job_id='low'").fetchone()
        assert row['status']=='completed' and row['preempt_count']==0


def test_classic_new_assignment_has_fresh_identity_and_stale_ack_is_ignored(client,monkeypatch):
    w=worker();job('low','classic','low','running');job('high','classic','high')
    assert preempt(w,monkeypatch)[0]
    ack=dict(job_id='low',status='failed',exit_code=-15,failure_reason='JobPreempted',runner_id='runner-low',worker_id='w')
    assert client.post('/update_job_status',json=ack).status_code==200
    late_success=dict(ack,status='completed',exit_code=0)
    late_success.pop('failure_reason')
    assert client.post('/update_job_status',json=late_success).status_code==200
    with persistence.get_db_conn() as c:
        assert c.execute("SELECT status FROM jobs WHERE job_id='low'").fetchone()[0]=='pending'
    with persistence.get_db_conn() as c:c.execute("UPDATE jobs SET status='completed' WHERE job_id='high'");c.commit()
    sched.schedule_iteration()
    with persistence.get_db_conn() as c:before=dict(c.execute("SELECT * FROM jobs WHERE job_id='low'").fetchone())
    assert before['runner_id']!='runner-low' and before['status']=='assigned'
    assert client.post('/update_job_status',json=late_success).status_code==409
    assert client.post('/update_job_status',json=ack).status_code==200
    with persistence.get_db_conn() as c:after=dict(c.execute("SELECT * FROM jobs WHERE job_id='low'").fetchone())
    assert before==after


def test_stage_override_competes_with_classic_job_priority(client,monkeypatch):
    worker();job('stage','dvc','low');job('classic','classic','normal')
    with persistence.get_db_conn() as c:
        c.execute("UPDATE job_nodes SET scheduling_priority='high' WHERE job_id='stage'");c.commit()
    monkeypatch.setattr(sched.requests,'post',Mock(return_value=Mock(status_code=202)))
    sched.schedule_iteration()
    with persistence.get_db_conn() as c:
        assert c.execute("SELECT status FROM jobs WHERE job_id='stage'").fetchone()[0]=='assigned'
        assert c.execute("SELECT status FROM jobs WHERE job_id='classic'").fetchone()[0]=='pending'


@pytest.mark.parametrize('mode',['classic','dvc'])
def test_no_preemption_if_waiter_fits_without_it(client,monkeypatch,mode):
    w=worker();job('low',mode,'low','running');job('high','classic','high')
    with persistence.get_db_conn() as c:
        c.execute("UPDATE jobs SET ram_required_gb=1,vram_required_gb=0 WHERE job_id='high'");c.commit()
    result,post=preempt(w,monkeypatch);assert not result;post.assert_not_called()


@pytest.mark.parametrize('mode',['classic','dvc'])
def test_unreachable_worker_keeps_capacity_and_retries_stop_only(client,monkeypatch,mode):
    w=worker();job('low',mode,'low','running');job('high','classic','high')
    monkeypatch.setattr(sched.requests,'post',Mock(side_effect=ConnectionError('offline')))
    with persistence.get_db_conn() as c:
        assert sched.check_and_preempt_for_high_priority_nodes(c,[w],{'w':sched.get_worker_allocated_resources(c,'w')})
        assert sched.get_worker_allocated_resources(c,'w')['used_vram_gb']==100
        assert not sched.check_and_preempt_for_high_priority_nodes(c,[w],{'w':sched.get_worker_allocated_resources(c,'w')})
        assert sched.get_worker_allocated_resources(c,'w')['used_vram_gb']==100


def test_classic_cannot_steal_gpu_while_high_stage_waits_for_runner(client,monkeypatch):
    worker();job('stage','dvc','high');job('classic','classic','normal')
    with persistence.get_db_conn() as c:
        c.execute('UPDATE jobs SET created_at=CURRENT_TIMESTAMP')
        c.execute("UPDATE jobs SET ram_required_gb=1,vram_required_gb=8 WHERE job_id='classic'")
        c.execute("UPDATE job_nodes SET resources=? WHERE job_id='stage'",
                  (json.dumps(dict(cpus=4,ram_gb=1,vram_gb=8,gpus=1,workers=['HEC45803'])),));c.commit()
    monkeypatch.setattr(sched.requests,'post',Mock(return_value=Mock(status_code=202)))
    sched.schedule_iteration();sched.schedule_iteration()
    with persistence.get_db_conn() as c:
        assert c.execute("SELECT status FROM jobs WHERE job_id='classic'").fetchone()[0]=='pending'
    result=sched.handle_next_node(dict(job_id='stage',worker_id='w',runner_id='high-runner'))
    assert result['action'] in ('run','switch_image')


def test_high_extra_worker_does_not_yield_to_low_classic_waiter(client,monkeypatch):
    worker();job('stage','dvc','high');job('classic','classic','low')
    with persistence.get_db_conn() as c:
        c.execute("UPDATE jobs SET status='running',home_worker='other',active_workers='[\"other\",\"w\"]' WHERE job_id='stage'");c.commit()
    result=sched.handle_next_node(dict(job_id='stage',worker_id='w',runner_id='high-runner'))
    assert result['action'] in ('run','switch_image')


def test_classic_preemption_waits_for_cleanup_not_an_ordinary_failure(client,monkeypatch):
    w=worker();job('low','classic','low','running');job('high','classic','high')
    assert preempt(w,monkeypatch)[0]
    response=client.post('/update_job_status',json=dict(job_id='low',status='failed',exit_code=137,
                                                      runner_id='runner-low',worker_id='w'))
    assert response.status_code==200
    with persistence.get_db_conn() as c:
        assert c.execute("SELECT preempting FROM jobs WHERE job_id='low'").fetchone()[0]==1
        assert sched.get_worker_allocated_resources(c,'w')['used_vram_gb']==100


def test_no_preemption_when_unrelated_branch_lock_would_still_block_request(client,monkeypatch):
    w=worker();job('low','dvc','low','running');job('high','classic','high')
    with persistence.get_db_conn() as c:
        c.execute("UPDATE jobs SET repo='org/low' WHERE job_id='high'");c.commit()
    result,post=preempt(w,monkeypatch);assert not result;post.assert_not_called()
