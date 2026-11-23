# Cursor PR review

A standalone, single-pass reviewer for GitHub pull requests, GitLab merge requests and local branches. It uses PR-Agent’s published review prompt, line-numbered diff formatter and token estimator. Cursor Agent performs inference. Assay’s installed `deskread` and `deskpost` handle authenticated forge access when you use a change number. Local and CI runs require only the Git objects.

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

With the tool installed on the runner, use the same job command on both platforms:

```sh
cursor-review --ci --max-input-tokens 4000 > review.json
```

GitHub uses a `pull_request` event and its recorded base and head SHAs. Fetch sufficient history, for example `fetch-depth: 0` with `actions/checkout`. GitLab uses a merge request pipeline and `CI_MERGE_REQUEST_DIFF_BASE_SHA`. It uses the source branch SHA for merged-results pipelines and the commit SHA for detached MR pipelines. Configure `GIT_DEPTH: '0'` or otherwise supply those Git objects. Keep the executable and its environment in a trusted install separate from the change under review. In both systems, save `review.json` as a job artifact even on exit 2.

### Publish a summary

Add `--post` to the `--change` command to post the reviewed commit and findings as a single ordinary comment. Assay handles GitHub/GitLab routing, reviewer custody, repeated identical comments and the current-head check via `deskpost comment ... --kind mr --head SHA`. A stale head is refused. Publication requires the configured Assay reviewer role. A failed post can be retried with the same model and cache without paying again for an unchanged prompt. No write happens by default.

This CLI has no webhook listener. For upstream PR-Agent chat commands such as `/review`, `/ask` and `/improve`, see [GitHub integration](https://docs.pr-agent.ai/installation/github/) and [GitLab integration](https://docs.pr-agent.ai/installation/gitlab/). GitLab pipeline jobs alone do not receive comment events. The upstream comment server and this CLI are separate entry points.

## Verification

```sh
uv run --frozen python -m unittest discover -s tests -v
```

The fixtures exercise GitHub and GitLab event payloads, the Assay forge envelope/command contract, caching, the budget cutoff, incomplete inputs and Cursor failure handling. Install ShellCheck to run the integration fixtures. Tests substitute a local executable for inference and never post externally.
