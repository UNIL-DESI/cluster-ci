# Docker Containers & Compute Environments

All pipeline jobs run inside Docker containers to guarantee isolation and reproducibility. This guide explains the default environment, how to customize it, and special considerations for heavy ML libraries and parallel DAG execution.

---

## 1. Default Environment (NVIDIA NGC PyTorch)

The cluster provides a pre-compiled, GPU-optimized base image so you don't need to compile PyTorch or CUDA from source:

*   **Default Image**: `nvcr.io/nvidia/pytorch:26.05-py3`
*   **Python**: 3.12
*   **PyTorch**: 2.12+ (NVIDIA optimized build)
*   **CUDA**: 13.2
*   **Included**: TensorRT, cuDNN, NCCL, and optimized mathematical libraries.

### How Your Dependencies Are Installed
When a job starts, the cluster installs the packages from your `pyproject.toml` directly into the container using `uv pip install --system`. This ensures your custom packages run on top of the pre-optimized NVIDIA PyTorch binary.

To speed up repeated runs, the cluster **caches your installed packages**. If your dependency files (`pyproject.toml`, `uv.lock`, `requirements.txt`) haven't changed since the last run, the installation step is skipped entirely.

??? note "Internal Details"
    The container runtime includes protective mechanisms (NGC Library Shadowing Protection, vLLM NVSHMEM Stub Symlinking, bitsandbytes CUDA Compatibility Patch) that run transparently. Dependency caching uses a composite MD5 hash of your dependency files. For technical details, see the [Infrastructure Internals](../admin/infrastructure_internals.md#3-container-hardening) documentation.

---

## 2. Overriding the Docker Image

If your project or specific pipeline stages require a different base environment, you can override the Docker settings at two levels:

### A. Stage-Level Overrides (`dvc.yaml`)
In Cluster-CI v3, individual stages can run in different Docker images. Set `image`, `image_arm64`, or `image_amd64` under `meta.cluster` in your `dvc.yaml`:

```yaml
stages:
  specialized_stage:
    cmd: python run.py
    meta:
      cluster:
        image: nvcr.io/nvidia/nemo-automodel:26.04
```

See [Per-Stage Resources Guide](stage_resources.md) for full details.

### B. Repository-Level Overrides (`.cluster-ci`)
You can define a global default image in your [`.cluster-ci` configuration file](configuration.md):

```ini
# Global overrides (applies to both AMD64 and ARM64 workers)
DOCKER_IMAGE=my-custom-registry/my-project-image:latest
DOCKER_PLATFORM=linux/arm64
DOCKER_FLAGS=--env-file=custom.env

# Architecture-specific overrides (takes priority on matching workers)
DOCKER_IMAGE_ARM64=nvcr.io/nvidia/pytorch:26.05-py3
DOCKER_IMAGE_AMD64=my-custom-registry/my-project-image-amd64:latest
```

| Parameter | Description |
| :--- | :--- |
| `DOCKER_IMAGE` / `_ARM64` / `_AMD64` | The Docker image tag to pull and run. |
| `DOCKER_PLATFORM` / `_ARM64` / `_AMD64` | The `--platform` flag (e.g. `linux/arm64`). |
| `DOCKER_FLAGS` / `_ARM64` / `_AMD64` | Arbitrary flags passed directly to `docker run`. |

---

## 3. Container Lifecycle in Parallel Mode (v3)

When running with `PARALLEL_STAGES=true`, branch executors optimize container reuse while strictly enforcing resource boundaries (`src/runner/branch_executor.py:1230-1262`):

### Dynamic Container Reuse & Hot Recreation
* **Warm Container Persistence**: On each assigned worker, the container remains running between consecutive DAG stages of the same job to eliminate container boot overhead.
* **Strict Parameter Comparison**: Before executing the next assigned stage, the branch executor computes the exact Docker runtime arguments (`compute_docker_resource_args`):
  - `--memory` and `--memory-swap`
  - `--cpus`
  - `--gpus` (or `CUDA_VISIBLE_DEVICES`)
  - `--shm-size`
  - `--cgroup-parent`
* **Hot Container Recreation**: If either the required Docker image changes (`action: "switch_image"`) OR any resource constraint changes between two consecutive stages, the executor gracefully stops the active container and launches a fresh container tailored to the new stage's requirements.

### Isolated Per-Image Home Volumes
To preserve package installations across runs while avoiding ABI incompatibilities between different images or Python versions, each image uses a dedicated Docker named volume mounted onto `/home/user`:

```text
cluster-ci-home-${REPO_SLUG}-${IMAGE_SLUG}
```

* For direct local runs submitted via `cluster-run --local`, the repository slug is prefixed with `_local-` (yielding `cluster-ci-home-_local-${REPO_SLUG}-${IMAGE_SLUG}`).
* This guarantees complete hermetic isolation between heterogeneous runtimes (e.g. NVIDIA NeMo vs standard PyTorch) while preserving installed wheels and virtual environments across runs of the same project and image.

### Shared Fast Package Caches
To avoid redundant network downloads across different containers, workspaces, and images on the same physical worker, every execution container mounts shared worker-level cache volumes:

* **`cluster-ci-uv-cache`**: Mounted onto `/root/.cache/uv` (or `/home/user/.cache/uv`), accelerating `uv pip install` resolutions.
* **`cluster-ci-pip-cache`**: Mounted onto `/root/.cache/pip` (or `/home/user/.cache/pip`), providing shared wheel caching.

---

## 4. Unsloth & LLM Fine-Tuning Setup

[Unsloth](https://github.com/unslothai/unsloth) accelerates LLM fine-tuning (up to 2x faster, 70% less memory) but has strict installation requirements. We recommend using a **custom pre-built Docker image** rather than installing it at runtime.

1.  **Build a custom image** inheriting from the NVIDIA PyTorch container:
    ```dockerfile
    FROM nvcr.io/nvidia/pytorch:26.05-py3

    # Install Unsloth dependencies
    RUN pip install --no-cache-dir "unsloth[colab-new] @ git+https://github.com/unslothai/unsloth.git"
    RUN pip install --no-cache-dir --no-deps xformers "trl<0.9.0" peft accelerate datasets
    ```
2.  **Push the image** to your registry (e.g. GitHub Container Registry).
3.  **Configure `.cluster-ci`** (or `dvc.yaml` under `meta.cluster`) to use it:
    ```ini
    REQUIRED_RAM=24GB
    REQUIRED_VRAM=24GB
    MAX_RUNTIME_HOURS=12
    DOCKER_IMAGE=ghcr.io/your-org/unsloth-custom:latest
    ```

---

## 5. Shared Memory & Data Loader Settings

PyTorch `DataLoader` workers use shared memory (`/dev/shm`) to pass tensors between processes. By default, Docker allocates only 64 MB, which causes crashes with `num_workers > 0`.

**The cluster handles this automatically** by injecting `--ipc=host` for all containers, giving your job access to 50% of the host's physical memory as shared memory. No configuration is needed on your part.
