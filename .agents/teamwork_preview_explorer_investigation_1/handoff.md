# Handoff Report: Investigation of "Ghost Jobs" in Cluster-CI

## 1. Observation
- In `src/runner/run_research_pipeline.sh` (lines 65-75), `submit_job.py` is executed within a Bash pipeline: 
  `python3 -u "$BASE_DIR/src/scheduler/submit_job.py" ... 2>&1 | stdbuf -oL -eL tee >(curl ...)`
- In `src/scheduler/submit_job.py` (line 177), `signal_handler(sig, frame)` is defined to intercept `SIGTERM`. The very first instruction in this handler is a `print()` statement, which is located **outside** and **before** the `try...except` block that contains the crucial HTTP cancellation request (`requests.post`).
- In `src/scheduler/headnode_service.py` (line 342), there is a fallback endpoint `@app.route('/clean_ghosts', methods=['POST'])`. However, searches via `grep_search` across the repository (including `.github/workflows`) show that this endpoint is never invoked. There is no `janitor.py`, no GitHub Actions cron, and no background thread in `headnode_service.py` that periodically triggers it.

## 2. Logic Chain
1. When a user cancels a job via the GitHub Actions interface, the GitHub runner sends a `SIGTERM` signal to the entire process group.
2. Both `tee` and `python3 submit_job.py` receive this signal simultaneously.
3. `tee` does not trap `SIGTERM` and is killed immediately by the OS. This immediately closes the read-end of the pipeline.
4. Python intercepts the `SIGTERM` and triggers the asynchronous `signal_handler()`. It immediately attempts to execute `print(f"\n🛑 Signal received...")`.
5. Because `tee` is dead, writing to `stdout` (which is piped) triggers an OS `EPIPE` error, which Python translates into a `BrokenPipeError` exception.
6. Since this `print()` statement is not wrapped in a `try...except` block, the `BrokenPipeError` causes the signal handler to crash abruptly.
7. Consequently, the `requests.post()` call meant to inform the headnode of the cancellation is never executed. The job remains in the database as 'pending' or 'running'.
8. The fallback mechanism (`/clean_ghosts`) fails to purge these lingering jobs because it is "dead code"—an API endpoint that is never actually called by any automated system.

## 3. Caveats
- I did not test the cancellation inside the actual GitHub Actions runner, but the interaction between Bash pipelines, Unix signals (`SIGTERM`), and Python's `BrokenPipeError` is deterministic and fully explains the symptoms.
- No historical context from AIVC was required since the exact code structure reveals the logic flaw perfectly.

## 4. Conclusion
The previous fixes failed due to two architectural oversights:
1. **Broken Pipe Crash**: The `SIGTERM` signal handler in `submit_job.py` crashes before making the HTTP cancellation call because it attempts to `print()` to a broken pipe (`tee` having already died).
2. **Missing Trigger**: The `/clean_ghosts` endpoint exists but has no scheduled trigger mechanism (cron or thread) to execute it.

**Proposed Fix Strategy**:
1. **For `submit_job.py`**: Prioritize the network call over logging. Move the `print()` statement inside the `try...except` block. Ideally, wrap all `print` statements in the signal handler with a mechanism that safely ignores `BrokenPipeError`, or simply perform the `requests.post` before attempting to write to `stdout`.
2. **For `headnode_service.py`**: Implement an internal background thread (similar to `cleanup_inactive_viewers` on line 1267) that runs a `while True` loop with a `time.sleep()` to periodically call the `clean_ghosts()` logic, completely eliminating the need for an external cron.

## 5. Verification Method
- **To reproduce the issue**: Run the pipeline locally `bash -c "python3 src/scheduler/submit_job.py test test | tee output.log"`, find the process group ID, and send `SIGTERM`. Observe that the Python script throws a `BrokenPipeError` traceback and fails to call the cancellation API.
- **To verify the fix**: Apply the proposed changes, repeat the local `SIGTERM` test, and check the `headnode_service.py` logs to confirm that the `update_job_status` cancellation API was successfully reached. Additionally, verify that `clean_ghosts` logs appear periodically in the headnode output once the background thread is added.
