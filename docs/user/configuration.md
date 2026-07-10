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

# Optional: restrict to specific workers
ALLOWED_WORKERS=gb10-node1,gb10-node2

# Optional: run only specific DVC stages
STAGES=train
```

---

## Parameter Reference

### Essential Parameters

These parameters control resource allocation and job timeout. Every `.cluster-ci` file should include them.

| Parameter | Required | Default | Description |
| :--- | :--- | :--- | :--- |
| `MAX_RUNTIME_HOURS` | **Yes** | — | Maximum allowed runtime in hours (1–24). The job is automatically killed if it exceeds this limit. |
| `REQUIRED_RAM` | No | `2GB` | Minimum physical RAM required on the worker. The cluster reserves 8 GB per worker for the OS, so a 128 GB worker can accept up to 120 GB. |
| `REQUIRED_VRAM` | No | `0GB` | Minimum GPU VRAM required. Set to `0GB` (or omit) to allow execution on CPU-only nodes. |

!!! warning "Set realistic values"
    If you request more RAM or VRAM than any worker can provide, your job will stay in the queue indefinitely. Check the [Dashboard](dashboard.md) to see available worker capacities.

### Execution Control

| Parameter | Default | Description |
| :--- | :--- | :--- |
| `STAGES` | *(empty — runs full pipeline)* | Comma-separated list of DVC stage names to execute. If empty or set to `all`, the cluster runs `dvc repro` (the full pipeline). To run a subset, specify the **last stage** you want — DVC will automatically run all its upstream dependencies. |
| `ALLOWED_WORKERS` | *(empty — all workers eligible)* | Comma-separated list of worker hostnames. Only these workers will be considered for scheduling. Useful for targeting specific GPU architectures (e.g. Blackwell GB10 vs RTX 3090). |

### Web Application Support

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

### Docker Overrides

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

# ── Execution Control ──
STAGES=train
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
REQUIRED_RAM=2GB
REQUIRED_VRAM=0GB
MAX_RUNTIME_HOURS=1
```

Adjust these values to match your project's needs before running `cluster-run`.
