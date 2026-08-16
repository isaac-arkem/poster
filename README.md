# Jenkins — publish due Instagram content posts

Scheduled job that publishes SSE calendar entries to Instagram once their
scheduled date arrives, for aliases with linked Composio credentials.

It is a **clock, not a worker**. All publish logic lives in the ArkGPT app and
is shared with the manual **Publish to Instagram** button, so there is exactly
one code path:

`GET /api/sse/cron/publish-content-posts`

The app selects due rows (`status = scheduled`, `platform = instagram`,
`scheduled_at <= now`, `external_post_id is null`) and publishes them via each
alias's `sse_agent_accounts` Composio credentials — different accounts
concurrently, one at a time per account.

## Files

| File | Purpose |
|------|---------|
| `Jenkinsfile` | Pipeline definition — schedule, concurrency guard, exit-code mapping |
| `publish_due_instagram_posts.py` | Drains the route and reports. Stdlib only, no `pip install` |

## Setup — the whole list

### 1. One secret on Jenkins

*Manage Jenkins → Credentials → System → Global → Add Credentials*

| Field | Value |
|-------|-------|
| Kind | **Secret text** |
| ID | `CRON_SECRET` |
| Secret | the VALUE of `CRON_SECRET` in the app's env — what follows the `=` |

That is the **only** credential this job needs. The ID is hardcoded in the
Jenkinsfile, so it must match exactly.

### 2. Env vars on the app (not on Jenkins)

`.env.local` for local dev, or the hosting provider's env for a deployment:

| Variable | Required | Missing → |
|----------|----------|-----------|
| `CRON_SECRET` | yes | route returns `401` for everything (fail-closed) |
| `COMPOSIO_API_KEY` | yes | route returns `503` |
| `COMPOSIO_USER_ID` | no | falls back to a connected-account lookup |
| `NEXT_PUBLIC_SUPABASE_URL` | yes | already needed to run the app |
| `NEXT_PUBLIC_SUPABASE_PUBLIC_KEY` | yes | already needed to run the app |
| `SUPABASE_SECRET_KEY` | yes | already needed to run the app |

Env changes are not hot-reloaded — restart `next dev` after editing.

### 3. One line in the Jenkinsfile

`ARKGPT_BASE_URL` in the `environment {}` block:

- local dev — `http://localhost:3000`
- Jenkins in Docker — `http://host.docker.internal:3000`
- production — `https://app.arkem.io`

It is deliberately **not** a build parameter. The job sends `CRON_SECRET` to
this origin in a header, so anyone able to override it at build time could
point the job at a host they control and capture the secret.

### 4. The job

New Pipeline job → *Pipeline script from SCM* → Git:

| Field | Value |
|-------|-------|
| Repository URL | `https://github.com/isaac-arkem/poster.git` |
| Credentials | `- none -` (public repo) |
| Branch Specifier | `*/main` — Jenkins defaults to `*/master`, which fails |
| Script Path | `Jenkinsfile` |

Then **Build Now** once. The `cron('*/5 * * * *')` trigger only registers
after Jenkins has evaluated the Jenkinsfile, so the schedule does not start on
its own until that first manual build.

The agent needs `python3`. Nothing else — the script is stdlib-only.

## Build parameter

| Parameter | Default | Notes |
|-----------|---------|-------|
| `FAIL_ON_PUBLISH_ERROR` | `false` | Off → per-post failures mark the build UNSTABLE. On → they fail it |

Tuning knobs (`PUBLISH_DRAIN_ROUNDS`, `PUBLISH_ROUND_PAUSE_SEC`,
`PUBLISH_TIMEOUT_SEC`) live in the `environment {}` block with working
defaults; you should not need to touch them.

## Build outcomes

| Script exit | Build | Meaning |
|-------------|-------|---------|
| `0` | SUCCESS | Nothing due, or everything published |
| `2` | UNSTABLE | Some posts failed; they stay `scheduled` and retry later |
| other | FAILURE | Unreachable, bad secret, publishing not configured |

`FAIL_ON_PUBLISH_ERROR=true` promotes exit `2` to FAILURE.

## Safety properties

- **No double-posting.** A row is marked `posted` with its Instagram media id
  only after Instagram returns one, and `external_post_id is null` is part of
  the due filter. `disableConcurrentBuilds()` additionally stops two sweeps
  from racing on the same row before either stamps the id.
- **No runaway drain.** If a round publishes nothing, the loop stops instead
  of re-requesting the same failing rows for the rest of the build.
- **No permanent red.** Per-post failures are UNSTABLE by default, so one
  broken alias does not red every build.
- **Secret hygiene.** `set +x`; the secret is read from the environment, never
  a command line; redirects are refused so it is not replayed on another host;
  a non-https base URL is rejected (localhost excepted); and the target origin
  is not overridable at build time.

## What the cron expression controls

Not posting time — `sse_content_posts.scheduled_at` carries that per post. The
cron only decides **how late a post can be**: at `*/5` a post scheduled for
10:00 publishes by 10:05 at the worst.

Raise the frequency for tighter timing, lower it to reduce noise. A tick with
nothing due is a single cheap query, and the route is idempotent, so frequency
is close to free.

## Drain rounds and the build timeout

`PUBLISH_DRAIN_ROUNDS` is bounded by the pipeline's `timeout`, not by appetite.
A round can use the route's full 300s, so

    rounds x (300 + PUBLISH_ROUND_PAUSE_SEC) < build timeout

must hold. At 4 rounds and a 30-minute timeout there is comfortable margin.
Getting this wrong is the one genuinely dangerous misconfiguration here: a
build killed mid-round can leave a post live on Instagram with nothing recorded
against it, and the next sweep would publish it again.

## Manual run

```bash
export ARKGPT_BASE_URL=https://app.arkem.io
export CRON_SECRET=...
python3 publish_due_instagram_posts.py
```

Or hit the route directly:

```bash
curl -sS -H "x-cron-secret: $CRON_SECRET" "$ARKGPT_BASE_URL/api/sse/cron/publish-content-posts" | jq
```

Response:

```json
{ "as_of": "2026-08-16T07:14:02.309Z", "checked": 2, "published": 1, "failed": 1,
  "contended": 0, "deferred": 0 }
```

`deferred` means work was left for the next round or build; `contended` means
another publisher held the row.

The route reports counts only, so a build that goes UNSTABLE says *how many*
posts failed but not which ones or why. The reason is persisted per row in
`sse_content_posts.publish_error` and surfaces in the calendar UI — check the
failed-posts panel there when a build is yellow.
