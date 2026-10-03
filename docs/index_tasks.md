# Index des Tâches du Cluster-CI

This index lists all design, development, and maintenance tasks on the project Roadmap.

---

## Support & Contribution Rules

The Cluster-CI system is designed, implemented, and maintained by **hjamet**. If you encounter bugs, performance bottlenecks, or want to suggest new features, you are encouraged to open a GitHub Issue.

### The Golden Rule of the Roadmap
To keep the project roadmap structured and aligned with development targets:
> ⚠️ **"No GitHub Issue = No line in the Roadmap"**

Before adding any task to the Roadmap section of the project's README, a corresponding GitHub Issue must be created on the repository.

### Mandatory GitHub Issue Template
Every issue must follow this structure:
```markdown
# [Task Title]

## 1. Contexte & Discussion (Narratif)
- Detailed summary of why we need this feature or how the bug occurs.
- Decision history.

## 2. Relevant Files
- List of files to modify or inspect (e.g. `src/scheduler/worker_agent.py`).

## 3. Objectifs (Definition of Done)
- High-level deliverables.
- Focus on end results, not implementation plans or pseudo-code.
```

---

## Task Index

| Note Title | Short Description | Last modified | Tag |
|------------------|-------------------|----------------|-----|
| [Client Script](tasks/client_script.md) | Client-side automatic installation script and CLI integration | 2026-05-19 | `Up to date` |
| [Concurrency Management](tasks/concurrency_management.md) | Intelligent concurrency management per research repository | 2026-05-20 | `Up to date` |
| [Deploy Local Cluster](tasks/deploy_local_cluster.md) | Initial local deployment and testing guide | 2026-05-19 | `Up to date` |
| [DVC Authentication](tasks/dvc_auth.md) | Silent DVC authentication integration with Google Drive | 2026-05-19 | `Up to date` |
| [JIT Garbage Collector](tasks/jit_gc.md) | Automated Just-In-Time cleanup of caches and orphan Docker images | 2026-05-21 | `Up to date` |
| [Setup Orchestrator](tasks/setup_orchestrator.md) | Initialisation et configuration de base du service de runner | 2026-05-19 | `Up to date` |
| [Global Execution Timeout](tasks/global_timeout.md) | Timeout guard, clean Docker termination, and researcher notification | 2026-05-22 | `Up to date` |
| [Ghost Jobs Fix](tasks/ghost_jobs_fix.md) | Definitive resolution of ghost jobs issue | 2026-05-23 | `Up to date` |
| [Fix P2P and Cancellation Logs](tasks/fix_p2p_and_cancellation_logs.md) | P2P remote IP fix and notification for canceled jobs | 2026-05-26 | `Up to date` |
| [Optimize Git and DVC Sync](tasks/optimize_git_and_dvc_sync.md) | Git intermediate commit history optimization and automated intelligent DVC sync | 2026-05-26 | `Up to date` |
| [Rescue and Robustness GHA Runner](tasks/rescue_and_robustness_runner.md) | Durcissement systemd, nettoyage profond des slots et processus orphelins suite aux annulations de jobs | 2026-05-27 | `Up to date` |
| [OOM Cgroups Detection](tasks/oom_cgroups_detection.md) | Explicit, robust detection of RAM limits via Docker cgroups | 2026-05-27 | `Up to date` |