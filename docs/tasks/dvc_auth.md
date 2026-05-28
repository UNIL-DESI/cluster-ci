# Silent DVC Authentication

## 1. Context & Discussion (Narrative)
> *Handover*: For continuous integration via the self-hosted runner to be seamless, the pipeline risks blocking during DVC authentication with the cloud (typically Google Drive). The usual local flow relies on OAuth, which requires opening a browser, causing a CI timeout.

Therefore, a robust "silent" authentication mechanism must be added to the orchestrator. After architectural discussion, the GitHub Secrets option was abandoned in favor of **Option 1: Global Cluster**. Credentials (such as `GCP_CREDENTIALS` or API tokens) will be stored on the runner host machine (e.g., in a `.env.secrets` or `.env` file) and dynamically sourced by the `run_research_pipeline.sh` script before `uv run dvc repro` execution. This avoids duplicate keys for all research projects in the lab.

## 2. Affected Files
- `src/runner/run_research_pipeline.sh` (modified to include sourcing the cluster-ci `.env`)
- `docs/tasks/dvc_auth.md`

## 3. Objectives (Definition of Done)
- The `run_research_pipeline.sh` orchestrator must load environment variables present in a global `.env` or `.env.secrets` file (located at the `cluster-ci` root).
- Variables must be correctly exported so that `uv run dvc repro` has transparent access to them.
- Validate that the browser-opening deadlock never triggers when accessing cloud remotes if credentials are present.

## 4. Implementation Completed (2026-04-13)
The `src/runner/run_research_pipeline.sh` orchestrator was modified to dynamically source `.env` and `.env.secrets` using:
```bash
set -a
source "$BASE_DIR/.env"
set +a
```
This ensures that all variables (such as `GCP_CREDENTIALS` or `GCP_TOKEN`) are exported to child processes, specifically `dvc repro`.
