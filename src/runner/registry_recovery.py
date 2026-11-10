"""Fail-closed recovery after a killed runner misses its shell EXIT trap."""
TERMINAL={'completed','failed','cancelled','canceled'}
def project_has_active_jobs(conn,repo,local):
    return bool(conn.execute("SELECT EXISTS(SELECT 1 FROM jobs WHERE repo=? AND COALESCE(is_local,0)=? AND status IN ('pending','assigned','running'))",(repo,int(local))).fetchone()[0])
def recover_running(registry,lookup,is_mounted,now):
    changed=[]
    for name,data in registry.items():
        ids=data.get('job_ids')
        if data.get('status')!='running' or not isinstance(ids,list) or not ids or is_mounted(name):continue
        local=name.startswith('_local/');repo=name[7:] if local else name
        try:
            jobs=[lookup(jid) for jid in ids]
            if not all(j.get('job_id')==jid and j.get('repo')==repo and bool(j.get('is_local'))==local and j.get('status') in TERMINAL and j.get('project_has_active_jobs') is False for jid,j in zip(ids,jobs)):continue
        except Exception:continue
        data.update(status='idle',last_execution=now,job_ids=[])
        changed.append(name)
    return changed
