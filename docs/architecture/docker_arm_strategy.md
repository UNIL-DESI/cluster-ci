# Docker Multi-Architecture Strategy (ARM64 & AMD64)

## 1. Context & The Multi-Arch Challenge
The Cluster-CI infrastructure runs on a **heterogeneous cluster** combining ARM64 workers (NVIDIA Blackwell GB10) and an AMD x86_64 headnode (dual-mode: scheduler + executor with 2× RTX 3090). The Python/PyPI ecosystem does not always provide pre-compiled wheels for `aarch64` (ARM), particularly for heavy compute libraries like PyTorch, Ray, grpcio, or SciPy.

## 2. Per-Architecture Image Selection

The runner automatically detects the host architecture via `uname -m` and selects the appropriate Docker image:

| Architecture | Environment Variable | Default Image |
|---|---|---|
| `x86_64` (AMD64) | `DOCKER_IMAGE_AMD64` | `nvcr.io/nvidia/pytorch:26.04-py3` |
| `aarch64` (ARM64) | `DOCKER_IMAGE_ARM64` | `nvcr.io/nvidia/pytorch:26.04-py3` |
| Other / Legacy | `DOCKER_BASE_IMAGE` | Fallback (backward-compatible) |

**Key implementation details:**
- The `--platform` flag is automatically injected in all `docker run` and `docker pull` commands to ensure correct image variant selection.
- `DOCKER_BASE_IMAGE` is preserved as a **legacy fallback** for backward compatibility. If per-architecture variables are not set, the runner falls back to `DOCKER_BASE_IMAGE`.
- Resolution order: `DOCKER_IMAGE_<ARCH>` → `DOCKER_BASE_IMAGE` → hardcoded default.

## 3. The Hybrid Strategy: "Golden Image" + Dynamic Dependencies

### A. The "Golden Image" (Heavy Core)
Rather than installing hard-to-compile libraries at runtime, we use a base Docker image (the "Golden Image").
- **Current image**: `nvcr.io/nvidia/pytorch:26.04-py3` (NGC container with Python 3.12, PyTorch 2.12, CUDA 13.2)
- **Role**: Provides the OS, CUDA/TensorRT drivers, and natively optimized critical libraries (PyTorch).
- If researchers need additional complex libraries (e.g. `ray`), an administrator can create a custom image inheriting from the NGC base. The `DOCKER_IMAGE_AMD64` / `DOCKER_IMAGE_ARM64` variables in the `.env` will point to the appropriate custom images per architecture.

### B. Dynamic Injection via `pyproject.toml` (Lightweight Plugins)
Researchers develop their code by listing their (lightweight) dependencies in their `pyproject.toml`.
- **Mechanism**: At job launch, the orchestrator (`run_research_pipeline.sh`) detects the architecture (`aarch64`) and bypasses `uv sync`.
- It executes instead: `uv pip install --system . || uv pip install --system -r pyproject.toml`
- **Benefit**: This installs missing libraries (pandas, tqdm, requests...) directly into the container's main Python environment (which is ephemeral).
- **Result**: Researcher dependencies are layered on top of the heavy core. The absence of a `.venv` allows code to use NVIDIA's optimized PyTorch (located in `/usr/local/lib/python...`).

## 4. Governance for Researchers

1. **No special configuration required**: Researchers can use `uv.lock` on their Windows/Mac/Linux PC (x86_64) without issues.
2. **Priority to pyproject.toml**: On the cluster (both ARM64 and AMD64 workers), only `pyproject.toml` is read. Dependencies are resolved dynamically for the target architecture.
3. **Golden Image updates**: If a project consistently fails on an unavailable dependency (C++ blocker), the researcher should request its integration into the lab's Golden Image.

> **💡 Note on x86_64 execution**: When a Cluster-CI worker runs on the x86_64 headnode (dual-mode), the same NGC container is used with the `--platform linux/amd64` flag. The orchestrator applies the same dynamic dependency injection strategy for consistency across architectures.
