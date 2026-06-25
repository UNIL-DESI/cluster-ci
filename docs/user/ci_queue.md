# CI & Queue Scheduler

The cluster scheduler orchestrates resource allocation on Ubuntu 24.04 workers equipped with NVIDIA Blackwell GPUs.

## Configuration File `.cluster-ci`

To define constraints for your job, place a `.cluster-ci` environment file at the root of your project:

```env
REQUIRED_RAM=4GB
REQUIRED_VRAM=24GB
MAX_RUNTIME_HOURS=2
```

- `REQUIRED_RAM`: Placement constraint for CPU RAM (defaults to 2GB).
- `REQUIRED_VRAM`: Placement constraint for GPU VRAM (defaults to 0, i.e., CPU only).
- `MAX_RUNTIME_HOURS`: Hard limit for job execution (maximum 24 hours, mandatory).
- `STAGES`: Leave empty by default to run the entire pipeline (`dvc repro`).
