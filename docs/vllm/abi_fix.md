# vLLM C++ ABI Incompatibility & Resolution Guide

This document describes the root cause of the C++ ABI incompatibility between vLLM and PyTorch inside the NVIDIA NGC container environment, the step-by-step resolution, and how the fix is integrated into the Cluster-CI pipeline.

## 1. Root Cause Analysis (Diagnosis)

### Context & Symptoms
When attempting to import vLLM in a Python environment running inside the NVIDIA NGC container (`nvcr.io/nvidia/pytorch:26.05-py3`), users encountered import failures or dynamic linking errors, such as:
```
ImportError: ... undefined symbol: _ZN5torch...
```
This is a classic **C++ ABI (Application Binary Interface) mismatch** error.

### Technical Analysis
1. **System PyTorch ABI Configuration:**
   NVIDIA NGC PyTorch containers use custom-built versions of PyTorch (e.g., `2.6.0.dev20241217+cu126` on Ubuntu 24.04 / 22.04) compiled with the legacy C++ ABI:
   ```python
   >>> import torch
   >>> print(torch._C._GLIBCXX_USE_CXX11_ABI)
   False
   ```
   This means `_GLIBCXX_USE_CXX11_ABI=0` is active. NVIDIA compiles PyTorch this way to maintain compatibility with proprietary optimized libraries.

2. **vLLM Precompiled Wheels ABI:**
   Standard precompiled wheels of `vllm` distributed via PyPI are built against standard official PyTorch releases compiled with the new C++11 ABI:
   ```python
   _GLIBCXX_USE_CXX11_ABI=1  # (True)
   ```
   Furthermore, PyPI wheels target specific stable versions of PyTorch (e.g., `2.5.1` or `2.4.0`), creating version conflicts with the custom `2.6.0.dev` version in the container.

3. **Incompatibility:**
   When Python attempts to dynamically load the vLLM C++/CUDA extension libraries (`_C.abi3.so`), the dynamic linker (`ld.so`) fails to resolve symbols referencing PyTorch C++ classes (like `c10` or `torch::jit`) because the compiled signatures differ completely between `ABI=0` and `ABI=1` (e.g., different types for `std::string`).

---

## 2. Resolution (Action)

The only robust solution is to **build vLLM from source inside the target NGC container**. 

By building from source within the container:
- The build process uses the system's PyTorch compiler headers (`torch/utils/cpp_extension.py`).
- It automatically inherits `_GLIBCXX_USE_CXX11_ABI=0` from the active PyTorch instance.
- It links against the highly optimized system-provided CUDA, NCCL, and cuDNN.

### Step-by-Step Compilation Script
A dedicated script was created at `scripts/install_vllm_source.sh` to automate this process. It includes optimizations for the high-end **NVIDIA Blackwell** GPU.

```bash
#!/bin/bash
set -e

# 1. Optimize compilation concurrency for high-end CPU (e.g., 64-core)
export MAX_JOBS=32

# 2. Automatically target compilation for the active GPU architecture
GPU_CC=$(python3 -c "import torch; print(torch.cuda.get_device_properties(0).major)")
GPU_SUB=$(python3 -c "import torch; print(torch.cuda.get_device_properties(0).minor)")
export TORCH_CUDA_ARCH_LIST="${GPU_CC}.${GPU_SUB}" # e.g., 10.0 for Blackwell

# 3. Clean-up previous attempts
cd /home/user
rm -rf vllm

# 4. Clone stable vLLM branch
git clone --depth 1 --branch v0.7.3 https://github.com/vllm-project/vllm.git
cd vllm

# 5. Patch PyTorch strict dependency constraints to allow the system PyTorch
python3 use_existing_torch.py

# 6. Install build requirements
pip install --break-system-packages cmake ninja setuptools-scm

# 7. Compile and install in editable mode without build isolation
pip install --break-system-packages --no-build-isolation -e .
```

### Validation & Verification
After running the build script on the worker `cluster-node-3` (`130.223.169.200`), the installation was tested successfully:
```python
>>> import vllm
>>> print(vllm.__version__)
0.7.3
>>> from vllm import LLM
>>> # Successfully loaded!
```

---

## 3. Integration with Cluster-CI

To prevent the automated pip dependency resolver of the CI (`smart_install.sh`) from overriding this custom-built vLLM library and the optimized system PyTorch during future pipeline runs, the Cluster-CI setup has been modified.

### Changes in `src/runner/smart_install.sh`
We updated the dependency installer to inject `--exclude` arguments into the `uv pip install` call:
1. Always exclude `torch`, `torchvision`, and `torchaudio` to safeguard the pre-installed system PyTorch.
2. Dynamically exclude `vllm` if it is already installed and successfully imported in the environment.

```diff
# Install project with system packages, allowing pre-releases for NGC PyTorch
-uv pip install --system --break-system-packages --prerelease allow --prefix /home/user/.local -e .
+EXCLUDE_ARGS="--exclude torch --exclude torchvision --exclude torchaudio"
+if python3 -c "import vllm" 2>/dev/null; then
+    echo "ℹ️  [Cluster-CI] vLLM is pre-installed/compiled. Excluding it from pip install to prevent overwrite."
+    EXCLUDE_ARGS="$EXCLUDE_ARGS --exclude vllm"
+fi
+
+uv pip install --system --break-system-packages --prerelease allow --prefix /home/user/.local $EXCLUDE_ARGS -e .
```

This prevents any automatic pipeline run from overwriting the optimized Blackwell compilation, ensuring 100% stable execution of LLM inference stages.
