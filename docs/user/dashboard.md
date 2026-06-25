# Monitoring Dashboard Guide

The cluster exposes a real-time web-based monitoring dashboard to track active runs, system health, and resource utilization.

## Accessing the Dashboard

- The dashboard displays running jobs, hardware statistics (CPU, GPU, memory load), and execution logs.
- When exposing web interfaces from your job (e.g., TensorBoard or Gradio), use the `EXPOSED_PORT` configuration in `.cluster-ci`.

## Exposing Custom Ports

In your `.cluster-ci` file:
```env
EXPOSED_PORT=7860
```
This maps your Gradio/Streamlit application running on port `7860` in the container to the cluster proxy, allowing you to access it via the dashboard.
