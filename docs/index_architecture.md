# Architecture Documentation

| Note Title | Short Description | Last Mod | Tag |
|------------|-------------------|----------|-----|
| [Headnode/Worker Topology](#headnode-worker-topology-and-dynamic-scheduling) | Description of the new distributed computing architecture | 2026-05-05 | Architecture |
| [JIT GC](tasks/jit_gc.md) | LRU-based Garbage Collector for managing disk space. | 2026-05-05 | Infrastructure |
| [Concurrency Management](tasks/concurrency_management.md) | Management of obsolete job cancellations. | 2026-05-05 | Infrastructure |
| [Docker Multi-Arch Strategy](architecture/docker_arm_strategy.md) | Stratégie multi-architecture Docker (ARM64/AMD64) avec sélection per-arch via `DOCKER_IMAGE_AMD64`/`DOCKER_IMAGE_ARM64` et fallback `DOCKER_BASE_IMAGE`. | 2026-06-03 | Up to date |
| [Zombie Prevention](architecture/zombie_prevention.md) | Stratégie d'éradication et de prévention des processus orphelins dvc-viewer et conteneurs zombies (Issue #94). | 2026-05-24 | Up to date |
| [Resilient Logging](architecture/resilient_logging.md) | Stratégie de reconconnexion résiliente des logs et protocole de heartbeat pour ppng.io (Issue #111). | 2026-06-04 | Up to date |


## Headnode/Worker Topology and Dynamic Scheduling

### Context
To optimize hardware resource utilization (specifically Lenovo ThinkStation machines), the system is evolving towards an asymmetric topology.

### Roles
- **Headnode (desi):** Acts as the single entry point for CI. It hosts the Scheduler and manages the job queue.
- **Workers (Lenovo):** Machines dedicated to execution. They report their state (available RAM) to the Headnode and execute assigned jobs.

### Scheduling (Bin-Packing)
Job allocation relies on a **Bin-Packing** algorithm based on unified memory (RAM).
1. The job declares its RAM requirement (via `.cluster-ci`).
2. The scheduler maintains a real-time view of available RAM on each Worker.
3. The job is assigned to the first Worker with enough available RAM, or queued if no resources are free.

### Isolation and Protection (Software Memory Watchdog)
To ensure Worker stability, each job is monitored by a **Software Watchdog** based on the `psutil` library.

- **Recursive Measurement:** At regular intervals (every 2 seconds), the Worker calculates the sum of physical memory (RSS) of the computing process and all its child processes (e.g., DVC sub-processes).
- **UX Interruption:** If the sum exceeds the RAM limit declared in `.cluster-ci`, the Worker immediately kills the process tree.
- **GitHub Feedback:** An explicit error message is displayed on standard error (stderr), allowing the researcher to know exactly why their job failed and how much RAM was consumed relative to their reservation.
