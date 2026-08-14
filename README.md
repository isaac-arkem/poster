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

## Setup

1. **Put this folder in SCM.** The `Jenkinsfile` is consumed via *Pipeline
   script from SCM*, which needs a git remote. A local folder on the agent
   will not work.
2. **Credential** — add a Jenkins *Secret text* credential holding the same
   value as `CRON_SECRET` on the target deployment. Default ID
   `arkgpt-cron-secret`. Use a distinct credential and job per environment.
3. **Deployment env** — `CRON_SECRET` and `COMPOSIO_API_KEY` must be set on
   the deployment. Missing secret → `401`, missing Composio key → `503`. The
   job fails loudly in both cases rather than reporting a quiet success.
4. **Job** — new Pipeline job → *Pipeline script from SCM* → Script Path
   `Jenkinsfile`. The `triggers { cron(...) }` block supplies the schedule
   after the first manual build.
5. **Agent** — needs `python3`. Nothing else.

## Parameters

| Parameter | Default | Notes |
|-----------|---------|-------|
| `ARKGPT_BASE_URL` | `https://app.arkem.io` | Target origin, no trailing slash |
| `CRON_SECRET_CREDENTIAL_ID` | `arkgpt-cron-secret` | Secret-text credential ID |
| `PUBLISH_DRAIN_ROUNDS` | `10` | 5 posts/round, so 50 posts per build; the rest rolls over |
| `PUBLISH_ROUND_PAUSE_SEC` | `5` | Pause between rounds |
| `PUBLISH_TIMEOUT_SEC` | `330` | Must exceed the route's 300s `maxDuration` |
| `FAIL_ON_PUBLISH_ERROR` | `false` | Off → per-post failures mark the build UNSTABLE. On → they fail it |

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
  and a non-https base URL is rejected.

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
