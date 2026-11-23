# Cursor PR review

A standalone, single-pass reviewer for GitHub pull requests, GitLab merge requests and local branches. It builds on [PR-Agent](https://github.com/the-pr-agent/pr-agent). The PR-Agent contributors provide the review prompt, line-numbered diff formatter and token estimator used here. We ship their [MIT-licensed](https://github.com/the-pr-agent/pr-agent/blob/main/LICENSE) Python package in the container, with its original license notice. Cursor Agent performs inference. Assay’s installed `deskread` and `deskpost` handle authenticated forge access when you use a change number. Local and CI runs require only the Git objects.

## Request a review in a PR or MR

Post a new comment containing just `/review` on an open GitHub PR or GitLab MR. That comment is the entire request. The receiver uses the PR or MR number from the event and posts a summary on the reviewed head with its configured reviewer account. Requesters do not need to create a tracking issue, add a role label or approve a separate posting step. An edit, push, approval, ordinary comment, internal GitLab note or bot response spends zero model tokens. Each accepted comment gets one durable queue entry. After a restart, queued requests continue. A request interrupted after it started stays recorded; a fresh `/review` can request another attempt. The worker processes requests one at a time and reuses successful results.

### Docker setup

The image contains Cursor Agent, Assay’s released forge tools, ShellCheck and actionlint. Use an **amd64 Linux** runner or Docker’s amd64 emulation. To build from source:

```sh
docker build --platform linux/amd64 -t assay-pr-agent .
```

Provide a persistent state volume, one trusted checkout per project and a dedicated Assay reviewer configuration that can already read and comment on the chosen projects. Mount the actual checkout including its Git objects. The container runs as UID 10001. Give that user read access to the checkouts and read/write access to its dedicated Assay credential cache. Configure the reviewer roster, role credentials and forge mapping for the instance before accepting requests. Supply only the credentials needed by this reviewer. Scanner coverage depends on having the reviewed head available in the checkout.

Start interactively with the paths below adjusted for your host. The setup asks for the public HTTPS receiver URL, each project's forge and full URL, its container checkout path, allowed human account IDs and its webhook secret. It saves the answers to a mode-0600 file on the state volume. Reuse that volume on subsequent starts.

```sh
docker run -it --rm --platform linux/amd64 -p 127.0.0.1:8787:8787 \
  -v review-state:/state -v /path/to/checkouts:/repos:ro \
  -v /path/to/reviewer-assay-config:/home/reviewer/.config/assay \
  -e CURSOR_API_KEY assay-pr-agent
```

For an unattended container, pass the same settings as environment variables. Example Docker env file (one variable per line; keep real secrets outside Git):

```dotenv
REVIEW_PUBLIC_URL=https://review.example.com
REVIEW_PROJECTS=[{"provider":"github","url":"https://github.com/example/api","checkout":"/repos/api","requester_ids":[101],"secret_env":"REVIEW_SECRET_GITHUB"},{"provider":"gitlab","url":"https://gitlab.example.com/example/web","checkout":"/repos/web","requester_ids":[202],"secret_env":"REVIEW_SECRET_GITLAB"}]
REVIEW_SECRET_GITHUB=replace-with-a-unique-random-GitHub-secret
REVIEW_SECRET_GITLAB=replace-with-a-different-random-GitLab-secret
REVIEW_MAX_INPUT_TOKENS=6000
```

Use `--env-file /path/to/reviewer.env` in place of `-it` and keep the same mounts, port and Cursor API key. `REVIEW_PROJECTS` takes precedence over saved settings. `REVIEW_MODEL` selects a Cursor model. Each project URL must use the `owner/project` form supported by the pinned Assay posting tool.

Put an HTTPS reverse proxy in front of port 8787, forwarding the body and provider headers unchanged. Startup prints the exact webhook URL for each project. Register each URL once:

| Project settings | Events and verification |
|---|---|
| GitHub → Settings → Webhooks | Select JSON, enter the project’s secret and choose **Issue comments**. A pull request conversation comment arrives as `issue_comment`. The receiver verifies the SHA-256 signature. [GitHub webhook setup](https://docs.github.com/en/webhooks/using-webhooks/creating-webhooks) |
| GitLab → Settings → Webhooks | Enter the project's **Secret token** and enable **Comments** (called **Note events** by the API). Disable the default push trigger. The receiver checks the secret token and the MR target. [GitLab webhook setup](https://docs.gitlab.com/user/project/integrations/webhooks/) |

An accepted delivery returns HTTP 202 once queued. Ignored and duplicate deliveries return 204. Keep `/state` across container replacements for deduplication and cache reuse. The queue and cache are for a single persistent service instance. Runtime logs show review state, publication outcome and Cursor invocation count. A webhook secret authenticates delivery; the Assay reviewer credentials authorize reading and posting.

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
- Each run can launch Cursor **once**, with a 120-second timeout and at most three accepted findings. There is no model fallback, repair prompt, automatic retry or polling. The cap covers the submitted prompt. Cursor controls its own system context, output and billed usage. `cursor_usage` is populated only if the CLI supplies it.
- Successful results are cached under `~/.cache/cursor-pr-review` (or `XDG_CACHE_HOME`). Identical evidence, scope identity, model selection and prompt reuse the result. The lock serializes duplicate local requests. For CI reuse across jobs, provide a persistent, trusted `--cache-dir`. Each ephemeral runner otherwise starts cold. Use an explicit model for reproducible comparisons.
- JSON keeps scanner findings separate from model findings. A source file with no usable patch or head-side location (including a whole-file deletion) is listed as omitted. Missing scanner evidence stays visible even when the model finds nothing.
- Exit 0 means the requested full review/estimate completed, including a review with findings. Exit 2 means partial coverage, budget refusal, a model/tool error or a publication failure. Inspect `state`, `checks` and `findings` when using this in CI.

### Static analysis

ShellCheck and actionlint run on the reviewed head’s Git blobs in a temporary directory. The runner uses stdin and the tool defaults. To avoid duplicate shell findings, actionlint’s embedded ShellCheck integration is disabled. Standalone ShellCheck covers changed `.sh` and `.bash` files. If a forge head is absent locally, an applicable scanner reports `could-not-check`.

To reuse Assay’s Go linters, pass `--go-report path/to/report.json` from its [pinned analysis action](https://github.com/medici-finance/assay/tree/main/actions/go-analysis). Supply an artifact from a trusted producer. The runner checks the `go-analysis/v1` schema, source SHA, coverage count, totals and gate, then imports new and worsened findings for the selected files. Scanner diagnostics outside the shown hunks remain in the report. Only diagnostics located in the supplied patch consume model context.

### GitHub Actions and GitLab CI

To run the CLI explicitly in a PR or MR pipeline, use the pipeline event's commit references. This command invokes a review. Use the webhook receiver above for comment-only operation:

```sh
cursor-review --ci --max-input-tokens 4000 > review.json
```

GitHub uses a `pull_request` event and its recorded base and head SHAs. Fetch sufficient history, for example `fetch-depth: 0` with `actions/checkout`. GitLab uses a merge request pipeline and `CI_MERGE_REQUEST_DIFF_BASE_SHA`. It uses the source branch SHA for merged-results pipelines and the commit SHA for detached MR pipelines. Configure `GIT_DEPTH: '0'` or otherwise supply those Git objects. Keep the executable and its environment in a trusted install separate from the change under review. In both systems, save `review.json` as a job artifact even on exit 2.

### Publish a summary

Add `--post` to the `--change` command to post the reviewed commit and findings as a single ordinary comment. Assay handles GitHub/GitLab routing, reviewer custody, repeated identical comments and the current-head check via `deskpost comment ... --kind mr --head SHA`. A stale head is refused. Publication requires the configured Assay reviewer role. A failed post can be retried with the same model and cache without paying again for an unchanged prompt. No write happens by default.

The standalone receiver supports `/review`. For additional upstream PR-Agent chat commands such as `/ask` and `/improve`, see [GitHub integration](https://docs.pr-agent.ai/installation/github/) and [GitLab integration](https://docs.pr-agent.ai/installation/gitlab/). GitLab pipeline jobs alone do not receive comment events. The upstream comment server and this CLI are separate entry points.

## Verification

```sh
uv run --frozen python -m unittest discover -s tests -v
```

The fixtures exercise GitHub and GitLab event payloads, the Assay forge envelope/command contract, caching, the budget cutoff, incomplete inputs and Cursor failure handling. Install ShellCheck to run the integration fixtures. Tests substitute a local executable for inference and never post externally.
