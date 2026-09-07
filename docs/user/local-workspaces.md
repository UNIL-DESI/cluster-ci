# Local workspaces

Normal jobs keep their existing `repositories/<owner>/<repo>` workspace, cache,
Docker volume names, and dashboard access. Local jobs use
`repositories/_local/<owner>/<repo>` and a separate home volume. Local jobs of the
same repository share that workspace on each worker; this is not a per-run
snapshot or isolation between trusted cluster members.

Local workspace files, dataset inputs, result contents, and local viewers require
the existing `CLUSTER_TOKEN`. The CLI already supplies that token for result
transfers. Dashboard status, timing, terminal output, file names and sizes remain
accessible as before. A signed-in dashboard user can unlock local files at
`/local-access`; the session cookie stores a signed proof, not the shared token.
Token rotation invalidates that proof. Normal-job files require no new login or
token entry. Existing public job credential fields are unchanged.

Local viewers bind to worker loopback and are reached through the authenticated
worker proxy. Normal viewer addresses are unchanged. This does not restrict
trusted job code, administrator access, custom Docker network overrides, or
other lab members who possess the shared token. Use the existing trusted network
or HTTPS for token transport.

## Cleanup

Both modes use the existing `repositories/registry.json` and oldest-idle-first
cleanup order. The local registry key is `_local/<owner>/<repo>`. The default
maintenance threshold is 100 GB free (configurable with
`GC_FREE_SPACE_THRESHOLD_GB`); emergency eviction starts below 50 GB free.
Local workspaces are evicted without `dvc push`, even if their configuration
contains a remote. Pending-sync draining skips local workspace keys.
Local jobs receive no additional retention guarantee.

Headnode transfer/result packages retain their existing 24-hour expiry. They are
separate from worker workspaces. Download results promptly; historical local run
identifiers do not turn the shared workspace into historical snapshots.

## Existing workspaces

Deployment does not automatically classify or move existing directories. Before
resuming local jobs, inventory the affected workers and migrate directories that
contain local data while their jobs and cleanup are stopped. Preserve any incident
evidence separately. Move each approved directory into `_local/<owner>/<repo>`
and move its registry entry to the matching key without resetting its age.
Do not merge with an existing destination or leave a public symlink to it.
A mixed normal/local directory must be treated as containing local data; the next
normal job can recreate the original normal path using a clean clone.

Review active containers, bind mounts, absolute cache/worktree links and volume
contents before migration. Existing containers and viewers must be restarted with
the new layout, and old public copies must not remain accessible. Deploy the
headnode, workers and runner together using the administrator's normal deployment
procedure. Do not use this patch alone as proof that an old directory is protected.
