# Assay PR Agent

A standalone, single-pass reviewer for GitHub pull requests, GitLab merge requests and local branches. It builds on [PR-Agent](https://github.com/the-pr-agent/pr-agent). The PR-Agent contributors provide the review prompt, line-numbered diff formatter and token estimator used here. We ship their [MIT-licensed](https://github.com/the-pr-agent/pr-agent/blob/main/LICENSE) Python package in the container, with its original license notice. Choose Cursor Agent, Claude Code, or Codex CLI for inference. Assay’s installed `deskread` and `deskpost` handle authenticated forge access when you use a change number. Local and CI runs require only the Git objects.

## Install with Docker Compose

Start the containers from this checkout, then attach to the installer:

```sh
docker compose up -d --build
docker compose attach setup
```

Press **Enter** in the attached terminal. It prints a random **setup key** and the local URL; open `http://127.0.0.1:8765/`, enter the key, and the page keeps it in a session cookie. Set the repository, permitted requester IDs and review provider in the browser. Follow the provider’s sign-in prompts in the same terminal. Leave checkout paths blank for automatic sync. The sync and review services wait for the first save and reload subsequent saves between cycles.

To return to your shell, type **q** and press **Enter** at the manager menu or while the browser wizard is waiting for an answer. You can also click **Quit setup** in the browser, or enter **/quit** at a terminal wizard prompt. Quitting closes the installer and discards unfinished edits. The reviewer and sync services keep running with the last saved configuration. To reopen setup, run `docker compose start setup && docker compose attach setup`. Press **r** for a fresh provider login.

The setup service publishes its browser port on `127.0.0.1:8765` and opens the listener during a wizard session. For a remote Docker host, forward that loopback port to port 8765 on your workstation. The key is regenerated for every wizard session. If you lose it, click **Print key to console** on the page and read the reprinted key from the attached terminal. Treat that terminal as a credential. The setup service disables Docker log retention for interactive sign-in. Use `docker compose logs sync reviewer` for worker logs. The optional `./install.sh` builds or pulls the image and attaches to the same manager.

To run just the manager from a locally built image, without Compose:

```sh
docker build --platform linux/amd64 -t assay-pr-agent .
docker run -dit --name review-setup --platform linux/amd64 --log-driver none \
  -p 127.0.0.1:8765:8765 -e REVIEW_SETUP_BIND=0.0.0.0 \
  -v review-state:/state -v review-repos:/repos:ro assay-pr-agent manage
docker attach review-setup
```

Use the same state and checkout volumes for the sync and reviewer containers. Compose manages all three services for you. The attachable manager is available when building this checkout.

### Connect a forge

- **GitHub** — Choose **create** and register the private App under the personal or organization account that owns the repositories. Approve the permissions on GitHub, return to setup, and install the App on the selected repositories. The installer exchanges the single-use manifest code for the App ID and private key and verifies the installation ID against each repository. It requests contents and Actions read access and pull request write access. Review discovery still uses polling. Choose **existing** to import an existing App. [GitHub manifest flow](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest)
- **GitLab** — Enter the HTTPS **base URL** of the instance (default `https://gitlab.com`), including its port or installation path, for example `https://git.example:8443/gitlab`. Enter the project as `group/project`. Choose **create** and supply an authorized personal access token with `api` scope once. Setup creates a Reporter project access token, or a group access token for multiple projects in the same group, with `api` and `read_repository` scopes. Project creation requires a Maintainer or Owner, group creation requires a group Owner, and the instance must support access tokens. The reviewer stores the resulting service token. The bootstrap PAT is used only for creation. For projects spanning groups, choose **existing** and supply a token with the required access. GitLab applies its token expiry policy. [Project access tokens](https://docs.gitlab.com/api/project_access_tokens/) · [Group access tokens](https://docs.gitlab.com/api/group_access_tokens/)
- **GitLab TLS** — Import a PEM private CA bundle if the instance uses private PKI. You can also import a PEM X.509 certificate revocation list (CRL). Setup applies trust to token creation, API reads and artifacts. Assay tools and git-sync use a container-local HAProxy TLS connection to the configured instance with hostname, chain and CRL verification. Replace the CRL in the manager before it expires. A missing applicable or expired CRL blocks the verified connection. If a TLS setting is supplied via a mounted file, roll both workers after replacing it.

If setup stops after creating a remote credential, select **create** again for the same scope to resume from the protected state volume. Replacing an active credential creates a new one. After the workers have switched, revoke the old token or retire the old App in the forge.

### Review provider

| `REVIEW_PROVIDER` | Command | Credential |
|---|---|---|
| `cursor` (default) | `cursor-agent -p --mode=ask` | Cursor CLI login or `CURSOR_API_KEY` |
| `claude` | `claude -p` with tools disabled and one turn | Claude subscription login or `ANTHROPIC_API_KEY` |
| `codex` | `codex exec` in an ephemeral read-only session with shell and web search disabled | ChatGPT device login or `CODEX_API_KEY` |

For new provider configurations the wizard defaults to CLI sign-in. Existing API-key configurations keep their current method. In the terminal the container runs `cursor-agent login`, `codex login --device-auth` or `claude auth login --claudeai`. Follow the CLI’s link in your own browser and enter the code where the provider directs you. Codex device login may need to be enabled by your workspace administrator. For Claude, sign in directly to the bundled Claude Code CLI with your own subscription. Configure your own forge requester IDs for this installation. Each person using subscription billing should use their own signed-in instance. The CLI handles the authorization code and stores its own session. [Codex authentication](https://developers.openai.com/codex/auth/) · [Claude authentication](https://code.claude.com/docs/en/authentication) · [Using Claude Code in other products](https://code.claude.com/docs/en/legal-and-compliance#can-customers-offer-claude-code-in-their-products)

Compose shares the writable `provider-login` volume between setup and reviewer. Protect it like a credential. The sync sidecar has no mount for this volume. The reviewer checks sign-in on startup. To switch accounts, attach to setup and press **r**. Use the browser to confirm the repository list and Cursor team. For plain Docker the default location is inside the existing state volume at `/state/provider-login`. To mount a separate writable directory, set `REVIEW_LOGIN_DIR` identically for setup and reviewer.

For an environment-only deployment set `REVIEW_PROVIDER` to `cursor`, `codex` or `claude` and `REVIEW_AUTH_MODE=login`, then run the image once with `-it`, the persistent volumes, and the `login` command before starting the reviewer. For unattended API-key provisioning, set `REVIEW_AUTH_MODE=api-key` (the environment default). Each API key also supports a `_FILE` setting. Use `REVIEW_MODEL` to specify a model offered by the selected provider. The agent launches at most one provider CLI per eligible uncached request, with the same prompt estimate and timeout. CLIs control their own system context, internal requests and billing. Output includes `provider`, `model_invocations` and available `model_usage`. The cache separates providers and Cursor teams.

Cursor setup uses the CLI's `team list` and `team switch` commands. With multiple eligible teams you choose a numeric ID. With one, the installer selects it automatically. An empty membership list uses personal scope. An account with memberships but no selectable team fails setup. The service rechecks the configured account at startup and verifies the active team in an isolated Cursor configuration. For environment-only deployment set `REVIEW_CURSOR_TEAM_ID` when the account has multiple eligible teams. A failed membership lookup or selection stops startup before any review. These checks use Cursor's account service without starting an inference request.

### Automatic repository sync

Compose runs Kubernetes `git-sync` in a separate container with write access to the checkout volume. For each managed project it refreshes the remote default branch with full history, using the existing Assay reviewer credential, and atomically switches the `current` symlink. The reviewer mounts the volume read-only. Allow repository contents read access for the GitHub App or GitLab repository read access for its token. The sync interval defaults to 120 seconds (`REVIEW_SYNC_SECONDS` on the sync service). Polling uses the forge's PR/MR diff directly. When a requested review needs a local static scan and its commit is missing locally, the reviewer uses git-sync to fetch just that change ref into a temporary checkout, verifies its SHA, and discards it after scanning. A moved or unavailable ref is reported as incomplete coverage.

In `REVIEW_PROJECTS`, use `"managed": true` in place of a checkout path for a managed clone, or keep your existing absolute `checkout` and bind mount. With Compose, mount custom checkout paths in both services. The sync service leaves operator-managed checkouts alone. Private repositories retain a local copy of their history on the checkout volume.

## Request a review in a PR or MR

Post a new comment containing just `/review` on an open GitHub PR or GitLab MR. The service polls the configured projects every two minutes by default. It accepts new requests from the human accounts you configure and posts the result on the reviewed head. Setup takes GitHub or GitLab usernames and stores their numeric account IDs. A poll uses zero model tokens. Requests run one at a time with the existing prompt budget and cache.

Assay’s `deskmonitor pr` supplies the open change numbers. Batched `deskread comments --change` calls read their conversations on every cycle, including changes with no new commits. The poller skips old commands, edits detectable from the forge timestamps, bot responses and ordinary messages. For GitLab, Assay v1.0.94 omits the internal-note flag. Only an otherwise eligible command gets a single [GitLab note lookup](https://docs.gitlab.com/api/notes/#retrieve-a-merge-request-note), using the existing Assay reviewer credential and instance. Internal and system notes cannot start a review.

Each project starts watching at its first activation. Keep the state volume across restarts to pick up requests posted during downtime on still-open changes. Queued reviews survive a restart. So does a review that was running when the poller stopped: the next start queues it again. A repeat stays at the head its first start resolved. `<state>/requests.head` names the request being reviewed, which the poller writes before it starts the request, and the head its review resolved, which the review adds before the model is called. A restart queues an interrupted request again only when that record names it, so a retry never takes another request's head, and a repeat that finds the change at another head is not reviewed. The request is then marked failed, with a line in the poller log naming the change and the comment, and needs a fresh `/review`. Repeating a review is otherwise safe, because the model result is cached for an unchanged head and a repeated identical comment is dropped. A request is started at most twice. One that was running when the poller stopped for the second time is marked failed instead, with a line in the poller log naming the change and the comment, and needs a fresh `/review` to run again. A review that was running when a version without the head record stopped is marked failed as well, because the head it started at is unknown. A restart never repeats a review that ran but could not publish its result; post `/review` again to retry it. The review records how it ended before it exits, so the next start keeps that outcome for a review that finished after its poller was killed, instead of repeating the request. Duplicate reads do not incur another model call. A deleted comment or a change closed between polls may never be observed.

Only one poller may use a state volume. A second poller on the same volume exits at startup with `Another poller is already running on this state volume`, because it would otherwise take the first poller’s running reviews for interrupted ones and run them again. Stop the old poller before starting its replacement, as the Compose and Kubernetes deployments in this repository do. A review in progress holds the lock as well, so a poller that is killed while a review runs leaves the lock with that review until it exits, and its replacement is refused until then.

### Docker setup

The image includes all three provider CLIs, git-sync, Qualgen, Assay’s released forge tools, ShellCheck, actionlint, Hadolint, zizmor and OSV-Scanner. Use an **amd64 Linux** runner or Docker’s amd64 emulation. To build from source:

```sh
docker build --platform linux/amd64 -t assay-pr-agent .
```

Add `--build-arg REVIEW_BUILD_SHA=$(git rev-parse HEAD)` to have a local image report its commit; otherwise the startup banner shows `commit unknown` and the build time.

### Interactive setup

Run the setup command once with a writable state volume and your checkouts mounted at the paths the reviewer will use:

```sh
docker run --rm -it --platform linux/amd64 \
  -v review-state:/state -v /path/to/checkouts:/repos:ro assay-pr-agent setup
```

Enter the projects, checkout paths inside the container, visibility, the GitHub or GitLab usernames (or numeric IDs) allowed to request reviews, and the responsible operator’s username. Setup looks the numeric account IDs up on the forge; GitLab lookups use the reviewer token. With both forges configured, enter the operator as `username:numeric-id`. Setup asks for the selected authentication method and credentials for the forges you selected. For GitHub, have the App ID, installation ID and private PEM ready. For GitLab, have the reviewer token ready. You can accept the defaults of 120 seconds between polls, 6,000 estimated input tokens per review and the selected CLI’s default model.

For each credential, choose **paste** to enter it with terminal echo disabled, **import** to copy a readable file into the volume, or **mount** to keep using an external file. For import or mount, add the source volume to the setup command (for example, `-v /path/to/secrets:/run/secrets:ro`) and enter the file’s absolute path **inside** the container. With **mount**, provide the same file path when running the service. With **import**, the source mount is only needed during setup.

Start the reviewer with the same volumes. No configuration environment variables are required:

```sh
docker run --rm --platform linux/amd64 \
  -v review-state:/state -v /path/to/checkouts:/repos:ro assay-pr-agent
```

The active settings and imported or pasted credentials are in `/state/setup/`. Credential files are plaintext, owner-readable and writable (0600), inside a private directory (0700). Protect the volume and its backups. Setup checks local files and credential formats; Cursor team selection requires account service access. The poller checks forge access during normal operation.

To change settings or rotate credentials, run the setup command again, or run `docker exec -it <container> cursor-review-setup`. Existing settings and credentials can be kept without displaying their values. The Compose workers reload on save. For plain Docker, restart the sync and reviewer containers. Cancelling before the save keeps the previous configuration active. An explicit environment setting takes precedence over its saved value.

### Standalone Docker credentials

Mount the checkout for each project read-only and a writable volume at `/state` for the request queue, cache and Assay audit records. The container runs as UID 10001. It generates the reviewer configuration on first start and updates it from the supplied settings on restart. You can use it without installing Assay on the host.

| Docker setting | What to supply |
|---|---|
| `CURSOR_API_KEY` | Cursor API key for headless inference. |
| `REVIEW_OPERATOR` | The responsible human’s forge `login:numeric-id` (the setup wizard also accepts a bare username and looks up the ID). Assay uses this for posting attribution. The project’s `requester_ids` separately control who can request reviews. |
| `REVIEW_GITHUB_APP_ID`, `REVIEW_GITHUB_INSTALLATION_ID` | Numeric IDs for the GitHub App and the installation covering your repositories. Use a reviewer installation with access to read PRs and diffs and write PR conversation comments. |
| `REVIEW_GITHUB_PRIVATE_KEY` **or** `REVIEW_GITHUB_PRIVATE_KEY_FILE` | The App’s unencrypted RSA PEM or an absolute path to the mounted PEM. The value accepts actual line breaks or literal `\n`. |
| `REVIEW_GITLAB_TOKEN` **or** `REVIEW_GITLAB_TOKEN_FILE` | A dedicated GitLab reviewer token with API permission to read MRs and post notes, or an absolute path to the mounted token. |
| `REVIEW_GITLAB_BASE_URL` | HTTPS GitLab installation URL, including optional port and path prefix. An entry in `REVIEW_PROJECTS` can specify `gitlab_base`. |
| `REVIEW_GITLAB_CA_FILE`, `REVIEW_GITLAB_CRL_FILE` | Optional absolute paths to a PEM private CA bundle and PEM CRL. The corresponding variables without `_FILE` accept inline PEM. Supply the same paths to setup, sync and reviewer. |
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

Pushes to `main` or any Git tag build the linux/amd64 image, run the test suite inside it with networking disabled and publish the tested image. GitHub publishes to `ghcr.io/medici-finance/assay-pr-agent`; GitLab uses the project's container registry. Every build gets a `sha-<commit>` tag plus `main` and `latest` for the default branch, or the Git tag. Reserved or registry-incompatible Git tags map to `git-ref-<hash>`. The commit, tags and build time are baked into the image, and the container prints them as its first line at startup.

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
- The limit is **6,000 estimated input tokens** by default for the complete user-supplied prompt. The saved setup's `REVIEW_MAX_INPUT_TOKENS` setting (asked during setup, or set as an environment variable) changes it; `--max-input-tokens` overrides both for one run. An oversized review returns `over-budget` before invoking Cursor. Repeat `--path path/to/file` to explicitly select a smaller scope. The JSON lists the omitted paths.
- Each run can launch Cursor **once**, with a 120-second timeout and at most three accepted findings. There is no model fallback, repair prompt, automatic retry or model polling. The cap covers the submitted prompt. Cursor controls its own system context, output and billed usage. `cursor_usage` is populated only if the CLI supplies it.
- Successful results are cached under `~/.cache/cursor-pr-review` (or `XDG_CACHE_HOME`). The container image sets `XDG_CACHE_HOME=/tmp/.cache` because its root filesystem is read-only, and its poller passes `--cache-dir /state/cache` for the reviews it runs. Identical evidence, scope identity, model selection and prompt reuse the result. The lock serializes duplicate local requests. For CI reuse across jobs, provide a persistent, trusted `--cache-dir`. Each ephemeral runner otherwise starts cold. Use an explicit model for reproducible comparisons.
- JSON keeps scanner findings separate from model findings. A source file with no usable patch or head-side location (including a whole-file deletion) is listed as omitted. Missing scanner evidence stays visible even when the model finds nothing.
- Exit 0 means the requested full review/estimate completed, including a review with findings. Exit 2 means partial coverage, budget refusal, a model/tool error or a publication failure. Inspect `state`, `checks` and `findings` when using this in CI.

### Static analysis

ShellCheck and actionlint run on the reviewed head’s Git blobs in a temporary directory. The runner uses stdin and the tool defaults. To avoid duplicate shell findings, actionlint’s embedded ShellCheck integration is disabled. Standalone ShellCheck covers changed `.sh` and `.bash` files. If a forge head is absent locally, an applicable scanner reports `could-not-check`.

To reuse Assay’s Go linters, pass `--go-report path/to/report.json` from its [pinned analysis action](https://github.com/medici-finance/assay/tree/main/actions/go-analysis). Supply an artifact from a trusted producer. The runner checks the `go-analysis/v1` schema, source SHA, coverage count, totals and gate, then imports new and worsened findings for the selected files. Scanner diagnostics outside the shown hunks remain in the report. Only diagnostics located in the supplied patch consume model context.

### Use the Go report in a comment-triggered review

In container setup, enter the project's trusted Go analysis workflow or job. Alternatively, add the optional `go_analysis` field to its entry in `REVIEW_PROJECTS`. Leave it blank for projects without an Assay producer. Only enable a producer whose CI definition and artifact publication your team controls. A contributor who can replace the producer could also fabricate its findings.

| Project | Example setting | CI setup |
|---|---|---|
| GitHub | `"go_analysis": "go-lint.yml"` | Use the workflow filename or numeric ID. Run the [Assay action](https://github.com/medici-finance/assay/tree/v1.0.94/actions/go-analysis) on the PR source head. Give the existing reviewer App **Actions: read**. The worker reads the latest run of that workflow for the source SHA and its exact attempt's `go-analysis-lint-<sha>-<run-id>-<attempt>` artifact. |
| GitLab | `"go_analysis": "go-lint"` | Use the exact job name in the MR head pipeline in the target project. Run the Assay scanner against the MR source head with `GITHUB_REPOSITORY="$CI_PROJECT_PATH"` (the existing report format reads this variable). Have that job publish the sanitized report at the artifact root as `report.json`, using `artifacts: {when: always, paths: [report.json]}`. The reviewer needs access to read the pipeline, job and artifact. |

The worker looks up the report only when processing an authorized `/review`. It accepts a completed scan from a failed lint job, verifies the repository and analyzed source SHA, then imports new and worsened findings for changed paths. Findings inside the shown hunks share the existing Cursor prompt and token budget. The summary includes imported findings outside the hunks as well.

If CI is still running or the report is missing, expired, unreadable, invalid or for a different head, the summary marks Go analysis as `could not check`. The rest of the review proceeds with the existing one-call ceiling. A new `/review` after CI finishes can pick up the report. Ordinary polls and CI completion never trigger a model call.

### Hadolint, zizmor and OSV-Scanner

On an authorized GitHub or GitLab `/review`, the reviewer selects non-deleted files by path. Hadolint checks Dockerfiles and Containerfiles, including names such as `Dockerfile.release` and `build.dockerfile`. Zizmor audits GitHub Actions workflow and action definition files, including those stored in GitLab repositories. OSV-Scanner checks recognized dependency files such as `go.mod`, `Cargo.lock`, `uv.lock`, `requirements.txt`, and `package-lock.json`. Findings are scoped to changed files and can include existing issues on unchanged lines in those files.

The reviewer first looks for a completed producer at the reviewed **source commit**. The defaults work on both forges without additional reviewer configuration:

| Tool | GitHub workflow | GitHub artifact / GitLab job | File inside artifact |
|---|---|---|---|
| Hadolint | `hadolint.yml` | `hadolint` | `hadolint.json` (Hadolint JSON) |
| zizmor | `zizmor.yml` | `zizmor` | `zizmor.sarif` (SARIF 2.1.0) |
| OSV-Scanner | `osv-scanner.yml` | `osv-scanner` | `osv-scanner.json` (OSV JSON) |

Reuse an existing CI job by pointing `scanner_ci` in its project entry (`REVIEW_PROJECTS` or `projects.json`) at its native reports. For example, a GitHub project whose trusted workflow checks every Dockerfile can include:

```json
"scanner_ci": {
  "hadolint": {
    "producer": "lint.yml",
    "artifact": "lint-results",
    "report": "reports/hadolint.json",
    "all_paths": true
  }
}
```

The GitHub reviewer App needs Actions read permission to retrieve artifacts. The GitLab reviewer token needs access to the target project’s CI jobs and artifacts. For GitLab, `producer` is the job name and `report` is the project-relative artifact path. GitHub reads the named artifact from the current workflow attempt. A `pull_request` workflow commonly checks out a synthetic merge commit even though the API reports the PR head. For non-push workflows, check out the source revision, run `git rev-parse HEAD > review-head.txt` in the same checkout as the scanner, and upload that file at the artifact root next to the native report. The reviewer compares it to the reviewed SHA. A missing or mismatched stamp triggers local fallback. GitLab verifies the target project, MR pipeline SHA and job commit. Configure producers whose scope and output you trust. The importer recognizes repository-relative paths and standard GitHub and GitLab checkout paths. Set `source_root` to the absolute CI checkout directory for custom runners.

By default only paths evidenced in the native report get reused. For example, an empty Hadolint JSON array cannot tell the reviewer which files CI checked. Set `all_paths: true` only for a producer that checks **every applicable changed file** and reports scanner errors as job failures. A successful report from that producer can cover clean files. A failed producer only supplies paths with actual findings. Missing, stale, expired, malformed or incomplete evidence falls back per uncovered file.

Local checks read regular Git blobs at the reviewed SHA. They run in temporary directories without forge or model credentials. OSV compares dependencies locally and caches public advisory databases in `/state/osv-db`. By default it can download or refresh those databases. To operate offline, prepopulate `/state/osv-db/osv-scalibr/<ecosystem>/all.zip` and set `REVIEW_OSV_DOWNLOAD_DATABASES=0` on the reviewer. An unavailable database, missing blob or timeout is reported as incomplete coverage. [OSV offline database guide](https://google.github.io/osv-scanner/usage/offline-mode/)

The posted summary identifies CI versus local results. The JSON report includes the findings under `static_checks`. These three scanners contribute **zero model tokens**. Their output is outside the review prompt, token estimate and model cache key, including when the model review is skipped or fails.

### Qualgen history advisories

The container includes the checksum-verified Assay Qualgen v1.0.94 release. On startup and then every 24 hours a background worker runs `qualgen mine` against each project's mounted checkout. Use a managed checkout or keep a full checkout of the trusted integration branch refreshed in your deployment. The miner reads local Git history and stores its results under `/state/qualgen`. A changed tip extends the previous snapshot when history permits. An unchanged tip skips mining. Reviews can still read the last completed snapshot during a refresh.

Set `REVIEW_QUALGEN_INTERVAL_SECONDS` in Docker or enter the refresh interval during setup. Use `0` to disable mining and advisories. Keep the state volume private to the reviewer. It contains historical file and commit data.

An authorized GitHub or GitLab `/review` runs `qualgen check` for the changed paths and adds its notices to the posted summary. A notice can identify a frequently co-changed file that is missing from the change. The summary identifies the mined commit and marks paths with insufficient history. Live reference notices are included only when the checkout is at the reviewed head. While the first snapshot is being built, the review reports that history is unavailable. The comment shows up to 20 entries; the JSON includes the full advisory list.

Qualgen uses local CPU, disk and Git history. Its output goes directly into the summary, outside the Cursor prompt, token estimate and model cache key. An over-budget review or a model failure can still publish historical advisories.

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

## License

This repository is licensed under the [Apache License 2.0](LICENSE). The license covers this project's own source. Third-party software that the image downloads or bundles keeps its own license and terms: the provider CLIs, PR-Agent, Assay's desk tools and Qualgen, and the scanners.
