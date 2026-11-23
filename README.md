# Cursor PR review

A standalone, single-pass reviewer for GitHub pull requests, GitLab merge requests and local branches. It builds on [PR-Agent](https://github.com/the-pr-agent/pr-agent). The PR-Agent contributors provide the review prompt, line-numbered diff formatter and token estimator used here. We ship their [MIT-licensed](https://github.com/the-pr-agent/pr-agent/blob/main/LICENSE) Python package in the container, with its original license notice. Cursor Agent performs inference. Assay’s installed `deskread` and `deskpost` handle authenticated forge access when you use a change number. Local and CI runs require only the Git objects.

## Request a review in a PR or MR

Post a new comment containing just `/review` on an open GitHub PR or GitLab MR. The service polls the configured projects every two minutes by default. It accepts new requests from the numeric human account IDs you configure and posts the result on the reviewed head. A poll uses zero model tokens. Requests run one at a time with the existing prompt budget and cache.

Assay’s `deskmonitor pr` supplies the open change numbers. Batched `deskread comments --change` calls read their conversations on every cycle, including changes with no new commits. The poller skips old commands, edits detectable from the forge timestamps, bot responses and ordinary messages. For GitLab, Assay v1.0.91 omits the internal-note flag. Only an otherwise eligible command gets a single [GitLab note lookup](https://docs.gitlab.com/api/notes/#retrieve-a-merge-request-note), using the existing Assay reviewer credential and instance. Internal and system notes cannot start a review.

Each project starts watching at its first activation. Keep the state volume across restarts to pick up requests posted during downtime on still-open changes. Queued reviews survive a restart. A review interrupted after starting needs a fresh `/review` to run again. Duplicate reads do not incur another model call. A deleted comment or a change closed between polls may never be observed.

### Docker setup

The image contains Cursor Agent, Assay’s released forge tools, ShellCheck and actionlint. Use an **amd64 Linux** runner or Docker’s amd64 emulation. To build from source:

```sh
docker build --platform linux/amd64 -t assay-pr-agent .
```

### Interactive setup

Run the setup command once with a writable state volume and your checkouts mounted at the paths the reviewer will use:

```sh
docker run --rm -it --platform linux/amd64 \
  -v review-state:/state -v /path/to/checkouts:/repos:ro assay-pr-agent setup
```

Enter the projects, checkout paths inside the container, visibility, numeric IDs allowed to request reviews, and the responsible operator’s `login:numeric-id`. Setup asks for a Cursor API key and only the credentials for the forges you selected. For GitHub, have the App ID, installation ID and private PEM ready. For GitLab, have the reviewer token ready. You can accept the defaults of 120 seconds between polls, 6,000 estimated input tokens per review and Cursor’s default model.

For each credential, choose **paste** to enter it with terminal echo disabled, **import** to copy a readable file into the volume, or **mount** to keep using an external file. For import or mount, add the source volume to the setup command (for example, `-v /path/to/secrets:/run/secrets:ro`) and enter the file’s absolute path **inside** the container. With **mount**, provide the same file path when running the service. With **import**, the source mount is only needed during setup.

Start the reviewer with the same volumes. No configuration environment variables are required:

```sh
docker run --rm --platform linux/amd64 \
  -v review-state:/state -v /path/to/checkouts:/repos:ro assay-pr-agent
```

The active settings and imported or pasted credentials are in `/state/setup/`. Credential files are plaintext, owner-readable and writable (0600), inside a private directory (0700). Protect the volume and its backups. Setup checks the local files and credential formats offline. The poller checks forge access during normal operation.

To change settings or rotate credentials, run the setup command again, or run `docker exec -it <container> cursor-review-setup`. Existing settings and credentials can be kept without displaying their values. Restart the reviewer to load the new setup. Cancelling before the save keeps the previous configuration active. An explicit environment setting takes precedence over its saved value.

### Standalone Docker credentials

Mount the checkout for each project read-only and a writable volume at `/state` for the request queue, cache and Assay audit records. The container runs as UID 10001. It generates the reviewer configuration on first start and updates it from the supplied settings on restart. You can use it without installing Assay on the host.

| Docker setting | What to supply |
|---|---|
| `CURSOR_API_KEY` | Cursor API key for headless inference. |
| `REVIEW_OPERATOR` | The responsible human’s forge `login:numeric-id`. Assay uses this for posting attribution. The project’s `requester_ids` separately control who can request reviews. |
| `REVIEW_GITHUB_APP_ID`, `REVIEW_GITHUB_INSTALLATION_ID` | Numeric IDs for the GitHub App and the installation covering your repositories. Use a reviewer installation with access to read PRs and diffs and write PR conversation comments. |
| `REVIEW_GITHUB_PRIVATE_KEY` **or** `REVIEW_GITHUB_PRIVATE_KEY_FILE` | The App’s unencrypted RSA PEM or an absolute path to the mounted PEM. The value accepts actual line breaks or literal `\n`. |
| `REVIEW_GITLAB_TOKEN` **or** `REVIEW_GITLAB_TOKEN_FILE` | A dedicated GitLab reviewer token with API permission to read MRs and post notes, or an absolute path to the mounted token. |
| `REVIEW_PROJECTS` | JSON project list. Each entry has a forge, full HTTPS URL, checkout path, allowed human account IDs and optional `visibility` (`private` by default, `internal`, or `public`). Set the actual visibility to authorize posting on public or internal projects. |

Supply only the providers you use. Each container supports a GitHub App installation on github.com and one GitLab instance. The poller derives `GITLAB_API_BASE` from the configured GitLab projects. An explicit value must match their HTTPS instance and end in `/api/v4`.

For example, this `review.env` uses both providers and reads the keys from a mounted secrets directory:

```dotenv
REVIEW_OPERATOR=alice:101
REVIEW_GITHUB_APP_ID=123456
REVIEW_GITHUB_INSTALLATION_ID=789012
REVIEW_GITHUB_PRIVATE_KEY_FILE=/run/secrets/github-reviewer.pem
REVIEW_GITLAB_TOKEN_FILE=/run/secrets/gitlab-reviewer-token
REVIEW_PROJECTS=[{"provider":"github","url":"https://github.com/example/api","checkout":"/repos/api","requester_ids":[101],"visibility":"public"},{"provider":"gitlab","url":"https://gitlab.example.com/example/web","checkout":"/repos/web","requester_ids":[202],"visibility":"private"}]
REVIEW_POLL_SECONDS=120
REVIEW_MAX_INPUT_TOKENS=6000
```

```sh
docker run --rm --platform linux/amd64 --env-file review.env -e CURSOR_API_KEY \
  -v review-state:/state -v /path/to/checkouts:/repos:ro \
  -v /path/to/reviewer-secrets:/run/secrets:ro assay-pr-agent
```

For environment-only credentials, omit the two `_FILE` lines and the secrets volume. Pass `-e REVIEW_GITHUB_PRIVATE_KEY -e REVIEW_GITLAB_TOKEN` to forward exported values. For Argo CD or Flux, the same container accepts Kubernetes Secret volumes via `_FILE` or Secret-backed environment variables via `env.valueFrom.secretKeyRef`. Mount files readable by UID 10001. Choose one source per credential. The agent reads them at startup, so restart or roll the deployment after rotation.

For example, this Pod spec fragment mounts the GitHub App key and injects the GitLab token. The other settings from `review.env` can come from a ConfigMap.

```yaml
spec:
  containers:
    - name: reviewer
      # Add to the reviewer's existing container settings.
      env:
        - name: REVIEW_GITHUB_PRIVATE_KEY_FILE
          value: /run/secrets/reviewer/github.pem
        - name: REVIEW_GITLAB_TOKEN
          valueFrom:
            secretKeyRef:
              name: reviewer-credentials
              key: gitlab-token
      volumeMounts:
        - name: reviewer-key
          mountPath: /run/secrets/reviewer
          readOnly: true
  volumes:
    - name: reviewer-key
      secret:
        secretName: reviewer-credentials
        items:
          - key: github-private-key
            path: github.pem
```

Assay uses mode-0600 runtime copies of the supplied credentials. In Docker these live in `/dev/shm`; incoming keys are excluded from the project settings and review database. Keep one service instance per state volume. The normal `/review` path uses GitLab’s token without rotating it. `REVIEW_MODEL` selects the Cursor model.

On an unconfigured interactive first start, the container runs setup before polling. Deployments with an existing Assay reviewer configuration can still mount it at `/home/reviewer/.config/assay` and omit the standalone setup variables. When supplying credentials alongside a mounted roster, its existing repository and identity rules apply.

**Upgrading from v0.1.0.** Stop the old receiver and start the polling image with the same state volume. It reads the existing project settings, request queue and cache. You can remove the forge webhooks, reverse proxy, published port, `REVIEW_PUBLIC_URL` and webhook secrets. The polling container only needs outbound connections.

### Container CI

Pushes to `main` or any Git tag build the linux/amd64 image, run the test suite inside it with networking disabled and publish the tested image. GitHub publishes to `ghcr.io/medici-finance/assay-pr-agent`; GitLab uses the project's container registry. Every build gets a `sha-<commit>` tag plus `main` or the Git tag. Reserved or registry-incompatible Git tags map to `git-ref-<hash>`.

Set the GitHub Actions repository variable `CONTAINER_RUNNER` to a Docker-capable Linux x64 runner label registered for this repository. The default is `ubuntu-latest`. GitLab uses its available untagged runners; the included Docker-in-Docker job requires a runner configured for privileged DinD with its TLS client certificate volume shared. Neither pipeline uses a Cursor API key.

## Install

Use Python 3.12 or later on macOS or Linux. Install `uv`, Git and Cursor Agent, then run these commands in this project. `cursor-agent status` should show the account you expect to be billed. A headless CI runner can use a scoped `CURSOR_API_KEY`.

```sh
uv sync --frozen
.venv/bin/cursor-review --help
```

Run the installed executable from any checkout, or use `--repo` to point it at the project under review. ShellCheck and actionlint are optional executables on `PATH`. A missing applicable scanner appears in the report as `could-not-check`.

## Review a change

```sh
cursor-review --repo /path/to/checkout --base main --estimate
cursor-review --repo /path/to/checkout --base main --max-input-tokens 4000
cursor-review --repo /path/to/checkout --change 'org/repo#42' --estimate
cursor-review --repo /path/to/checkout --change 'org/repo#42' --model MODEL
```

The local form compares the merge base to committed `HEAD`, or to an explicit `--head`. Both commits and their common ancestor must be available locally. The forge form uses the configured Assay backend for that repository. Both providers use the `owner/repo#number` address accepted by `deskread`. GitLab subgroup projects can also use CI mode independent of deskread’s slug syntax. Forge access requires the installed desk tools and their normal role/repository configuration. It reads metadata before and after the file list, checks head and file-count consistency, and records missing patches as incomplete coverage.

### Cost and coverage

- `--estimate` makes zero Cursor calls. It performs the same diff preparation and static checks as a review.
- The default limit is **6,000 estimated input tokens** for the complete user-supplied prompt. An oversized review returns `over-budget` before invoking Cursor. Repeat `--path path/to/file` to explicitly select a smaller scope. The JSON lists the omitted paths.
- Each run can launch Cursor **once**, with a 120-second timeout and at most three accepted findings. There is no model fallback, repair prompt, automatic retry or model polling. The cap covers the submitted prompt. Cursor controls its own system context, output and billed usage. `cursor_usage` is populated only if the CLI supplies it.
- Successful results are cached under `~/.cache/cursor-pr-review` (or `XDG_CACHE_HOME`). Identical evidence, scope identity, model selection and prompt reuse the result. The lock serializes duplicate local requests. For CI reuse across jobs, provide a persistent, trusted `--cache-dir`. Each ephemeral runner otherwise starts cold. Use an explicit model for reproducible comparisons.
- JSON keeps scanner findings separate from model findings. A source file with no usable patch or head-side location (including a whole-file deletion) is listed as omitted. Missing scanner evidence stays visible even when the model finds nothing.
- Exit 0 means the requested full review/estimate completed, including a review with findings. Exit 2 means partial coverage, budget refusal, a model/tool error or a publication failure. Inspect `state`, `checks` and `findings` when using this in CI.

### Static analysis

ShellCheck and actionlint run on the reviewed head’s Git blobs in a temporary directory. The runner uses stdin and the tool defaults. To avoid duplicate shell findings, actionlint’s embedded ShellCheck integration is disabled. Standalone ShellCheck covers changed `.sh` and `.bash` files. If a forge head is absent locally, an applicable scanner reports `could-not-check`.

To reuse Assay’s Go linters, pass `--go-report path/to/report.json` from its [pinned analysis action](https://github.com/medici-finance/assay/tree/main/actions/go-analysis). Supply an artifact from a trusted producer. The runner checks the `go-analysis/v1` schema, source SHA, coverage count, totals and gate, then imports new and worsened findings for the selected files. Scanner diagnostics outside the shown hunks remain in the report. Only diagnostics located in the supplied patch consume model context.

### Use the Go report in a comment-triggered review

In container setup, enter the project's trusted Go analysis workflow or job. Alternatively, add the optional `go_analysis` field to its entry in `REVIEW_PROJECTS`. Leave it blank for projects without an Assay producer. Only enable a producer whose CI definition and artifact publication your team controls. A contributor who can replace the producer could also fabricate its findings.

| Project | Example setting | CI setup |
|---|---|---|
| GitHub | `"go_analysis": "go-lint.yml"` | Use the workflow filename or numeric ID. Run the [Assay action](https://github.com/medici-finance/assay/tree/v1.0.91/actions/go-analysis) on the PR source head. Give the existing reviewer App **Actions: read**. The worker reads the latest run of that workflow for the source SHA and its exact attempt's `go-analysis-lint-<sha>-<run-id>-<attempt>` artifact. |
| GitLab | `"go_analysis": "go-lint"` | Use the exact job name in the MR head pipeline in the target project. Run the Assay scanner against the MR source head with `GITHUB_REPOSITORY="$CI_PROJECT_PATH"` (the existing report format reads this variable). Have that job publish the sanitized report at the artifact root as `report.json`, using `artifacts: {when: always, paths: [report.json]}`. The reviewer needs access to read the pipeline, job and artifact. |

The worker looks up the report only when processing an authorized `/review`. It accepts a completed scan from a failed lint job, verifies the repository and analyzed source SHA, then imports new and worsened findings for changed paths. Findings inside the shown hunks share the existing Cursor prompt and token budget. The summary includes imported findings outside the hunks as well.

If CI is still running or the report is missing, expired, unreadable, invalid or for a different head, the summary marks Go analysis as `could not check`. The rest of the review proceeds with the existing one-call ceiling. A new `/review` after CI finishes can pick up the report. Ordinary polls and CI completion never trigger a model call.

### GitHub Actions and GitLab CI

To run the CLI explicitly in a PR or MR pipeline, use the pipeline event's commit references. This command invokes a review. Use the polling service above for comment-only operation:

```sh
cursor-review --ci --max-input-tokens 4000 > review.json
```

GitHub uses a `pull_request` event and its recorded base and head SHAs. Fetch sufficient history, for example `fetch-depth: 0` with `actions/checkout`. GitLab uses a merge request pipeline and `CI_MERGE_REQUEST_DIFF_BASE_SHA`. It uses the source branch SHA for merged-results pipelines and the commit SHA for detached MR pipelines. Configure `GIT_DEPTH: '0'` or otherwise supply those Git objects. Keep the executable and its environment in a trusted install separate from the change under review. In both systems, save `review.json` as a job artifact even on exit 2.

### Publish a summary

Add `--post` to the `--change` command to post the reviewed commit and findings as a single ordinary comment. Assay handles GitHub/GitLab routing, reviewer custody, repeated identical comments and the current-head check via `deskpost comment ... --kind mr --head SHA`. A stale head is refused. Publication requires the configured Assay reviewer role. A failed post can be retried with the same model and cache without paying again for an unchanged prompt. No write happens by default.

The standalone poller supports `/review`. For additional upstream PR-Agent chat commands such as `/ask` and `/improve`, see [GitHub integration](https://docs.pr-agent.ai/installation/github/) and [GitLab integration](https://docs.pr-agent.ai/installation/gitlab/). GitLab pipeline jobs alone do not receive comment events. The upstream comment server and this CLI are separate entry points.

## Verification

```sh
uv run --frozen python -m unittest discover -s tests -v
```

The fixtures exercise GitHub and GitLab polling, restart handling, the Assay forge envelope/command contract, caching, the budget cutoff, incomplete inputs and Cursor failure handling. Install ShellCheck to run the integration fixtures. Tests substitute a local executable for inference and never post externally.
