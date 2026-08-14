// Jenkins pipeline: publish due Instagram content posts.
//
// This job is a CLOCK, not a worker. All publishing logic (alias ->
// sse_agent_accounts Composio credentials -> sign media -> create container ->
// publish -> mark posted) lives in the ArkGPT app and is shared with the manual
// "Publish to Instagram" button, so there is exactly one code path. Jenkins
// only calls GET /api/sse/cron/publish-content-posts on a schedule.
//
// What "due" means today: sse_content_posts.scheduled_date is a DATE column,
// so a post becomes due at 00:00 UTC on its scheduled day. The cron expression
// below is therefore what actually decides the time of day posts go out.
// Per-post publish times need a scheduled_at timestamptz column first.
//
// Job setup -> Pipeline -> "Pipeline script from SCM" -> Script Path: Jenkinsfile
//
// One-time prerequisites:
//   1. A Jenkins "Secret text" credential holding CRON_SECRET, matching the
//      value set on the target deployment. Default ID: arkgpt-cron-secret.
//      Use a separate credential (and job) per environment.
//   2. python3 on the agent. The script is stdlib-only — no pip install.
//   3. CRON_SECRET and COMPOSIO_API_KEY set on the deployment. Without them
//      the route returns 401 / 503 and this job fails loudly rather than
//      reporting a quiet success.

pipeline {
  agent {
    label 'Dobby'
  }

  options {
    timestamps()
    // Two overlapping sweeps could both select the same row before either
    // stamps external_post_id, which would double-post to Instagram.
    disableConcurrentBuilds()
    buildDiscarder(logRotator(numToKeepStr: '100'))
    timeout(time: 20, unit: 'MINUTES')
  }

  triggers {
    // Every 15 minutes. H spreads the minute across the hour so a fleet of
    // Jenkins jobs does not all fire on :00. Tighten or widen freely — the
    // route is idempotent (external_post_id gates re-publishing) and a tick
    // with nothing due is a single cheap request.
    cron('H/15 * * * *')
  }

  parameters {
    string(
      name: 'ARKGPT_BASE_URL',
      defaultValue: 'https://app.arkem.io',
      description: 'Origin of the deployment to sweep, no trailing slash. Point a second job at staging.'
    )
    string(
      name: 'CRON_SECRET_CREDENTIAL_ID',
      defaultValue: 'arkgpt-cron-secret',
      description: 'Jenkins "Secret text" credential holding CRON_SECRET for the target deployment.'
    )
    string(
      name: 'PUBLISH_DRAIN_ROUNDS',
      defaultValue: '10',
      description: 'Safety cap on drain rounds. The route publishes at most 5 posts per call, so 10 rounds clears 50 posts per build; the rest rolls to the next build.'
    )
    string(
      name: 'PUBLISH_ROUND_PAUSE_SEC',
      defaultValue: '5',
      description: 'Pause between rounds so a backlog does not hammer Composio and the Instagram Graph API.'
    )
    string(
      name: 'PUBLISH_TIMEOUT_SEC',
      defaultValue: '330',
      description: 'HTTP timeout per request. Must exceed the route maxDuration of 300s.'
    )
    booleanParam(
      name: 'FAIL_ON_PUBLISH_ERROR',
      defaultValue: false,
      description: 'Off: per-post failures mark the build UNSTABLE (they are recorded on the row and retried after the route backoff). On: they fail the build, e.g. if you page on this job.'
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

          case "$ARKGPT_BASE_URL" in
            https://*|http://localhost*|http://127.0.0.1*) ;;
            *)
              echo "ARKGPT_BASE_URL must be https:// (or a localhost origin for testing): $ARKGPT_BASE_URL" >&2
              exit 1
              ;;
          esac

          for n in "$PUBLISH_DRAIN_ROUNDS" "$PUBLISH_ROUND_PAUSE_SEC" "$PUBLISH_TIMEOUT_SEC"; do
            case "$n" in
              ''|*[!0-9]*) echo "drain rounds / pause / timeout must be integers (got '$n')." >&2; exit 1 ;;
            esac
          done

          [ "$PUBLISH_TIMEOUT_SEC" -gt 300 ] || {
            echo "PUBLISH_TIMEOUT_SEC must exceed the route's 300s maxDuration." >&2
            exit 1
          }

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
            string(credentialsId: params.CRON_SECRET_CREDENTIAL_ID, variable: 'CRON_SECRET')
          ]) {
            status = sh(
              returnStatus: true,
              // Jenkins runs sh with -x; the cron secret would otherwise be
              // echoed into the build log. The script reads it from the env,
              // so it never appears on a command line either.
              script: '''
                set +x
                export PUBLISH_FAIL_ON_PARTIAL=0
                python3 publish_due_instagram_posts.py
              '''
            )
          }

          // 0 = clean, 2 = some posts failed, anything else = hard failure.
          if (status == 0) {
            echo 'Sweep clean.'
          } else if (status == 2) {
            if (params.FAIL_ON_PUBLISH_ERROR) {
              error('Post(s) failed to publish — see the per-post detail above.')
            }
            currentBuild.description = 'publish failures — see log'
            unstable('Post(s) failed to publish; they stay scheduled and are retried after the route backoff.')
          } else {
            error("Publish sweep failed (exit ${status}) against ${params.ARKGPT_BASE_URL}.")
          }
        }
      }
    }
  }

  post {
    failure {
      echo "Nothing is double-published: a post is only marked posted after Instagram returns a media id."
    }
  }
}
