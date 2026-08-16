# Jenkins — publish due Instagram content posts

Scheduled job that publishes SSE calendar entries to Instagram once their
scheduled date arrives, for aliases with linked Composio credentials.

It is a **clock, not a worker**. All publish logic lives in the ArkGPT app and
is shared with the manual **Publish to Instagram** button, so there is exactly
one code path:

`GET /api/sse/cron/publish-content-posts`

The app selects due rows (`status = scheduled`, `platform = instagram`,
`scheduled_date <= UTC today`, `external_post_id is null`) and publishes up to
5 per tick via each alias's `sse_agent_accounts` Composio credentials.

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
| ID | `arkgpt-cron-secret` |
| Secret | the same string as `CRON_SECRET` in the app's environment |

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

Then **Build Now** once. The `cron('H/15 * * * *')` trigger only registers
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
  broken alias does not red every build on a 15-minute schedule.
- **Secret hygiene.** `set +x`; the secret is read from the environment, never
  a command line; redirects are refused so it is not replayed on another host;
  a non-https base URL is rejected (localhost excepted); and the target origin
  is not overridable at build time.

## Known gap: time of day

`sse_content_posts.scheduled_date` is a `date`, not a timestamp. A post is due
at 00:00 UTC on its day, so **the cron expression in the Jenkinsfile is what
actually decides posting time** — with `H/15 * * * *` everything scheduled for
today goes out on the first tick after midnight UTC.

Options, in increasing order of work:

1. Change the cron to your posting window, e.g. `H 9-21/3 * * *`.
2. Add `scheduled_at timestamptz` to `sse_content_posts`, populate it from the
   calendar slot's `source_meta.calendar_slot.time_label`, and switch
   `loadDueInstagramContentPosts` to `scheduled_at <= now()`. This is the real
   fix and the only one that gives per-post times.

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
{ "as_of": "2026-08-14", "checked": 2, "published": 1, "failed": 1 }
```

The route reports counts only, so a build that goes UNSTABLE says *how many*
posts failed but not which ones or why. The reason is persisted per row in
`sse_content_posts.publish_error` and surfaces in the calendar UI — check
there when a build is yellow.
