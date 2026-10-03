# Per-Stage Resources (`meta.cluster`)

Cluster-CI v3 introduces fine-grained, stage-level resource specification directly inside `dvc.yaml`. Instead of applying uniform hardware constraints to an entire pipeline, each stage can declare its own compute, memory, GPU, disk, and container requirements.

---

## Overview

In complex research pipelines, different stages have vastly different hardware requirements:
* Data preprocessing might need high CPU and RAM, but zero GPU VRAM.
* Large language model fine-tuning requires significant GPU VRAM and specific CUDA images.
* Evaluation or lightweight metric calculation only requires minimal resources.

By specifying resources per stage in `dvc.yaml` under `meta.cluster`, the Cluster-CI v3 scheduler matches each individual stage to the best-suited worker and allocates only the resources needed.

---

## Syntax and Available Fields

Stage-level resources are declared inside `dvc.yaml` under `stages.<stage_name>.meta.cluster`:

```yaml
stages:
  train_model:
    cmd: python src/train.py --batch-size 32
    deps:
      - data/processed.parquet
      - src/train.py
    outs:
      - models/model.pt
    meta:
      cluster:
        image: nvcr.io/nvidia/nemo-automodel:26.04
        image_arm64: nvcr.io/nvidia/nemo-automodel:26.04
        image_amd64: nvcr.io/nvidia/nemo-automodel:26.04-x86
        cpus: 8
        gpus: 1
        ram_gb: 40
        vram_gb: 40
        storage_gb: 50
        workers:
          - HEC45801
          - HEC45803
```

All fields under `meta.cluster` are **optional**. If a field is omitted, Cluster-CI falls back to repository-level configuration or cluster defaults.

### All 9 Resource Fields

| Field | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `image` | `string` | `nvcr.io/nvidia/pytorch:26.05-py3` | Default Docker base image for the stage. |
| `image_arm64` | `string` | `null` | Architecture override for ARM64 workers (e.g. NVIDIA Grace Blackwell GB10). If omitted, falls back to `image`. |
| `image_amd64` | `string` | `null` | Architecture override for x86_64 / AMD64 workers (e.g. dual-mode Headnode). If omitted, falls back to `image`. |
| `cpus` | `integer` | `2` | Number of CPU cores allocated for the stage (`>= 1`). |
| `gpus` | `integer` | `0` | Number of physical GPUs allocated for the stage (`>= 0`). |
| `ram_gb` | `float` | `10.0` | Minimum physical host RAM in GB required (`>= 0.0`). |
| `vram_gb` | `float` | `0.0` | Minimum GPU VRAM in GB required (`>= 0.0`). When `0.0`, stage runs on CPU-only nodes. |
| `storage_gb` | `float` | `0.0` | Minimum free disk space in GB required on worker (`>= 0.0`). Set `0.0` to disable disk check. |
| `workers` | `list[string]` | `null` (All workers) | Whitelist of worker hostnames eligible to execute this stage (e.g. `['HEC45801']`). |

!!! danger "Strict Validation & Consistency Rules (Fail-Fast)"
    * **Schema Validation**: Any unrecognized key under `meta.cluster` causes **immediate job rejection** at submission time (HTTP 400). Valid allowed keys are: `image`, `image_arm64`, `image_amd64`, `cpus`, `gpus`, `ram_gb`, `vram_gb`, `storage_gb`, `workers`.
    * **Type Validation**: Types are strictly enforced: `cpus` must be an integer `>= 1`, `gpus` an integer `>= 0`, `ram_gb`/`vram_gb`/`storage_gb` numbers `>= 0`, and `workers` a list of strings.
    * **GPU Consistency Rule (A16/A17)**: Declaring `vram_gb > 0` strictly requires `gpus >= 1`. If `vram_gb > 0` while `gpus == 0`, Cluster-CI raises an immediate actionable error:
      ```text
      Fichier dvc.yaml, stage '<stage>' : incohérence de ressources entre 'meta.cluster.vram_gb' (X Go) et 'meta.cluster.gpus' (0).
      Cause : vram_gb exige gpus >= 1 (la mémoire vidéo ne peut être allouée sans GPU).
      Remède : déclarez 'gpus: 1' (ou plus) sous meta.cluster dans dvc.yaml (ou REQUIRED_GPUS dans .cluster-ci), ou fixez vram_gb à 0.
      ```

---

## Unified Memory (GB10) vs. Discrete GPUs

Cluster-CI v3 transparently schedules workloads across both unified memory nodes and discrete GPU nodes:

