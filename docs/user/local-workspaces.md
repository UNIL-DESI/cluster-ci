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

Hash-based peer downloads (`/fetch_cas/<hash>`) search public caches only, even
when the caller supplies a token. Local consumers explicitly request `local=1`
and supply `Authorization: Bearer <CLUSTER_TOKEN>` to include protected caches.
A hash is an integrity check, not an access credential. The runner client sends
the token only in local mode and does not follow download redirects.
Parallel `next_node` and `runner_heartbeat` control APIs also require the token;
they reject requests when the server token is unconfigured. Dashboard metadata
and console logs retain their existing access rules.

Artifact discovery, dependency recovery and cache-affinity lookup exclude local
producers for ordinary jobs. An artifact whose producing job is unknown is not
assumed public. Local jobs may reuse both public and protected artifacts because
their outputs remain local. Parallel local jobs use separate home, pip and uv
volumes; normal volume names remain unchanged.

Before deploying this separation, inventory old parallel home/package volumes
that were shared between modes. A newly separated name does not sanitize an old
volume or an already contaminated public workspace. Preserve and quarantine any
such data through the existing administrator procedure; this patch does not
delete or migrate volumes automatically.

The runner owns `IS_LOCAL`, `CLUSTER_CI_MODE`, `JOB_ID`, `HEADNODE_URL`,
`CLUSTER_TOKEN` and the node attempt counter. Custom stage variables/secret files
cannot override those values. A local pipeline refuses external log delegation.
The maintained CLI remembers local mode before submission work can fail, and
ambiguous old recovery state is treated conservatively. `--local` supports
submission and `attach`; GitHub-only subcommands are rejected with that flag.
The legacy shell launcher rejects `--local` rather than starting a shadow push.

Local submission, workers, stage containers and viewers set `DVC_NO_ANALYTICS=1`
before invoking DVC. This suppresses DVC's external telemetry; it does not change
dashboard metrics or the network permissions of trusted job code.

Cluster API clients refuse HTTP redirects, including result uploads, peer
downloads and authenticated control requests. Configure the canonical headnode
URL directly; a redirect is an error, not permission to forward credentials or
job content to another server. Standalone `cluster-run` installations use the
same no-redirect policy.

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
