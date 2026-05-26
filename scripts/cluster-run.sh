#!/bin/bash
set -e

# Disable Python output buffering globally
export PYTHONUNBUFFERED=1


# Cluster-CI Run CLI
# Helps researchers submit jobs via "Shadow Push" to a draft branch.

# Global variables for cleanup
RUN_ID=""
BRANCH=""
USER_INTERRUPTED="false"

trap_ctrl_c() {
    USER_INTERRUPTED="true"
    exit 130
}
trap trap_ctrl_c SIGINT

show_help() {
    echo "Usage: cluster-run [COMMAND] [OPTIONS]"
    echo ""
    echo "Commands:"
    echo "  (default)           Package changes and submit a shadow run"
    echo "  list                List recent cluster runs"
    echo "  view <run_id>       View details/logs of a specific run"
    echo "  cancel <run_id>     Cancel a specific run"
    echo ""
    echo "Options:"
    echo "  --background, -b    Submit the run and exit without watching logs"
    echo "  --help, -h          Show this help message"
    echo ""
}

check_dependencies() {
    if ! command -v gh &> /dev/null; then
        echo "❌ Error: github-cli (gh) is not installed."
        echo "Please install it: https://cli.github.com/"
        exit 1
    fi
    if ! git rev-parse --is-inside-work-tree &> /dev/null; then
        echo "❌ Error: Not in a git repository."
        exit 1
    fi
}

check_gh_auth() {
    if ! gh auth status >/dev/null 2>&1; then
        echo "🔐 GitHub CLI not authenticated. Starting login..."
        gh auth login
    fi
}

get_current_user() {
    gh api user -q .login
}

cleanup() {
    # If we have a branch, try to delete it
    if [ -n "$BRANCH" ]; then
        # If we have a run_id, try to cancel it ONLY if the user manually interrupted (Ctrl+C)
        if [ -n "$RUN_ID" ] && [ "$USER_INTERRUPTED" = "true" ]; then
             local raw_status=$(gh run view "$RUN_ID" --json status -q .status < /dev/null 2>/dev/null || echo "completed"); local status=$(echo "$raw_status" | tr -cd 'a-zA-Z')
             if [[ "$status" != "completed" && "$status" != "success" && "$status" != "failure" && "$status" != "cancelled" ]]; then
                 # Cancel silently to avoid scary terminal output
                 gh run cancel "$RUN_ID" < /dev/null >/dev/null 2>&1 || true
             fi
        fi
        # Silence branch deletion
        git push origin --delete "$BRANCH" --quiet >/dev/null 2>&1 || true
    fi
}

