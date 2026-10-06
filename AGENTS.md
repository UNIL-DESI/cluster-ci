# Agent Instructions

## Local mode and confidentiality

Ensure the local mode is always implemented and respected in new features. The only exceptions are 1) the console logs and other general metrics that are made available on the local network for the dashboard and 2) the files that are available on the local network but protected by the cluster token. In no case should a job submitted with the --local flag or its associated data be uploaded to a third-party server, including GitHub, or served publicly. The threat model to use for this is the following: everyone on the cluster (GitHub org access to the cluster token) is fully trusted (e.g., containers are considered a convenience and not a security feature, and container isolation features can be ignored), the local network is trusted for console outputs and job metadata, and public web access or third-party servers are completely off limits.

### Token-Protected Endpoints

All endpoints serving file contents, workspace archives, execution control, or private caches must enforce cluster token verification (`Authorization: Bearer <CLUSTER_TOKEN>` or unlocked dashboard browser session via `/local-access`):

- **Headnode Service (`src/scheduler/headnode_service.py`)**:
  - `GET /api/runs/<job_id>/files` (when `job['is_local'] == 1` via `require_local_files()`)
  - `GET /artifacts/<repo_owner>/<repo_name>/<rev>/<path:file_path>` (when `is_local_revision()` via `require_local_files()`)
  - `GET /view/<owner>/<repo>/<path>` and `/local-view/<owner>/<repo>/<path>` (when local revision via `require_local_files()`)
  - `GET /api/projects/<repo>/run/<commit>/hydra-params` (when `is_local_revision()` via `require_local_files()`)
  - `GET /api/jobs/<job_id>/download_code` (local source archive transfer)
  - `POST /api/jobs/<job_id>/sync_results` and `GET/DELETE /api/jobs/<job_id>/results`
  - `POST /api/local_transfers`, `PUT /api/local_transfers/<transfer_id>/chunks/<chunk_index>`, `POST /complete`, `DELETE /<transfer_id>`
  - `POST /api/jobs/<job_id>/next_node` and `POST /api/jobs/<job_id>/runner_heartbeat` (runner control)
  - State modification endpoints (`submit_job`, `register_worker`, `worker_poll`, `update_job_status`, etc.)

- **Worker Agent (`src/scheduler/worker_agent.py`)**:
  - `GET /fetch_cas/<md5>?local=1` (validates `valid_cluster_token()`; non-local queries never access `_local*` private caches)
  - `GET /api/worker/dvc/get?local=1` and `GET /api/worker/dvc/list?local=1` (requires token)
  - `POST /api/worker/dvc-viewer/start?local=1` and `ALL /api/worker/local/view/<owner>/<repo>/<path>` (requires token)
  - `GET /fetch_artifact/<file_path>` (requires token when targeting `_local*` folders)
  - Network binding: Worker listens on `0.0.0.0:AGENT_PORT` (default `6000`, `src/scheduler/worker_agent.py:2186`) to enable peer-to-peer CAS and headnode communication across cluster machines.

