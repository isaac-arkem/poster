#!/usr/bin/env python3
"""Jenkins job: publish due Instagram content posts from the SSE calendar.

Triggers the ArkGPT cron route that:
  - finds ``sse_content_posts`` with status=scheduled, platform=instagram,
    scheduled_date <= UTC today, and no external_post_id
  - publishes each via the linked ``sse_agent_accounts`` Composio credentials
  - processes up to 5 posts per tick

This script does **not** talk to Supabase or Composio directly — the app owns
due selection + publish. Jenkins only schedules the HTTP call.

Required env:
  ARKGPT_BASE_URL   e.g. https://arkgpt.example.com  (no trailing slash)
  CRON_SECRET       same value as the app's CRON_SECRET

Optional env:
  VERCEL_PROTECTION_BYPASS  Vercel "Protection Bypass for Automation" secret.
                            Required when ARKGPT_BASE_URL is a PREVIEW
                            deployment (e.g. the develop branch), because
                            Vercel gates those behind Deployment Protection and
                            would answer with its own auth page long before the
                            request reaches the route. Not needed for
                            production.
  PUBLISH_DRAIN_ROUNDS     re-call until nothing is left due (default 1)
  PUBLISH_ROUND_PAUSE_SEC  pause between rounds (default 5)
  PUBLISH_TIMEOUT_SEC      HTTP timeout per request (default 330 — must exceed
                           the route's 300s maxDuration so a slow tick surfaces
                           as a server error, not an ambiguous client timeout)
  PUBLISH_FAIL_ON_PARTIAL  "1" -> exit 1 when any post failed; default "0"
                           exits 2, which the Jenkinsfile maps to UNSTABLE

Exit codes:
  0  nothing due, or everything published
  1  hard failure — unreachable, bad secret, publishing not configured
  2  some posts failed to publish (they stay scheduled and are retried later)

Example (local):
  export ARKGPT_BASE_URL=http://localhost:3000
  export CRON_SECRET=dev-secret
  python3 publish_due_instagram_posts.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any


ENDPOINT_PATH = "/api/sse/cron/publish-content-posts"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_PARTIAL = 2


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects.

    The request carries the cron secret in its headers, and urllib would
    replay them on the redirect target. A redirect on this endpoint is a
    misconfigured base URL (usually http -> https), so fail instead.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise SystemExit(
            f"refusing to follow HTTP {code} redirect to {newurl!r} — "
            f"set ARKGPT_BASE_URL to the final origin so the cron secret is "
            f"not replayed on another host"
        )


_opener = urllib.request.build_opener(NoRedirect)


def env_str(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or not str(value).strip():
        raise SystemExit(f"missing required env: {name}")
    return str(value).strip()


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as e:
        raise SystemExit(f"invalid int for {name}: {raw!r}") from e


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def trigger_publish(
    base_url: str,
    secret: str,
    timeout_sec: int,
    bypass: str | None = None,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}{ENDPOINT_PATH}"
    headers = {
        "Accept": "application/json",
        "x-cron-secret": secret,
        # Also accepted by the route (Vercel-style).
        "Authorization": f"Bearer {secret}",
    }
    if bypass:
        # Gets us past Vercel Deployment Protection on a preview deployment.
        # Without it Vercel answers with its own auth challenge and the route
        # never runs — which looks like a broken cron rather than a gate.
        headers["x-vercel-protection-bypass"] = bypass
        headers["x-vercel-set-bypass-cookie"] = "false"
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with _opener.open(req, timeout=timeout_sec) as res:
            body = res.read().decode("utf-8", errors="replace")
            status = res.status
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if e.fp else ""
        hint = ""
        if e.code == 401:
            # The route always answers JSON. An HTML 401 came from the platform
            # in front of it — on Vercel that is Deployment Protection, which
            # looks identical to a bad secret unless you read the body.
            if "<html" in detail[:400].lower():
                hint = (
                    "\nhint: this 401 is an HTML page, so it came from the hosting "
                    "platform rather than the route — on a Vercel preview "
                    "deployment set VERCEL_PROTECTION_BYPASS."
                )
            else:
                hint = "\nhint: CRON_SECRET does not match the value set on the deployment."
        elif e.code == 503:
            hint = "\nhint: COMPOSIO_API_KEY is not configured on the deployment."
        raise SystemExit(
            f"publish cron HTTP {e.code} for {url}\n{detail[:800]}{hint}"
        ) from e
    except urllib.error.URLError as e:
        raise SystemExit(f"publish cron request failed for {url}: {e}") from e

    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError as e:
        raise SystemExit(
            f"publish cron returned non-JSON (HTTP {status}): {body[:400]}"
        ) from e

    if not isinstance(payload, dict):
        raise SystemExit(f"publish cron unexpected payload: {payload!r}")
    return payload


def main() -> int:
    base_url = env_str("ARKGPT_BASE_URL")
    secret = env_str("CRON_SECRET")
    bypass = os.environ.get("VERCEL_PROTECTION_BYPASS", "").strip() or None
    rounds = max(1, env_int("PUBLISH_DRAIN_ROUNDS", 1))
    pause_sec = max(0, env_int("PUBLISH_ROUND_PAUSE_SEC", 5))
    timeout_sec = max(30, env_int("PUBLISH_TIMEOUT_SEC", 330))
    fail_on_partial = env_bool("PUBLISH_FAIL_ON_PARTIAL", False)

    if not base_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        raise SystemExit(
            f"ARKGPT_BASE_URL must be https:// (or a localhost origin for "
            f"testing) so the cron secret is not sent in the clear: {base_url}"
        )

    total_checked = 0
    total_published = 0
    total_failed = 0
    last_as_of: str | None = None
    rounds_ran = 0

    for round_idx in range(1, rounds + 1):
        rounds_ran = round_idx
        print(f"[publish-due] round {round_idx}/{rounds} -> {base_url}{ENDPOINT_PATH}")
        result = trigger_publish(base_url, secret, timeout_sec, bypass)
        as_of = str(result.get("as_of") or "")
        checked = int(result.get("checked") or 0)
        published = int(result.get("published") or 0)
        failed = int(result.get("failed") or 0)

        last_as_of = as_of or last_as_of
        total_checked += checked
        total_published += published
        total_failed += failed

        print(f"  as_of={as_of} checked={checked} published={published} failed={failed}")

        # No more due posts this UTC day (or batch empty).
        if checked == 0:
            print("[publish-due] nothing else is due — drain complete")
            break

        # No progress: a failed post keeps status='scheduled', so another round
        # would hand back the same rows and fail them again. Stop rather than
        # burn the remaining rounds on posts that just failed.
        if published == 0:
            print(
                "[publish-due] no post published this round — stopping the drain; "
                "these posts are retried on the next build"
            )
            break

        if round_idx < rounds:
            time.sleep(pause_sec)
    else:
        if total_checked > 0:
            print(
                f"[publish-due] hit PUBLISH_DRAIN_ROUNDS={rounds} with work possibly "
                f"still due — the remainder rolls over to the next build"
            )

    summary = {
        "as_of": last_as_of,
        "rounds_ran": rounds_ran,
        "checked": total_checked,
        "published": total_published,
        "failed": total_failed,
    }
    print("[publish-due] summary")
    print(json.dumps(summary, indent=2))

    if total_failed > 0:
        print(
            f"[publish-due] {total_failed} post(s) failed to publish; they stay "
            f"scheduled and are retried on a later build",
            file=sys.stderr,
        )
        return EXIT_ERROR if fail_on_partial else EXIT_PARTIAL

    print("[publish-due] OK")
    return EXIT_OK


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        raise SystemExit(130)
