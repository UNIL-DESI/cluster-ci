# Progress tracking — 2026-06-25T13:43:30+02:00

Last visited: 2026-06-25T13:43:30+02:00

## Tasks
- [ ] Initialize investigation briefing and progress tracking (Done)
- [ ] Investigate Domain 1: DVC & Storage
  - [ ] Emergency GC (50 Go)
  - [ ] Lazy Transfer (`sync_status`)
  - [ ] DVC Historical Viewer (Isolated worktree, cache symlink, 30m timeout auto-cleanup)
- [ ] Investigate Domain 2: Docker Containers
  - [ ] `smart_install.sh` composite hash
  - [ ] NGC protection
  - [ ] Stub NVSHMEM
  - [ ] bitsandbytes CUDA compatibility patch
- [ ] Investigate Domain 3: CI & Queue Scheduler
  - [ ] Watchdog GPU double seuil (VRAM Soft, RAM Hard 90% + Grace Blackwell)
  - [ ] Zombie GC (multi-dimensional inactiveness 10m)
  - [ ] DVC Git Watchdog (intermediate commits)
- [ ] Investigate Domain 4: Client (cluster-run)
  - [ ] Intelligent CWD redirection via `db.json`
  - [ ] Post-run sync validation base-commune
  - [ ] Interactive pre-commit auto-correction (`validate_pyproject.py`)
- [ ] Investigate Domain 5: Administration & Resilience
  - [ ] RunnerManager (ephemeral runners & termination loop detection)
  - [ ] System pre-requisites (passwordless sudoers, systemd watchdog auto-reboot)
  - [ ] Maintenance mode (suspension, env variables)
- [ ] Write detailed report in `handoff.md`
- [ ] Notify parent orchestrator