stream_logs() {
    local run_id=$1
    local commit_sha=$2
    local repo_full_name
    repo_full_name=$(git config --get remote.origin.url 2>/dev/null | sed -E 's/.*github.com[:\/](.*)\.git/\1/' 2>/dev/null)
    [ -z "$repo_full_name" ] && repo_full_name="UNIL-DESI/cluster-ci"

    local tmate_connected=false

    # ──────────────────────────────────────────────────────────────────────
    # 1. Poll for tmate reverse SSH session (published via GitHub Commit Status API)
    # ──────────────────────────────────────────────────────────────────────
    if [ -n "$commit_sha" ]; then
        echo "🔍 Polling for live terminal connection (timeout ~4 mins)..."
        local spin_idx=0
        local spin_chars="/-\|"

        for attempt in {1..120}; do
            # Check if the run has already completed (no point connecting)
            local run_status
            run_status=$(gh run view "$run_id" --json status -q '.status' 2>/dev/null || echo "completed")
            if [ "$run_status" == "completed" ] || [ "$run_status" == "cancelled" ]; then
                break
            fi

            local status_json
            status_json=$(gh api "repos/$repo_full_name/commits/$commit_sha/statuses" 2>/dev/null || true)

            if [ -n "$status_json" ]; then
                local tmate_url
                tmate_url=$(echo "$status_json" | python3 -c "import sys, json; data = json.load(sys.stdin); item = next((x for x in data if x.get('context') == 'tmate'), None); print(item['target_url'] if item else '')" 2>/dev/null || echo "")
                local tmate_ssh
                tmate_ssh=$(echo "$status_json" | python3 -c "import sys, json; data = json.load(sys.stdin); item = next((x for x in data if x.get('context') == 'tmate'), None); print(item['description'].replace('SSH: ', '') if item else '')" 2>/dev/null || echo "")

                if [ -n "$tmate_ssh" ] && [[ "$tmate_ssh" == ssh* ]]; then
                    printf "\r\033[K"
                    echo "🟢 Live terminal stream found!"
                    echo "🔗 Web: $tmate_url"
                    echo "🔌 SSH: $tmate_ssh"
                    echo "⚡ Capturing real-time logs from runner (streaming to your terminal)..."
                    echo "=========================================================================="

                    # ── KEY TECHNIQUE: direct SSH pipe + Python filter ──
                    # SSH with -tt forces a pseudo-TTY so tmate/tmux works.
                    # All output is piped through a Python filter that strips
                    # tmux/ANSI escape sequences in real-time. Clean output
                    # goes directly to stdout — persistent in the terminal.
                    local filter_script
                    filter_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/tmate_log_filter.py"
                    if [ ! -f "$filter_script" ]; then
                        # Fallback: look next to the installed binary
                        filter_script="$(dirname "$(command -v cluster-run 2>/dev/null || echo "$0")")/tmate_log_filter.py"
                    fi
                    if [ ! -f "$filter_script" ]; then
                        # Last resort: installed location
                        filter_script="$HOME/.local/bin/tmate_log_filter.py"
                    fi

                    $tmate_ssh -o StrictHostKeyChecking=accept-new \
                               -o ServerAliveInterval=10 \
                               -o ServerAliveCountMax=3 \
                               -tt 2>&1 | python3 -u "$filter_script" || true

                    echo "=========================================================================="

                    tmate_connected=true
                    break
                fi
            fi

            local char="${spin_chars:spin_idx:1}"
            spin_idx=$(( (spin_idx + 1) % 4 ))

            if [ "$run_status" == "queued" ]; then
                printf "\r⏳ Waiting in GitHub Actions queue [%s] (allocation of a runner slot)..." "$char"
            else
                printf "\r⏱️  Booting job environment [%s] (registering with scheduler)..." "$char"
            fi

            sleep 2
        done
        printf "\r\033[K"
    fi

    # ──────────────────────────────────────────────────────────────────────
    # 2. If tmate connected, wait for GHA to report final conclusion
    # ──────────────────────────────────────────────────────────────────────
    if [ "$tmate_connected" = true ]; then
        # tmate session ended but GHA might still be finalizing (cleanup, sync, etc.)
        echo "⏳ Waiting for GitHub Actions to finalize..."
        for i in {1..30}; do
            local final_json
            final_json=$(gh run view "$run_id" --json status,conclusion 2>/dev/null || echo "")
            local final_status=""
            local final_conclusion=""
            if [ -n "$final_json" ]; then
                final_status=$(echo "$final_json" | python3 -c "import sys, json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
                final_conclusion=$(echo "$final_json" | python3 -c "import sys, json; print(json.load(sys.stdin).get('conclusion',''))" 2>/dev/null || echo "")
            fi
            if [ "$final_status" == "completed" ] && [ -n "$final_conclusion" ]; then
                if [ "$final_conclusion" == "success" ]; then
                    echo "✅ Cluster-CI run completed successfully!"
                else
                    echo "❌ Cluster-CI run finished with status: $final_conclusion"
                fi
                return 0
            fi
            sleep 2
        done
        echo "⚠️  Tmate session ended. GHA status could not be determined."
        return 0
    fi

    # ──────────────────────────────────────────────────────────────────────
    # 3. Fallback: wait for GHA completion and dump logs (no real-time)
    # ──────────────────────────────────────────────────────────────────────
    echo "📺 Live terminal not available. Waiting for GHA completion to fetch logs..."
    local spin_idx=0
    local spin_chars="/-\|"

    while true; do
        local run_status_json
        run_status_json=$(gh run view "$run_id" --json status,conclusion 2>/dev/null || echo "")

        local info="queued"
        local conclusion=""
        if [ -n "$run_status_json" ]; then
            info=$(echo "$run_status_json" | python3 -c "import sys, json; data = json.load(sys.stdin); print(data.get('status', 'queued'))" 2>/dev/null || echo "queued")
            conclusion=$(echo "$run_status_json" | python3 -c "import sys, json; data = json.load(sys.stdin); print(data.get('conclusion', ''))" 2>/dev/null || echo "")
        fi

        local char="${spin_chars:spin_idx:1}"
        spin_idx=$(( (spin_idx + 1) % 4 ))

        if [ "$info" == "completed" ] || [ -n "$conclusion" ]; then
            printf "\r\033[K"
            echo "📥 Job completed. Fetching consolidated logs..."
            echo "=========================================================================="
            local logs
            logs=$(gh run view "$run_id" --log 2>/dev/null || true)
            if [ -n "$logs" ]; then
                echo "$logs" | awk -F'\t' '{
                    step=$2;
                    log_line=$3;
                    gsub(/^\xEF\xBB\xBF/, "", log_line);
                    gsub(/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]+Z /, "", log_line);
                    gsub(/##\[group\]/, "▶️  ", log_line);
                    gsub(/##\[endgroup\]/, "", log_line);
                    if (log_line != "") {
                        printf "\033[90m[%s]\033[0m %s\n", step, log_line;
                    }
                }'
            fi
            echo "=========================================================================="
            if [ "$conclusion" == "success" ]; then
                echo "✅ Cluster-CI run completed successfully!"
                return 0
            elif [ "$conclusion" == "cancelled" ]; then
                echo "⚠️  Cluster-CI run was cancelled."
                return 1
            else
                echo "❌ Cluster-CI run finished with status: ${conclusion:-failed}"
                return 1
            fi
        fi

        if [ "$info" == "queued" ]; then
            printf "\r⏳ Waiting in GitHub Actions queue [%s]..." "$char"
        else
            printf "\r⏱️  Job in progress [%s] (logs will appear on completion)..." "$char"
        fi
        sleep 3
    done
}

fetch_results() {
    local branch=$1
    echo "📥 Fetching results from cluster branch origin/$branch..."
    if git fetch origin "$branch" >/dev/null 2>&1; then
        # Check if local workspace is clean
        if [ -z "$(git status --porcelain)" ]; then
            echo "🧹 Local workspace is clean. Attempting a safe merge to bring back DVC lock and metrics history..."
            if git merge "origin/$branch" --no-edit >/dev/null 2>&1; then
                echo "✅ Successfully merged all DVC lock and metrics history into your current branch!"
                return 0
            else
                git merge --abort >/dev/null 2>&1
                echo "⚠️ Safe merge failed. Falling back to individual file extraction..."
            fi
        else
            echo "⚠️ Local workspace has uncommitted changes."
            echo "💡 Suggestion: To safely bring back the complete DVC stage commit history, run:"
            echo "   git stash && git merge origin/$branch && git stash pop"
            echo "👉 Extracting only the modified files to avoid touching your local uncommitted files..."
        fi
        
        # Fallback to checkout FETCH_HEAD .
        echo "📦 Synchronizing all files from the cluster commit..."
        if git checkout FETCH_HEAD . >/dev/null 2>&1; then
            echo "✅ Results successfully synchronized to your local workspace."
        else
            echo "⚠️ Failed to synchronize files from cluster commit."
        fi
    else
        echo "⚠️ Could not fetch results from origin/$branch."
    fi
}

shadow_run() {
    local background=$1
    check_gh_auth
    local user=$(get_current_user)
    BRANCH="cluster-draft/$user"

    # Register the cleanup trap to ensure the branch is deleted on exit
    trap cleanup EXIT

    echo "🏗️  Preparing shadow push for user: $user (including untracked files)"

    # Create a temporary index to include untracked files without affecting the current index
    local temp_index=$(mktemp)
    local commit_to_push=""

    # Use a subshell to keep the environment clean
    commit_to_push=$(
        export GIT_INDEX_FILE="$temp_index"
        git read-tree HEAD
        git add --all  # Includes tracked and untracked files
        local tree=$(git write-tree)
        git commit-tree "$tree" -p HEAD -m "Shadow commit for $user"
    )
    rm -f "$temp_index"

    if [ -z "$commit_to_push" ]; then
        echo "❌ Error: Failed to create shadow commit."
        exit 1
    fi

    # Get last run ID before push to prevent matching stale run
    local last_known_run_id
    last_known_run_id=$(gh run list --branch "$BRANCH" --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null || echo "")

    echo "🚀 Shadow pushing to origin/$BRANCH..."
    git push origin "$commit_to_push:refs/heads/$BRANCH" --force --quiet

    if [ "$background" = true ]; then
        echo "✅ Run submitted in background. You can watch it with: cluster-run list"
        # In background mode, we DON'T want the EXIT trap to delete the branch immediately
        # because the GHA is still running. The researcher will have to delete it later or
        # it will be overwritten by next run.
        # Actually, for background runs, we should probably just untrap.
        trap - EXIT
        return
    fi

    # 3. Find and watch the run
    echo "⏳ Waiting for GitHub Actions to trigger..."
    sleep 4
    for i in {1..15}; do
        RUN_ID=$(gh run list --branch "$BRANCH" --limit 1 --json databaseId,status -q '.[0] | select(.status != "completed") | .databaseId' 2>/dev/null || true)
        if [ -n "$RUN_ID" ] && [ "$RUN_ID" != "$last_known_run_id" ]; then
            break
        fi
        RUN_ID=""
        sleep 2
    done

    if [ -z "$RUN_ID" ]; then
        # Check if it already finished (very fast run?)
        RUN_ID=$(gh run list --branch "$BRANCH" --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null || true)
        if [ "$RUN_ID" == "$last_known_run_id" ]; then
            RUN_ID=""
        fi
    fi

    if [ -z "$RUN_ID" ]; then
        echo "❌ Error: Could not find the triggered workflow run."
        exit 1
    fi

    echo "📺 Streaming logs for run $RUN_ID (Ctrl+C to cancel)..."

    stream_logs "$RUN_ID" "$commit_to_push"

    # Check final status with robust retry to handle GitHub API latency
    local conclusion=""
    for i in {1..5}; do
        conclusion=$(gh run view "$RUN_ID" --json conclusion -q .conclusion < /dev/null 2>/dev/null || echo "")
        [ -n "$conclusion" ] && [ "$conclusion" != "null" ] && break
        sleep 1
    done

    if [ "$conclusion" == "success" ]; then
        echo "✅ Cluster-CI run completed successfully."
        fetch_results "$BRANCH"
    else
        echo "❌ Cluster-CI run finished with status: ${conclusion:-unknown}"
    fi

    # Final cleanup will be handled by the EXIT trap
}

# --- CLI Entry Point ---
check_dependencies

COMMAND=$1
case "$COMMAND" in
    list)
        gh run list --workflow "Cluster-CI Execution"
        ;;
    view)
        shift
        if [ -z "$1" ]; then
            # If no ID provided, try to find the last run for this user
            check_gh_auth
            USER=$(get_current_user)
            RUN_ID=$(gh run list --branch "cluster-draft/$USER" --limit 1 --json databaseId -q '.[0].databaseId')
            if [ -z "$RUN_ID" ]; then
                echo "Usage: cluster-run view <run_id>"
                exit 1
            fi
            gh run view "$RUN_ID" --log
        else
            gh run view "$@"
        fi
        ;;
    cancel)
        shift
        if [ -z "$1" ]; then
            check_gh_auth
            USER=$(get_current_user)
            BRANCH="cluster-draft/$USER"
            RUN_ID=$(gh run list --branch "$BRANCH" --limit 1 --json databaseId -q '.[0].databaseId')
             if [ -z "$RUN_ID" ]; then
                echo "Usage: cluster-run cancel <run_id>"
                exit 1
            fi
            echo "🛑 Cancelling run $RUN_ID..."
            gh run cancel "$RUN_ID"
            echo "🧹 Deleting branch $BRANCH..."
            git push origin --delete "$BRANCH" --quiet >/dev/null 2>&1 || true
        else
            gh run cancel "$@"
        fi
        ;;
    --help|-h)
        show_help
        ;;
    *)
        BG=false
        if [[ "$1" == "--background" || "$1" == "-b" ]]; then
            BG=true
        fi
        shadow_run "$BG"
        ;;
esac
