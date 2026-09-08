# GPU memory reservation and enforcement

`REQUIRED_VRAM` is a per-GPU requirement on discrete-GPU workers. Worker
registration reports the largest individual GPU's capacity; the scheduler
compares that capacity with the requested value. Two 24GB GPUs therefore do
not qualify for an unsplittable 32GB request. Reporting 48GB would incorrectly
allow that placement. The scheduler does not model GPU count requirements or
decide how a workload partitions its model across GPUs.

The runner gives its container access to all GPUs. The scheduler reserves
the entire worker for one scheduled job at a time, including jobs submitted
under different usernames. Multi-GPU execution must be implemented by the
workload itself.

The discrete-GPU watchdog compares the maximum `memory.used` across cards
with `REQUIRED_VRAM`. This is equivalent to requiring every card to stay
within that limit. Summing incorrectly rejects 16GB + 16GB against a 24GB
per-GPU allowance. Averaging incorrectly accepts 24GB + 8GB against a 16GB
allowance. A single-GPU worker retains its existing behavior.

The existing two-consecutive-sample soft-limit policy and polling interval
are unchanged. GB10 unified-memory monitoring still uses
`MemTotal - MemAvailable`, including its immediate 90%-of-system-RAM hard
threshold. On discrete GPUs, this script does not independently monitor host
RAM: its existing hard threshold is compared with the GPU reading. The
runner separately applies a Docker RAM limit.

These measurements are host-wide, not attributed to a container. Manually
started GPU processes can affect the reading. Concurrent GPU-sharing jobs
would require explicit device assignment, container device restrictions and
matching accounting; this watchdog change does not implement that support.

## Tests

Run without GPUs, Docker access or third-party Python packages:

```sh
python3 -m unittest discover -s src/runner -p test_gpu_watchdog.py -v
bash -n src/runner/gpu_watchdog.sh
```

The tests execute the actual Bash polling loop with synthetic GPU and RAM
readings. Docker inspect/kill and sleep are replaced with local fixtures;
no real container is contacted. They cover balanced and unequal multi-GPU
use, exact limits, either card exceeding its allowance, transient breaches,
single-GPU behavior and unified-memory soft/hard enforcement.

These tests do not establish live driver, container or scheduler integration.
A deployment should additionally run a non-confidential local job on the
dual-GPU worker to verify both-card access and survival when combined usage
exceeds the per-GPU allowance, and a bounded over-limit job to verify stopping
and result return. Verify unified-memory jobs on GB10 workers as well.
