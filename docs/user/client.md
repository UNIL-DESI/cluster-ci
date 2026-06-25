# Command-Line Client Guide

The Cluster-CI system provides a command-line utility to interface with the cluster scheduler, submit experiments, and monitor jobs.

## Using `cluster-run`

To submit a local experiment to the cluster without waiting for GitHub Actions CI:

```bash
cluster-run
```

This will:
1. Create a shadow commit of your current state.
2. Push it to the remote cluster queue.
3. Stream the execution logs to your terminal in real-time.

## Key CLI Commands

- `cluster-run`: Submits current directory/workspace state and streams logs.
- `cluster-run list`: Lists recent and current runs on the scheduler.
- `cluster-run view <run_id>`: Resumes log streaming for a specific run.
- `cluster-run cancel <run_id>`: Cancels an active or queued run.
