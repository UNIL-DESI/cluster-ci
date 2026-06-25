# DVC & Storage Guide

Data Version Control (DVC) is used on the cluster to manage large datasets and models efficiently without overloading Git.

## Key Storage Rules

- **P2P Cache**: Large data dependencies (`deps`) and outputs (`outs`) are managed by the cluster via peer-to-peer storage mechanisms. Do not commit these files to Git.
- **DVC Configuration**: The file `dvc.yaml` defines the stages, inputs, outputs, metrics, and plots of your pipeline.
- **Metrics & Plots**: Ensure metrics and plots are declared with `cache: false` in `dvc.yaml` so they sync back via Git automatically.

## Declaring a Stage

An example stage in `dvc.yaml`:

```yaml
stages:
  train:
    cmd: python3 src/train.py --lr 0.01
    deps:
      - src/train.py
      - data/dataset.csv
    outs:
      - models/model.pt
    metrics:
      - metrics.json:
          cache: false
```
