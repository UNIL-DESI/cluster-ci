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
        ram_gb: 40
        vram_gb: 40
        storage_gb: 50
        workers:
          - HEC45801
          - HEC45803
```

All fields under `meta.cluster` are **optional**. If a field is omitted, Cluster-CI falls back to repository-level configuration or cluster defaults.

### The 6 Resource Fields

| Field | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `image` | `string` | `nvcr.io/nvidia/pytorch:26.05-py3` | Docker base image used to execute the stage. Architecture-specific overrides can be provided using `image_arm64` and `image_amd64`. |
| `cpus` | `integer` | `4` | Number of CPU cores allocated for the stage. |
| `ram_gb` | `float` | `10.0` | Minimum physical host RAM in GB required for the stage. |
| `vram_gb` | `float` | `0.0` | Minimum GPU VRAM in GB required. When set to `0`, the stage can execute on CPU-only nodes. |
| `storage_gb` | `float` | `0.0` | Minimum free disk space in GB required on the worker. A value of `0` disables disk space checks. |
| `workers` | `list[string]` | All workers | Whitelist of worker hostnames eligible to execute this stage. |

!!! danger "Strict Validation (Fail-Fast)"
    Any unrecognized key under `meta.cluster` causes **immediate job rejection** at submission time (HTTP 400). Cluster-CI strictly rejects typos (e.g. `gpu_gb` or `cpu` instead of `vram_gb` or `cpus`) rather than silently ignoring them.

---

## Priority Order (Precedence Rules)

When both stage-level `meta.cluster` in `dvc.yaml` and repository-level parameters in `.cluster-ci` are defined, Cluster-CI applies the following precedence order:

| Parameter | Precedence Resolution |
| :--- | :--- |
| **Docker Image** | `meta.cluster.image_<arch>` > `meta.cluster.image` > `DOCKER_IMAGE_<ARCH>` (`.cluster-ci`) > `DOCKER_IMAGE` (`.cluster-ci`) > Default (`nvcr.io/nvidia/pytorch:26.05-py3`) |
| **RAM** | `meta.cluster.ram_gb` > `REQUIRED_RAM` (`.cluster-ci`) > Default (`10.0` GB) |
| **VRAM** | `meta.cluster.vram_gb` > `REQUIRED_VRAM` (`.cluster-ci`) > Default (`0.0` GB) |
| **CPUs** | `meta.cluster.cpus` > Default (`4` cores) |
| **Storage** | `meta.cluster.storage_gb` > Default (`0.0` GB — no check) |
| **Workers** | `meta.cluster.workers` > `ALLOWED_WORKERS` (`.cluster-ci`) > All admissible workers |

!!! info "Backward Compatibility"
    If your `dvc.yaml` does not contain any `meta.cluster` blocks, the global values in your `.cluster-ci` file (such as `REQUIRED_RAM`, `REQUIRED_VRAM`, and `DOCKER_IMAGE`) continue to apply to every stage in the pipeline.

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
<!-- v3: à vérifier contre l'implémentation : syntaxe d'interpolation item dans meta.cluster dvc.yaml -->
