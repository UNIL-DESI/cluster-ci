# Configuration Reference (`.cluster-ci`)

The `.cluster-ci` file at the root of your repository controls how your job is scheduled and executed on the cluster. It is created automatically by the [install script](onboarding.md), but you should customize it for each project.

---

## File Format

The file uses a simple `KEY=VALUE` format (one parameter per line). Lines starting with `#` are comments.

```ini
# Hardware requirements
REQUIRED_RAM=16GB
REQUIRED_VRAM=24GB
MAX_RUNTIME_HOURS=6

# Optional: parallel DAG execution across workers
PARALLEL_STAGES=true

# Optional: restrict to specific workers
ALLOWED_WORKERS=gb10-node1,gb10-node2

# Optional: run only specific DVC stages (classic mode)
STAGES=train
```

---

## Parameter Reference

### Essential & Hardware Parameters

These parameters control resource allocation and job timeout.

| Parameter | Required | Default | Description |
| :--- | :--- | :--- | :--- |
| `MAX_RUNTIME_HOURS` | **Yes** | — | Maximum allowed runtime in hours (1–24). The job is automatically killed if it exceeds this limit. |
| `REQUIRED_CPUS` | No | `2` | Number of CPU cores allocated for execution (integer `>= 1`). Overridden by `meta.cluster.cpus`. |
| `REQUIRED_GPUS` | No | `0` | Number of physical GPUs allocated (integer `>= 0`). Overridden by `meta.cluster.gpus`. |
| `REQUIRED_RAM` | No | `10GB` | Minimum physical RAM required on the worker. Can also be passed via `--ram <GB>`. Overridden by `meta.cluster.ram_gb`. |
| `REQUIRED_VRAM` | No | `0GB` | Minimum GPU VRAM required. When `0GB`, job runs on CPU-only nodes. Overridden by `meta.cluster.vram_gb`. |
| `REQUIRED_STORAGE` | No | `0GB` | Minimum free disk space in GB required on the worker. Also recognized as `REQUIRED_DISK`. Overridden by `meta.cluster.storage_gb`. |

!!! warning "GPU Consistency & Realistic Values"
    * If `REQUIRED_VRAM > 0` is set without specifying `REQUIRED_GPUS`, Cluster-CI automatically defaults `gpus` to `1` so the GPU allocation check succeeds.
    * If you request more RAM or VRAM than any worker can provide, your job will stay in the queue indefinitely. Check the [Dashboard](dashboard.md) to see available worker capacities.

### Execution Control

| Parameter | Default | Description |
| :--- | :--- | :--- |
| `PARALLEL_STAGES` | `false` | When set to `true`, enables **Parallel DAG execution** across multiple cluster workers. Independent branches of your `dvc.yaml` pipeline run concurrently on available nodes. See [Parallel DAG Execution](parallel_execution.md). |
| `STAGES` | *(empty — runs full pipeline)* | Comma-separated list of DVC stage names to execute (used in classic sequential mode). If empty or set to `all`, the cluster runs `dvc repro`. |
| `ALLOWED_WORKERS` | *(empty — all workers eligible)* | Comma-separated list of worker hostnames. Only these workers will be considered for scheduling. Useful for targeting specific GPU architectures (e.g. Blackwell GB10 vs RTX 3090). Overridden by `meta.cluster.workers`. |

---

## Per-Stage Resource Overrides (`meta.cluster`)

In addition to repository-wide parameters in `.cluster-ci`, Cluster-CI v3 supports fine-grained resource definitions inside `dvc.yaml` under `stages.<stage_name>.meta.cluster`.

Parameters specified in `dvc.yaml` take precedence over `.cluster-ci`:
* `meta.cluster.image` / `image_arm64` / `image_amd64` overrides `DOCKER_IMAGE` / `DOCKER_IMAGE_ARM64` / `DOCKER_IMAGE_AMD64`
* `meta.cluster.cpus` overrides `REQUIRED_CPUS` (default: 2)
* `meta.cluster.gpus` overrides `REQUIRED_GPUS` (default: 0)
* `meta.cluster.ram_gb` overrides `REQUIRED_RAM` / `--ram` (default: 10.0)
* `meta.cluster.vram_gb` overrides `REQUIRED_VRAM` (default: 0.0)
* `meta.cluster.storage_gb` overrides `REQUIRED_STORAGE` / `REQUIRED_DISK` (default: 0.0)
* `meta.cluster.workers` overrides `ALLOWED_WORKERS`

For complete syntax and examples, see the [Per-Stage Resources Guide](stage_resources.md).

---

## Web Application Support

| Parameter | Default | Description |
| :--- | :--- | :--- |
| `EXPOSED_PORT` | *(none)* | Port number to expose from the container to the host network (e.g. `8501` for Streamlit, `7860` for Gradio, `6006` for TensorBoard). The port must be ≥ 1024 and not 5000 or 6000 (reserved by the cluster). When set, the cluster maps this port so your web app is accessible from the [Dashboard](dashboard.md). |
| `CUSTOM_WEB_APP` | `false` | Set to `true` if your pipeline runs a custom web application (Gradio, Streamlit, etc.) instead of the default DVC-Viewer. When enabled, the cluster skips launching the built-in DVC-Viewer and routes traffic directly to your app on the `EXPOSED_PORT`. |

**Example — Exposing a Gradio app:**
```ini
REQUIRED_RAM=16GB
REQUIRED_VRAM=24GB
MAX_RUNTIME_HOURS=4
EXPOSED_PORT=7860
CUSTOM_WEB_APP=true
```

Your Gradio app will then be accessible from the Dashboard while the job is running.

---

## Docker Overrides

These parameters let you customize the Docker image and runtime settings. See the [Docker Containers Guide](containers.md) for detailed usage.

| Parameter | Default | Description |
| :--- | :--- | :--- |
| `DOCKER_IMAGE` | `nvcr.io/nvidia/pytorch:26.05-py3` | Docker image to use for the job container. |
| `DOCKER_PLATFORM` | *(auto-detected)* | Docker platform flag (e.g. `linux/arm64`, `linux/amd64`). |
| `DOCKER_FLAGS` | *(none)* | Extra flags passed directly to `docker run` (e.g. `--cap-add=SYS_NICE`, `--shm-size=16g`). |

All three parameters support **architecture-specific variants** by appending `_ARM64` or `_AMD64`:

```ini
DOCKER_IMAGE_ARM64=nvcr.io/nvidia/pytorch:26.05-py3
DOCKER_IMAGE_AMD64=my-registry/my-image-amd64:latest
DOCKER_FLAGS_ARM64=--cap-add=SYS_NICE
DOCKER_FLAGS_AMD64=--shm-size=16g
```

Architecture-specific values take priority over the global value on matching workers.

---

## Complete Example

```ini
# ── Resource Requirements ──
REQUIRED_RAM=24GB
REQUIRED_VRAM=24GB
MAX_RUNTIME_HOURS=12

# ── Parallel Execution ──
PARALLEL_STAGES=true

# ── Execution Control ──
ALLOWED_WORKERS=gb10-node1,gb10-node2

# ── Docker (optional) ──
DOCKER_IMAGE=ghcr.io/my-org/my-custom-image:latest
DOCKER_FLAGS=--env-file=custom.env

# ── Web Application (optional) ──
# EXPOSED_PORT=7860
# CUSTOM_WEB_APP=true
```

---

## Default Template

When you run the install script, the following minimal template is created:

```ini
REQUIRED_RAM=10GB
REQUIRED_VRAM=0GB
MAX_RUNTIME_HOURS=1
```

Adjust these values to match your project's needs before running `cluster-run`.