| Memory Architecture | Hardware Example | Scheduler Admission Check | Docker Container Enforcement |
| :--- | :--- | :--- | :--- |
| **Unified Memory** | NVIDIA Grace Blackwell (GB10) | `(ram_gb + vram_gb) <= total_ram_gb - 8.0`<br>*(8 GB OS headroom reserve)* | `--memory` is set to `ram_gb + vram_gb` combined, covering the unified NVLink-C2C pool. |
| **Discrete GPU** | Headnode (2× RTX 3090) / x86 nodes | `ram_gb <= available_ram_gb - 4.0`<br>`vram_gb <= available_vram_per_gpu` | `--memory` is set strictly to `ram_gb`. GPUs are allocated by index and isolated via `CUDA_VISIBLE_DEVICES`. |

---

## Priority Order (Precedence Rules)

When both stage-level `meta.cluster` in `dvc.yaml` and repository-level parameters in `.cluster-ci` are defined, Cluster-CI applies the following strict precedence order:

| Parameter | Precedence Resolution |
| :--- | :--- |
| **Docker Image** | `meta.cluster.image_<arch>` > `meta.cluster.image` > `DOCKER_IMAGE_<ARCH>` (`.cluster-ci`) > `DOCKER_IMAGE` (`.cluster-ci`) > Default (`nvcr.io/nvidia/pytorch:26.05-py3`) |
| **CPUs** | `meta.cluster.cpus` > `REQUIRED_CPUS` (`.cluster-ci`) > Default (`2` cores) |
| **GPUs** | `meta.cluster.gpus` > `REQUIRED_GPUS` (`.cluster-ci`) > Default (`0` GPUs) |
| **RAM** | `meta.cluster.ram_gb` > `REQUIRED_RAM` or `--ram` (`.cluster-ci`) > Default (`10.0` GB) |
| **VRAM** | `meta.cluster.vram_gb` > `REQUIRED_VRAM` (`.cluster-ci`) > Default (`0.0` GB) |
| **Storage** | `meta.cluster.storage_gb` > `REQUIRED_STORAGE` or `REQUIRED_DISK` (`.cluster-ci`) > Default (`0.0` GB) |
| **Workers** | `meta.cluster.workers` > `ALLOWED_WORKERS` (`.cluster-ci`) > All admissible workers |

!!! info "Automatic GPU Defaulting from `.cluster-ci`"
    If you specify `REQUIRED_VRAM > 0` in `.cluster-ci` without setting `REQUIRED_GPUS`, Cluster-CI automatically defaults `gpus` to `1` so the GPU consistency check passes seamlessly.

!!! info "Backward Compatibility"
    If your `dvc.yaml` does not contain any `meta.cluster` blocks, the global values in your `.cluster-ci` file continue to apply to every stage in the pipeline.

---

## Complete Example with `foreach` Matrix

DVC supports parameterized stages using `foreach`. In Cluster-CI v3, each generated stage automatically receives stage-level resource definitions, with support for parameterized resource sizing:

```yaml
stages:
  prepare_data:
    cmd: python src/prepare.py
    deps:
      - data/raw.csv
    outs:
      - data/splits.json
    meta:
      cluster:
        cpus: 8
        ram_gb: 32
        vram_gb: 0

  train_variant:
    foreach:
      small:
        model_name: "llama-1b"
        vram: 16
        ram: 20
        image: "nvcr.io/nvidia/pytorch:26.05-py3"
      large:
        model_name: "llama-8b"
        vram: 80
        ram: 100
        image: "nvcr.io/nvidia/nemo-automodel:26.04"
    do:
      cmd: python src/train.py --model ${item.model_name}
      deps:
        - data/splits.json
        - src/train.py
      outs:
        - models/${item.model_name}.pt
      meta:
        cluster:
          image: ${item.image}
          ram_gb: ${item.ram}
          vram_gb: ${item.vram}
          cpus: 8
          gpus: 1

  evaluate:
    cmd: python src/evaluate.py
    deps:
      - models/llama-1b.pt
      - models/llama-8b.pt
    metrics:
      - metrics.json:
          cache: false
    meta:
      cluster:
        cpus: 4
        ram_gb: 16
        vram_gb: 0
```

In this example:
1. `prepare_data` runs on a CPU worker with 32 GB RAM.
2. `train_variant@small` (16 GB VRAM) can run on discrete GPU workers (such as RTX 3090) or Grace Blackwell workers.
3. `train_variant@large` (80 GB VRAM) is scheduled on Grace Blackwell unified-memory nodes (GB10).
4. `evaluate` runs on any node once both training variants finish.
