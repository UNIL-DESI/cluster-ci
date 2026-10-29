---
title: 'Cluster-CI: A Pull-Based GitOps Orchestrator for Reproducible DVC Research Pipelines on Heterogeneous GPU Clusters'
tags: [Python, GitOps, DVC, GPU scheduling, machine learning infrastructure, reproducible research]
authors:
  - name: Henri Jamet
    orcid: 0000-0000-0000-0000
    corresponding: true
    affiliation: 1
affiliations:
  - name: Department of Information Systems (DESI), Faculty of Business and Economics (HEC Lausanne), University of Lausanne, Switzerland
    index: 1
date: 29 October 2026
bibliography: paper.bib
---

# Summary

Cluster-CI is an open-source, pull-based GitOps orchestrator built for reproducible machine learning experiments across heterogeneous on-premise GPU servers. Academic laboratories rarely run uniform hardware. Instead, they operate mixed fleets that combine x86_64 workstations with ARM64 unified-memory compute nodes, where enterprise schedulers demand dedicated control-plane administration, complex network overlays, and manual artifact synchronization. Cluster-CI replaces that operational overhead by coupling GitHub Actions self-hosted runners, Data Version Control (DVC) pipelines [@kuprieiev2024dvc], and isolated GPU containers under one declarative workflow.

Researchers trigger runs by pushing a Git commit or dispatching a workflow with target parameters. Everything starts from Git. The central coordinator (`headnode`) checks hardware requirements against live worker telemetry, schedules jobs onto matching physical nodes, and streams execution logs to the user. Worker nodes pull the exact commit, resolve the required DVC dependency subgraph, run stages inside architecture-specific containers, and commit updated lockfiles and metrics back to the repository after each stage finishes, guaranteeing end-to-end provenance without custom submission scripts.

# Statement of Need

Small and medium-sized university laboratories typically manage between two and ten physical GPU servers acquired across multiple funding cycles. Hardware heterogeneity is the norm. A representative academic fleet mixes AMD64 workstations equipped with discrete consumer or datacenter GPUs (such as NVIDIA RTX 3090 24 GB cards) alongside newer ARM64 nodes built around unified CPU-GPU memory architectures (such as the NVIDIA Grace Blackwell GB10 Superchip with 128 GB of coherent RAM). Operating these mixed fleets exposes three recurring friction points in daily research workflows.

First, traditional High-Performance Computing (HPC) workload managers such as Slurm [@yoo2003slurm] and container orchestrators such as Kubernetes [@burns2016borg] impose high administrative costs. They require kernel-level daemon maintenance, shared network filesystems (NFS or Lustre), and specialized job scripts (`sbatch` or YAML manifests) that diverge from local development environments. Distributed execution frameworks like Ray [@moritz2018ray] excel at tightly coupled in-memory actors, yet they do not natively manage multi-stage file-based data pipelines or declarative Git state synchronization. Most small labs lack a dedicated systems administrator. Maintaining these heavy control planes diverts researcher time away from scientific work.

Second, ad-hoc execution via manual SSH sessions and terminal multiplexers (`tmux` or `screen`) breaks scientific reproducibility. Manual workflows fail silently. Researchers frequently launch long training runs on uncommitted working trees, overwrite hyperparameter configurations in place, or lose track of which code revision produced a given evaluation table. When a node reboots or a process crashes mid-pipeline, intermediate results remain trapped on local disks without automated recovery.

Third, standard DVC execution (`dvc repro`) assumes a single homogeneous machine and invalidates downstream stages whenever any byte of a tracked source script changes. That strictness hurts rapid iteration. In empirical machine learning research, formatting edits, type annotations, or logging adjustments alter file MD5 digests without changing mathematical behavior, and re-running multi-hour GPU training stages after cosmetic code edits wastes compute capacity. Cluster-CI addresses these three challenges directly: it provides a zero-SSH GitOps execution model, enforces strict Git-to-DVC state binding, and introduces domain-specific mechanisms for partial DAG resolution, hash realignment, unified-memory protection, and air-gapped local execution.

# Architecture and Technical Contributions

Cluster-CI is implemented in Python around four decoupled components: a lightweight HTTP and SQLite coordinator (`headnode`), a node-level daemon (`worker`), a pipeline execution engine (`dvc_helper`), and a real-time monitoring dashboard. Firewall traversal requires zero inbound ports. Workers communicate with the coordinator over standard HTTP polling or outbound SSH tunnels, which allows nodes behind restrictive university networks to join the compute pool immediately. Beyond its pull-based control loop, Cluster-CI introduces four technical contributions tailored to reproducible machine learning research.

## Recursive DVC Sub-DAG Resolution

Large research repositories often define parameterized DVC pipelines covering multiple datasets, baseline models, and evaluation stages (`dvc.yaml`). Running the whole graph on one machine wastes time. Cluster-CI includes an iterative execution engine (`dvc_iterative_repro.py`) that queries `dvc dag --dot` to parse the directed acyclic graph before execution starts.

