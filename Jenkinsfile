// Jenkins pipeline: publish due Instagram content posts.
//
// This job is a CLOCK, not a worker. All publishing logic (alias ->
// sse_agent_accounts Composio credentials -> sign media -> create container ->
// publish -> mark posted) lives in the ArkGPT app and is shared with the manual
// "Publish to Instagram" button, so there is exactly one code path. Jenkins
// only calls GET /api/sse/cron/publish-content-posts on a schedule.
//
// ---------------------------------------------------------------------------
// TO CONFIGURE THIS JOB YOU NEED EXACTLY TWO THINGS:
//
//   1. Edit ARKGPT_BASE_URL below to point at the app you want swept.
//   2. Create ONE Jenkins credential:
//        Kind:   "Secret text"
//        ID:     CRON_SECRET          <- the name this file looks up
//        Secret: the VALUE of CRON_SECRET in the app's environment
//                (i.e. whatever follows the "=" in .env)
//
// That's it. Everything else has a working default.
// ---------------------------------------------------------------------------
//
// Job setup -> Pipeline -> "Pipeline script from SCM" -> Git
//   Repository URL:   https://github.com/isaac-arkem/poster.git
//   Branch Specifier: */main        (Jenkins defaults to */master — change it)
//   Script Path:      Jenkinsfile
//
// The agent needs python3. The script is stdlib-only, no pip install.
//
// What "due" means: sse_content_posts.scheduled_at is a timestamp, so a post
// carries its own time of day. The cron expression below no longer decides
// WHEN a post goes out — it decides how LATE it can be. At */5 a post
// scheduled for 10:00 publishes by 10:05 at the worst.

pipeline {
  // Any executor. Do NOT pin this to `label 'built-in'` — a default Jenkins
  // controller carries no labels at all, so that expression matches nothing
  // and the build sits forever on "Still waiting to schedule task".
  //
  // If a build does hang there, the reason is in Manage Jenkins -> Script
  // Console: `Jenkins.instance.queue.items.each { println it.why }`.
  // Usual causes: the built-in node has 0 executors (Manage Jenkins -> Nodes
  // -> Built-In Node -> Configure), or an earlier build is wedged and
  // disableConcurrentBuilds() below is holding this one behind it.
  agent any

  environment {
    // ---- EDIT THIS ----------------------------------------------------
    // Local dev:            http://localhost:3000
    // Jenkins in Docker:    http://host.docker.internal:3000
    // Production:           https://app.arkem.io
    //
    // Deliberately NOT a build parameter: the job sends CRON_SECRET to this
    // origin in a request header, so anyone able to override it at build time
    // could point the job at a host they control and capture the secret.
    ARKGPT_BASE_URL = 'http://localhost:3000'
    // -------------------------------------------------------------------

    // The route publishes across accounts concurrently, so a round clears
    // roughly 24 posts rather than 5. Four rounds is ~96 per build, which is
    // far more than a tick ever has waiting.
    //
    // The cap is bounded by the build timeout below, NOT by appetite: a round
    // can use the route's full 280s, so rounds x (280 + pause) must stay under
    // it. Being killed mid-round is the one genuinely dangerous outcome — the
    // post can be live on Instagram with nothing recorded against it.
    PUBLISH_DRAIN_ROUNDS = '4'
    // Pause between rounds so a backlog does not hammer the Graph API.
    PUBLISH_ROUND_PAUSE_SEC = '5'
    // Must exceed the route's 300s maxDuration, so a slow tick surfaces as a
    // server error rather than an ambiguous client timeout.
    PUBLISH_TIMEOUT_SEC = '330'
    // The Jenkinsfile maps the exit code to a build result below, so the
    // script itself should never hard-fail on a per-post failure.
    PUBLISH_FAIL_ON_PARTIAL = '0'
  }

  options {
    timestamps()
    // Two overlapping sweeps could both select the same row before either
    // stamps external_post_id, which would double-post to Instagram.
    disableConcurrentBuilds()
    // Set here, not in the job UI — this block rewrites the UI value on every
    // build, so editing it there silently reverts.
    //
    // At */5 this keeps roughly the last 25 minutes of runs. That is fine as a
    // record: the reason a post failed lives on the row in publish_error and
    // in the calendar's failed-posts panel, so the build log is a convenience
    // rather than the source of truth.
    buildDiscarder(logRotator(numToKeepStr: '5'))
    // Must exceed PUBLISH_DRAIN_ROUNDS x (route maxDuration + pause) with room
    // to spare — 4 x 285s is ~19 minutes, so 30 leaves margin for a slow
    // checkout. A build killed mid-publish is the worst case in this pipeline.
    timeout(time: 30, unit: 'MINUTES')
  }

  triggers {
    // Every 5 minutes: this now sets how late a post can be against its own
    // scheduled_at, not when it publishes. A tick with nothing due is one cheap
    // query, and the route is idempotent (external_post_id gates
    // re-publishing), so frequency is close to free.
    //
    // Fixed */5 rather than H/5 — H would offset the minute, and there is no
    // fleet of jobs here to spread the load of.
    cron('*/5 * * * *')
  }

  parameters {
    booleanParam(
      name: 'FAIL_ON_PUBLISH_ERROR',
      defaultValue: false,
      description: 'Off: per-post failures mark the build UNSTABLE (they are recorded on the row and retried on a later build). On: they fail the build, e.g. if you page on this job.'
    )
  }

  stages {
    stage('Preflight') {
      steps {
        sh '''
          set -eu

          command -v python3 >/dev/null 2>&1 || {
            echo "python3 is not installed on this agent." >&2
            exit 1
          }
          python3 --version
          python3 -m py_compile publish_due_instagram_posts.py

          echo "target: $ARKGPT_BASE_URL/api/sse/cron/publish-content-posts"
        '''
      }
    }

    stage('Publish due posts') {
      steps {
        script {
          def status = 1
          withCredentials([
            string(credentialsId: 'CRON_SECRET', variable: 'CRON_SECRET')
          ]) {
            status = sh(
              returnStatus: true,
              // Jenkins runs sh with -x; the cron secret would otherwise be
              // echoed into the build log. The script reads it from the env,
              // so it never appears on a command line either.
              script: '''
                set +x
                python3 publish_due_instagram_posts.py
              '''
            )
          }

          // 0 = clean, 2 = some posts failed, anything else = hard failure.
          if (status == 0) {
            echo 'Sweep clean.'
          } else if (status == 2) {
            if (params.FAIL_ON_PUBLISH_ERROR) {
              error('Post(s) failed to publish — check sse_content_posts.publish_error / the calendar UI.')
            }
            currentBuild.description = 'publish failures — check the calendar UI'
            unstable('Post(s) failed to publish; they stay scheduled and are retried on a later build.')
          } else {
            error("Publish sweep failed (exit ${status}) against ${env.ARKGPT_BASE_URL}.")
          }
        }
      }
    }
  }

  post {
    failure {
      echo 'Nothing is double-published: a post is only marked posted after Instagram returns a media id.'
    }
  }
}
