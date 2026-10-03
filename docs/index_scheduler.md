# Index de Documentation - Scheduler

This index gathers all technical notes, architecture guides, and documentation regarding the scheduler and dispatch system of **Cluster-CI v3**.

| Note Title | Short Description | Last modified | Tag |
|------------------|-------------------|----------------|-----|
| [CI Pipeline & Queue Scheduling](user/ci_queue.md) | Multi-machine scheduling, home worker priority, fair-share equity at node boundaries, and physical admission rules. | 2026-10-03 | `v3` |
| [Per-Stage Resources (`meta.cluster`)](user/stage_resources.md) | Granular resource specifications (CPU, RAM, VRAM, storage, image, workers) in `dvc.yaml`. | 2026-10-03 | `v3` |
| [Parallel DAG Execution](user/parallel_execution.md) | Distributed scheduling of DAG branches, branch executor, dedicated per-image volumes, and `dvc.lock` merge driver. | 2026-10-03 | `v3` |
| [Concurrency Management](concurrency_management.md) | Dual-mode concurrency model (draft vs non-draft), cancellation signal propagation, and multi-worker isolation. | 2026-10-03 | `v3` |
| [Resilience and Chaos Testing](scheduler/resilience_and_chaos_testing.md) | Historical stall analysis (SQLite deadlocks, Broken Pipes) and stress-test framework execution guide. | 2026-05-24 | `Up to date` |
| [Physical Resource Reconciliation and Ollama VRAM Purge](scheduler/physical_resource_reconciliation.md) | Details on reactive cancellation propagation and active Ollama VRAM purge on host to free GPU in <5s. | 2026-05-25 | `Up to date` |
| [Cluster Deployment and Update Protocol](scheduler/deployment_and_reconciliation_protocol.md) | Operational protocol for secure manual deployment and risk assessment when hot-updating cluster via update_cluster.sh. | 2026-06-01 | `Up to date` |