When a user specifies target stages (for instance, `STAGES=evaluate@dataset_a`), the engine computes the transitive closure of upstream dependencies in topological order. Unrelated branches stay untouched. Rather than delegating the entire run to a monolithic `dvc repro` call, the engine executes one stage at a time, pushes newly generated outputs to the content-addressed DVC cache immediately after each stage succeeds, and commits the updated `dvc.lock` and JSON metric files back to the active Git branch. If a multi-day job is preempted or fails at stage four of five, stages one through three remain permanently recorded in Git and cached on disk. Subsequent retries resume directly from stage four.

## Hash Realignment Without Cache Invalidation (`Skip-Code`)

Because DVC tracks source code dependencies by file MD5 hash, any comment addition, docstring update, or linter pass marks dependent pipeline stages as dirty. Trivial edits should not trigger GPU reruns. To prevent unnecessary recomputation while preserving strict data provenance, Cluster-CI provides a surgical hash realignment tool (`dvc_realign.py`) exposed via the `--skip-code` execution flag.

When triggered, `dvc_realign.py` inspects `dvc.yaml` and `dvc.lock` using `ruamel.yaml` to preserve exact file formatting and comments. It separates code artifacts (Python scripts and source modules) from data and hyperparameter inputs. Before updating any digest, the tool verifies that all stage outputs already exist in the local workspace or the remote DVC content-addressed storage (CAS). Missing outputs or modified hyperparameters abort the operation with an explicit error. When only code files have changed and valid outputs exist, the tool updates the code MD5 entries in `dvc.lock` and relinks cached outputs into the workspace so that cosmetic code maintenance stays separate from algorithmic modifications.

## Multi-Architecture Bin-Packing, Preemption, and Unified Memory Guard

The Cluster-CI scheduler maps incoming jobs to heterogeneous workers using a First-Fit bin-packing algorithm parameterized by architecture (`AMD64` or `ARM64`), GPU count, VRAM capacity, and system RAM. Host stability comes first. To keep operating systems responsive during heavy data loading, the scheduler enforces an 8 GB host RAM safety reserve on every node (`available_ram = total_ram - 8 GB`). When multiple independent sub-DAG branches are queued, the coordinator distributes parallel branches across distinct workers and synchronizes intermediate artifacts through a peer-to-peer HTTP CAS cache with local-disk hardlinking.

Unified memory architectures such as the ARM64 NVIDIA Grace Blackwell GB10 require specialized memory governance. Standard CUDA OOM handling does not protect unified RAM. Unlike discrete PCIe GPUs where an out-of-memory (OOM) allocation raises a recoverable `cudaErrorMemoryAllocation` exception inside the CUDA driver, exhausting unified memory starves the Linux kernel page cache and can freeze the physical host before `systemd-oomd` intervenes. Cluster-CI implements a hardware watchdog inside `worker_service.py` that samples `/proc/meminfo` (`MemAvailable`) alongside `nvidia-smi` telemetry, terminates offending container process groups as soon as available memory drops below the safety threshold, and logs an explicit `OOM_KILLED` diagnostic. The scheduler also supports priority-based preemption (`LOW`, `NORMAL`, `HIGH`, `CRITICAL`): high-priority interactive jobs can evict lower-priority batch runs, which automatically requeue and resume from their last committed `dvc.lock` checkpoint.

## Zero-Leak Local Mode (`--local`)

Academic collaborations frequently involve sensitive datasets governed by institutional review board (IRB) agreements or industrial non-disclosure contracts that prohibit uploading data or intermediate model weights to external clouds or GitHub servers. Data privacy must be enforced at the transport layer. Cluster-CI supports a confined execution mode activated via the `--local` CLI flag (`cluster_run.py`).

In local mode, the CLI packages uncommitted workspace changes and local data dependencies into an encrypted archive uploaded directly to the on-premise coordinator over the campus network. External clouds are bypassed. The worker fetches the bundle from an internal endpoint (`/fetch_cas?local=1`) authenticated by an HMAC cluster token, while external Git remotes and cloud DVC remotes remain disabled inside the container. Updated metrics and artifacts return exclusively to the workstation over the local API so zero bytes of sensitive code or data leave the university network.

# Validation and Production Deployment

Cluster-CI runs in daily production at the Department of Information Systems (HEC Lausanne, University of Lausanne) across a three-node heterogeneous cluster: one ARM64 node (NVIDIA Grace Blackwell GB10, 128 GB unified memory) and two AMD64 nodes (NVIDIA RTX 3090, 24 GB VRAM, 64 GB RAM). Daily workloads stress both architectures. In production research projects, the platform orchestrates Large Language Model (LLM) recommendation pipelines (`NeMo` ARM64 containers using up to 100 GB of unified RAM) and multi-stage data mining benchmarks.

Software reliability is verified through two test layers. First, the repository includes an automated test suite of 72 `test_*.py` modules (663 unit and component tests run via `pytest`) that test DAG parsing, First-Fit resource allocation, SQLite state transitions, workspace sanitization, and `dvc.lock` updates. Second, an end-to-end (E2E) integration suite validates eight live cluster scenarios: normal pipeline completion, failure handling, user cancellation, priority preemption, orphan container recovery, `Skip-Code` hash realignment, multi-worker DAG distribution, and confined `--local` execution.

# References
