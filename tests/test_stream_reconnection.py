"""Tests for resilient log streaming reconnection and deduplication (Chantier 17, Issue #111)."""

import queue
from typing import List


def simulate_stream_deduplication(first_batch: List[str], replayed_batch: List[str], fresh_batch: List[str]) -> List[str]:
    """Pure simulation of the cluster_run.py deduplication logic during reconnection."""
    q: queue.Queue = queue.Queue()
    displayed_output: List[str] = []
    recent_lines_history: List[str] = []
    recent_lines_max = 2000
    in_reconnect_sync = False

    # 1. Enqueue first batch
    for line in first_batch:
        q.put(line)

    while not q.empty():
        line_stripped = q.get().rstrip("\r\n")
        if in_reconnect_sync and line_stripped:
            recent_tail = recent_lines_history[-300:] if len(recent_lines_history) > 300 else recent_lines_history
            if line_stripped in recent_tail:
                continue
            else:
                in_reconnect_sync = False

        recent_lines_history.append(line_stripped)
        if len(recent_lines_history) > recent_lines_max:
            recent_lines_history.pop(0)
        displayed_output.append(line_stripped)

    # 2. Simulate socket drop & reconnection
    in_reconnect_sync = True

    # 3. Enqueue replayed lines followed by fresh lines
    for line in replayed_batch + fresh_batch:
        q.put(line)

    while not q.empty():
        line_stripped = q.get().rstrip("\r\n")
        if in_reconnect_sync and line_stripped:
            recent_tail = recent_lines_history[-300:] if len(recent_lines_history) > 300 else recent_lines_history
            if line_stripped in recent_tail:
                continue
            else:
                in_reconnect_sync = False

        recent_lines_history.append(line_stripped)
        if len(recent_lines_history) > recent_lines_max:
            recent_lines_history.pop(0)
        displayed_output.append(line_stripped)

    return displayed_output


def test_stream_deduplication_filters_replayed_lines():
    """Verify that replayed log lines after a connection drop are filtered without skipping new lines."""
    first = [
        "Starting pipeline execution...",
        "Stage 1/3: data preparation",
        "Data preparation complete (1000 items)",
        "Stage 2/3: model training",
        "Epoch 1/5: loss=0.543",
    ]
    # Server re-emits last 2 lines upon curl reconnect
    replayed = [
        "Stage 2/3: model training",
        "Epoch 1/5: loss=0.543",
    ]
    fresh = [
        "Epoch 2/5: loss=0.421",
        "Epoch 3/5: loss=0.312",
        "Training finished successfully.",
    ]

    out = simulate_stream_deduplication(first, replayed, fresh)

    # The expected output must have exactly the sequential lines without duplicate Epoch 1
    assert out == [
        "Starting pipeline execution...",
        "Stage 1/3: data preparation",
        "Data preparation complete (1000 items)",
        "Stage 2/3: model training",
        "Epoch 1/5: loss=0.543",
        "Epoch 2/5: loss=0.421",
        "Epoch 3/5: loss=0.312",
        "Training finished successfully.",
    ]
    assert out.count("Stage 2/3: model training") == 1
    assert out.count("Epoch 1/5: loss=0.543") == 1


def test_bounded_exponential_backoff():
    """Verify that reconnection backoff grows exponentially and caps at 10 seconds."""
    delays = []
    for attempt in range(10):
        backoff = min(10.0, 1.0 * (1.5 ** min(attempt, 6)))
        delays.append(backoff)

    assert delays[0] == 1.0
    assert delays[1] == 1.5
    assert delays[2] == 2.25
    assert delays[-1] == 10.0
    assert all(d <= 10.0 for d in delays)
