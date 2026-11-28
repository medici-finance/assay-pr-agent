# Assay PR Agent

A standalone, single-pass reviewer for GitHub pull requests, GitLab merge requests and local branches. It builds on [PR-Agent](https://github.com/the-pr-agent/pr-agent). The PR-Agent contributors provide the review prompt, line-numbered diff formatter and token estimator used here. We ship their [MIT-licensed](https://github.com/the-pr-agent/pr-agent/blob/main/LICENSE) Python package in the container, with its original license notice. Choose Cursor Agent, Claude Code, or Codex CLI for inference. The `pr-agent-forge` helper uses Assay’s Go library for authenticated forge access when you use a change number. Local and CI runs require only the Git objects. A `/visual-review` comment asks instead for a map of where a diff is worth reading and a short explanation of the change.

## Security model

The reviewer reads changes written by people who need no access to your deployment, and it posts under your reviewer identity. What sets it apart, at a glance:

- Deterministic checks run first at zero model tokens: ShellCheck, actionlint, Hadolint, zizmor, OSV-Scanner and, when configured, Assay's Go analysis report.
- One bounded model call per review, under a token budget of 6,000 estimated input tokens by default, with at most six findings by default (`REVIEW_MAX_FINDINGS`).
- Bounded Qualgen history advisories for files shown in the prompt, with additional advisories in the review report.
- Hardened for hostile pull requests: a requester allowlist for comment triggers, model tools disabled, untrusted input labelled, and a posted comment that is flattened and quoted as code.

The rest of this section states what the code enforces, in the order a request meets it. The sections after it cover installation and operation.

### Who can start a comment-triggered review

The poller queues a review only when a new comment on an open change meets all of these requirements:

- The body is `/review` and nothing else apart from surrounding whitespace. A quoted or decorated command such as `> /review` or `/review please` is an ordinary comment.
- The author's numeric account ID is in the project's `requester_ids`. Setup resolves usernames to IDs once. The list is the control on who can spend model budget; nothing limits a listed account beyond one review per comment.
- The comment is unedited, with equal created and updated timestamps, and is not older than the project's activation.
- On GitHub, the author type is `User`, which refuses bots, organizations and mannequins, and the comment URL is that change's `#issuecomment-<id>`.
- On GitLab, the conversation list that the forge helper reads omits note visibility and rounds timestamps to seconds, so the one otherwise eligible note is looked up once through the API with the reviewer token. It must be a non-internal, non-system note on that merge request, by the same author, with the same body and unedited according to the API's timestamps. The lookup rejects authors marked as bots. Allowlist human accounts because the API can omit the bot flag.

A failed GitLab lookup is retried on the next poll, and a note that fails the lookup's checks, or that the lookup no longer finds, is recorded as ignored. Each comment starts at most one review, and when the review starts the change must still be open at its configured URL. A poll never calls the model. Operators can also run `cursor-review --change ... --post` directly. This uses the configured project and reviewer credentials and does not require a request comment or check its author allowlist.

### What the model can and cannot do

Each review can make one inference invocation of the selected provider CLI, with a 120-second timeout and no retry or fallback. If the prompt exceeds the token limit, inference is skipped and the review returns `over-budget` (see [Cost and coverage](#cost-and-coverage)). Direct Cursor runs can perform a separate account check before estimating the prompt. The answer must be one successful result of at most 32,768 characters with at most the configured number of findings, six by default. Tools are disabled per provider:

- **Cursor** — `cursor-agent -p --mode=ask` in a temporary workspace holding a written `.cursor/cli.json` with an empty allow list and a deny list for shell, file read, file write, web fetch and MCP tools.
- **Claude Code** — `claude -p --tools "" --strict-mcp-config --max-turns 1 --no-session-persistence`.
- **Codex** — `codex exec --ephemeral --sandbox read-only`, with the shell tool, unified exec, multi-agent, apps and web search disabled and user configuration and rules ignored.

The CLI receives an allow-listed environment: its own provider's key or sign-in profile and the path, locale, configuration, temporary, proxy and TLS settings. Forge credentials, CI secrets and other providers' keys are not in that environment, and no repository is present in its workspace.

### Untrusted input and evidence binding

The regular prompt contains PR-Agent's review rubric, scanner diagnostics that fall on shown lines, and the diff hunks. It can include the forge's title and description when available, or recent commit messages in a local or CI review. Mid and max can add surrounding code and other bounded context. Max can also include eligible conversation comments, excluding internal GitLab notes and earlier review summaries. Qualgen advisories may appear for files shown in the prompt. Hadolint, zizmor and OSV-Scanner output stays in the report. The prompt labels the diagnostics, patch and context as `untrusted data` and instructs the model to work without tools. The reviewer checks the change before and after reading its file list and refuses it if the head moves.

A finding is accepted only if it names a reviewed file and cites a contiguous range of head lines that the shown hunks contain. One failing finding rejects the whole answer: the review ends as `model-unavailable` and its scanner results are still posted. A file name containing a C0 control character (below U+0020), or a duplicate file name, stops the review before the model is called.

### The posted comment

The reviewer's only forge write is one ordinary conversation comment, which the `pr-agent-forge` helper posts through Assay's forge library under the reviewer credential ([Publish a summary](#publish-a-summary)). It never submits a review verdict, approves, merges or edits a change. Before writing, the library checks that the repository's live visibility is within the project's configured `visibility`, that the change is still open and that its head is still the reviewed SHA. It also scans the body for an impersonated ruling by the configured operator, personal-data shapes and, on internal and public projects, references to private repositories and `REVIEW_WITHHELD_IDENTIFIERS`, and it refuses a body over 16 KiB. Once the agent writes a receipt to the state volume, it suppresses an identical retry for the same change and head, including after a restart. An interruption between forge acceptance and saving the receipt can result in a duplicate.

Every pull request path, model title and message, scanner rule and message, and Qualgen notice is quoted as a code span. Its delimiters are one backtick longer than any run inside the text, and an inner space at each end keeps backticks in the text from joining the delimiters. That keeps Markdown, links and `@mentions` in those strings literal. Line breaks are collapsed in every line of the comment, so none of those strings can add a line, start a forge quick action or open a multi-line construct. The Hadolint, zizmor and OSV-Scanner summary shows at most 30 rows of 450 characters, and the Qualgen list at most 20 entries of 500 characters. The forge helper refuses a body over 16 KiB, so a comment that would exceed it is shortened instead. In a regular review the reviewer reserves space for each finding's location, title and at least 140 characters of its message, or the whole message when shorter. It shares the remaining space among the findings and says when a message was shortened. When that is not enough, or in a higher tier, every finding keeps a 60-character title and a 140-character message, and scanner, history and coverage rows that do not fit are counted as omitted. A comment that still exceeds the limit is not posted and the review reports a publication failure.

### Local scanners

ShellCheck, actionlint, Hadolint, zizmor and OSV-Scanner never read the checkout's working tree or follow a link. Each receives an immutable Git blob from the reviewed revision: ShellCheck and actionlint on standard input in an empty temporary directory, the other three as a file in a temporary directory with an environment of only `PATH` and `HOME` (plus the advisory database cache for OSV-Scanner). Each run has a timeout and a size cap, and Hadolint, zizmor and OSV-Scanner refuse anything that is not a regular file, such as a symlink or a submodule. For a managed checkout whose reviewed commit is missing locally, the reviewer fetches only that change's ref into a temporary checkout, uses it only if its SHA matches, and discards it. Nothing from the change is built or executed.

### CI artifacts

Hadolint, zizmor, OSV-Scanner and the Go analysis report can come from CI instead. A report is accepted only from the producer named for the project (`go_analysis` only when you set it) and from a finished run for the reviewed source commit. Downloads are capped at 8 MiB, follow redirects by hand over HTTPS, and the forge token is dropped before any redirect target is requested. The Go report must carry the `go-analysis/v1` schema, the repository, the reviewed source SHA, complete coverage and agreeing totals, exit code and gate. For Hadolint, zizmor and OSV-Scanner, an unusable or missing CI report triggers a local scan when the source and scanner are available. If coverage still cannot be obtained, the summary says `could not check`. The reviewer continues without unusable Go analysis evidence. Use producers whose pipeline definitions and outputs your team controls, including when relying on default scanner producer names. A name and matching SHA alone do not establish producer trust. [Use the Go report in a comment-triggered review](#use-the-go-report-in-a-comment-triggered-review) describes the Go analysis setup.

### Credential custody

Forge access always uses the reviewer credential from setup or the container environment. Before it polls, syncs or reviews, the agent copies that credential into a private runtime directory (in `/dev/shm` under Docker) and removes the credential variables from its own environment. On GitHub, the forge helper exchanges the App private key for an installation token restricted to the one repository, which the agent reuses until five minutes before it expires; on GitLab the configured reviewer token is used as it is, and nothing is rotated. The helper receives the credential on standard input for each call, and the GitLab note lookup, CI artifact downloads and git-sync read the same runtime copies. Every forge and provider credential file the agent writes, including those runtime copies, is plaintext, mode 0600 and replaced by an atomic rename. The agent removes recognized provider API keys, `REVIEW_FORGE_*` variables and recognized forge secret and TLS inputs from the environments of its forge helper, Git commands and ShellCheck and actionlint scanners. Keep unrelated credentials such as `GH_TOKEN` out of the service environment because those child processes can inherit them.

The state volume holds the setup's credentials, the request queue, the cache, the publication receipts and Qualgen's history. Follow the existing advice to [protect the volume and its backups](#interactive-setup), to [treat the sign-in volume as a credential](#review-provider) and to [keep the state volume private](#qualgen-history-advisories).

### Deployment hardening

Discovery is polling only: there is no webhook, the reviewer and sync services publish no port, and the GitHub App is created with an inactive webhook and no events. The reviewer mounts the checkout volume read-only; only the sync service writes to it.

`compose.yaml` and the Kubernetes manifests run every container with a read-only root filesystem, all capabilities dropped and no privilege escalation, with a 1 GiB temporary mount on `/tmp`. The manifests also set a non-root user, the runtime's default seccomp profile, no service account token and a `restricted` pod-security namespace, and define the `pr-agent-egress` NetworkPolicy: egress only, to DNS on port 53 and otherwise to the public internet, with RFC1918, CGNAT and link-local ranges (which include cloud metadata addresses) and IPv6 unique-local and link-local ranges blocked. [`k8s/README.md`](k8s/README.md) covers a forge or proxy on a private network and the policy's node-traffic limit.

The image runs as UID 10001 and installs the Cursor Agent CLI from a release tarball pinned by version and SHA-256 digest, as it already does for Qualgen, Hadolint, zizmor and OSV-Scanner, instead of the `cursor.com/install` script that always fetches the current release. The forge helper is compiled from an Assay source commit pinned in the Dockerfile. The plain `docker run` commands in this README do not pass the equivalent runtime flags.

### Limits

A finding is still model output written after reading attacker-supplied code, so findings are advisory: prompt injection can change what the one comment says inside its code spans, or talk the model out of reporting, and a person decides what to do with a change. The tool restrictions are flags and configuration that each provider CLI enforces, not a sandbox this agent applies to the CLI process, which controls its own system context and requests. The scanners run as the service user without a filesystem sandbox, and ShellCheck and actionlint also inherit the filtered service environment, so a compromise of any scanner could read the credential files the service holds. Only line breaks are removed from posted text; other control and formatting characters, such as bidirectional overrides, pass through. The state volume holds credentials and repository history. The forge data behind the trigger checks, the GitHub token exchange and the visibility, head and body checks on a post are the behaviour of Assay's forge library, built into the helper from the pinned commit.

## Install with Docker Compose

Start the containers from this checkout, then attach to the installer:

```sh
docker compose up -d --build
docker compose attach setup
```

Press **Enter** in the attached terminal. It prints a random **setup key** and the local URL; open `http://127.0.0.1:8765/`, enter the key, and the page keeps it in a session cookie. Set the repository, permitted requester IDs and review provider in the browser. Follow the provider’s sign-in prompts in the same terminal. Leave checkout paths blank for automatic sync. The sync and review services wait for the first save and reload subsequent saves between cycles.

Once a setup is saved, the same page opens on its settings instead of the wizard. Change one setting at a time: each row has its own **Change** and **Save**, and a save replaces the saved configuration, so the sync and review services reload it between cycles like a wizard save. The page edits the prompt budget of a review and of a visual review, the findings cap of each review tier, the polling, sync and history intervals, the longest wait before a quiet change is read again, OSV database downloads, the model, the Cursor billing team, the operator, the GitHub App IDs, each repository’s requesters, visibility, Go analysis producer and scanner CI settings, and credentials, which are replaced but never shown. A setting supplied as an environment variable of the setup container is listed read-only with the variable's name (never a credential's value), because the environment wins; to change it, change that variable. The credential and Cursor team rows follow the provider and sign-in method that environment selects. The page sees only the setup container's environment; the sync and reviewer services read their own. To add or remove a repository, or to change the provider or sign-in method, click **Run the setup wizard**; the page returns when the wizard saves.

To return to your shell, type **q** and press **Enter** at the manager menu, while the browser wizard is waiting for an answer or on the settings page. You can also click **Quit setup** or **Close settings** in the browser, or enter **/quit** at a terminal wizard prompt. Quitting closes the installer and discards unfinished edits. The reviewer and sync services keep running with the last saved configuration. To reopen setup, run `docker compose start setup && docker compose attach setup`. Press **r** for a fresh provider login.

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

For an environment-only deployment set `REVIEW_PROVIDER` to `cursor`, `codex` or `claude` and `REVIEW_AUTH_MODE=login`, then run the image once with `-it`, the persistent volumes, and the `login` command before starting the reviewer. For unattended API-key provisioning, set `REVIEW_AUTH_MODE=api-key` (the environment default). Each API key also supports a `_FILE` setting. Use `REVIEW_MODEL` to specify a model offered by the selected provider. A regular review uses at most one inference call. Higher tiers share their token budget across multiple calls. CLIs control their own system context, internal requests and billing. Output includes `provider`, `model_invocations` and available `model_usage`. The cache separates providers and Cursor teams.

Cursor setup uses the CLI's `team list` and `team switch` commands. With multiple eligible teams you choose a numeric ID. With one, the installer selects it automatically. An empty membership list uses personal scope. An account with memberships but no selectable team fails setup. The service rechecks the configured account at startup and verifies the active team in an isolated Cursor configuration. For environment-only deployment set `REVIEW_CURSOR_TEAM_ID` when the account has multiple eligible teams. A failed membership lookup or selection stops startup before any review. These checks use Cursor's account service without starting an inference request.

### Automatic repository sync

Compose runs Kubernetes `git-sync` in a separate container with write access to the checkout volume. For each managed project it refreshes the remote default branch with full history, using the existing Assay reviewer credential, and atomically switches the `current` symlink. The reviewer mounts the volume read-only. Allow repository contents read access for the GitHub App or GitLab repository read access for its token. The sync interval defaults to 120 seconds (`REVIEW_SYNC_SECONDS` on the sync service). Polling uses the forge's PR/MR diff directly. When a requested review needs a local static scan and its commit is missing locally, the reviewer uses git-sync to fetch just that change ref into a temporary checkout, verifies its SHA, and discards it after scanning. A moved or unavailable ref is reported as incomplete coverage.

In `REVIEW_PROJECTS`, use `"managed": true` in place of a checkout path for a managed clone, or keep your existing absolute `checkout` and bind mount. With Compose, mount custom checkout paths in both services. The sync service leaves operator-managed checkouts alone. Private repositories retain a local copy of their history on the checkout volume.

## Request a review in a PR or MR

Post a new comment containing just `/review` on an open GitHub PR or GitLab MR. `/review-mid` and `/review-max` ask for a deeper review at 2 and 10 times the configured token limit; see [Review tiers](#review-tiers). The service polls the configured projects every two minutes by default. It accepts new requests from the human accounts you configure and posts the result on the reviewed head. Setup takes GitHub or GitLab usernames and stores their numeric account IDs. A poll uses zero model tokens. Requests run one at a time with the selected tier’s budget and the shared cache.

The helper lists open changes on every cycle and reads their conversations, including changes with no new commits, but no longer all of them on every cycle. A change the poller has not read before is read at once. After that it is read again after the poll interval, and each complete read that finds nothing new and sees the same number of comments and the same newest comment doubles that wait, up to `REVIEW_POLL_IDLE_MAX_SECONDS` (900 seconds by default, set as an environment variable on the reviewer). A change whose comments changed since the previous read, or that just had a request, goes back to the poll interval. A value no larger than the poll interval turns this off: every change is read on every cycle, and the saved waits are dropped at that start. A new `/review` on a quiet change is picked up on the next cycle that reads it. The change is due within 15 minutes by default, and the time to detect the request also includes any time spent running the previous cycle, including queued reviews, and waiting for the next cycle to start. An incomplete read is repeated on the next cycle and never lengthens the wait. The schedule is stored in the state volume, so a restart keeps it. Each cycle logs how many changes it read and how many it left for later. Set `REVIEW_POLL_IDLE_MAX_SECONDS` equal to the poll interval to read every open change on every cycle, as before. The poller skips old commands, edits detectable from the forge timestamps, bot responses and ordinary messages. For GitLab, an otherwise eligible command gets a single [GitLab note lookup](https://docs.gitlab.com/api/notes/#retrieve-a-merge-request-note), using the configured GitLab reviewer credential and instance. Internal and system notes cannot start a review.

Each project starts watching at its first activation. Keep the state volume across restarts to pick up requests posted during downtime on still-open changes. Queued reviews survive a restart. So does a review that was running when the poller stopped: the next start queues it again. A request's row in the queue (`<state>/requests.sqlite3`) keeps its status, how often it was started, the head its review resolved and any prepared report still awaiting publication. The review writes that head to the row before the model is called, and writes how it ended to the row before it exits. A repeat is held to the head in its row. If the change has moved to another head, the repeat is not reviewed: the request is marked failed, with a line in the poller log naming the change and the comment, and needs a fresh `/review`. If an interrupted review had already prepared its report, the next attempt submits the same report without invoking the model again. An identical comment is suppressed. A request is started at most twice. One that was running when the poller stopped for the second time is marked failed instead, with the same log line, and needs a fresh `/review` to run again. A review that was running when a version that kept no head stopped is marked failed as well, because the head it started at is unknown. A review that finished after its poller was killed keeps its own outcome, so a restart never repeats a review that ran but could not publish its result; post `/review` again to retry it. Duplicate reads do not incur another model call. A deleted comment or a change closed between polls may never be observed. For a quiet change that is the time until its next read, up to `REVIEW_POLL_IDLE_MAX_SECONDS`, so a `/review` posted on it and followed by a merge or close within that time is not seen.

While a request is queued or running, the reviewer marks it on the forge so the requester can see that it was accepted: on GitHub with an `eyes` reaction on the `/review` comment, on GitLab with an `hourglass` award emoji on the merge request. The poller removes the marker after the review finishes. On GitLab the award belongs to the merge request and stays while another request on it is waiting. If the review could not be completed or published, the reviewer also posts one ordinary comment that says the request was not completed, links the request it answers, and says that a new `/review` retries it.

The poller keeps nothing about a marker. Whether a request is marked is the forge’s to say: marking twice does no harm, and to remove a marker the poller asks the forge for its own one and deletes it. So a poller that stopped at any point, or an answer that never arrived, leaves nothing out of step. A request is marked after the read of the forge in which it was found, before any review starts; one that was queued when a poller stopped, or was waiting when a version without markers was upgraded, is marked the same way at the next start. What a finished request still owes, its marker’s removal and after a failure its notice, is stored with the queue until done. Each of the two is tried again while the poller runs and after a restart: after a minute, then after twice as long each time up to an hour, and given up after twelve tries, about six hours, with a log line. They are counted apart, so a removal that keeps failing cannot use up the notice’s tries, nor the other way round. A removal the forge refuses for good ends at once, with a log line unless the comment or merge request is simply gone.

A forge that answers slowly does not hold the queue back. Every marker and notice call runs under a deadline: 20 seconds per cycle for the markers to write, 20 for what earlier requests still owe, 5 for a request’s marker just before its review if it has none yet, and 20 for its removal and notice right after. After a failed review the notice goes first, then the removal. A removal or notice of which nothing could start in time is done in a later cycle and is not counted as a failed try; once any call of it started it counts, even if the time ran out partway. On GitHub, asking for the reaction can write it, so its deletion gets five seconds more when the time is up. Each request to the forge API is given up when its time is over, also when the forge keeps the response open by sending a little at a time; while such a request is still open no other is started to that forge, which is not counted as a try, so it costs one connection and not one per call, and other forges are not held up. Known limit: such a request cannot be stopped from outside, so for as long as a forge keeps one open, the markers and removals for that forge wait, without being counted as tries, and are made once it ends. Reviews, notices and other forges go on meanwhile. One call is never cut short: the helper run that publishes a failure notice starts only within its deadline and then has its usual 45 seconds, because stopping it just after the forge took the comment would post the notice again later.

The `pr-agent-forge` helper has no reaction operation, so the marker is the poller’s one direct write to a forge API: it uses the reviewer credential against the GitHub or GitLab REST API, with the same limits as the note lookup. The failure comment goes through the helper like a review, on the change’s current head, once per request and head. For the marker the GitHub App needs **Issues** read and write permission, which the manifest that setup creates does not request yet; add it to the App and accept it on the installation before the marker appears. On GitLab the award needs only project membership, so the Reporter token that setup creates is enough. A marker that cannot be added or removed produces a log line per attempt, with the HTTP status or the kind of error, within the bounds above, and nothing more: the request is neither dropped nor failed because of it. Without the permission that is one line per request.

Only one poller may use a state volume. A second poller on the same volume waits up to 15 seconds for the lock, which covers a poller that was just stopped and whose last review is still being ended, and then exits with `Another poller is already running on this state volume`, because it would otherwise take the first poller’s running reviews for interrupted ones and run them again. Stop the old poller before starting its replacement, as the Compose and Kubernetes deployments in this repository do. A queued review does not outlive its poller. The poller starts each review under a small supervisor process, which ends the review and every command the review started when the poller is gone, however it stopped, and when the review has run for 30 minutes. The supervisor, the review and its commands form one process group apart from the poller's, so a signal meant for the poller, from a terminal or an operator, reaches none of them directly, and the review's input is not the poller's. It asks first, so that the review removes its temporary files, and ends it outright five seconds later. The supervisor and the review hold the lock as well, so a replacement poller starts only once the interrupted review is gone, and then queues its request again. In the container the poller is the first process, so stopping it stops everything in any case; the supervisor matters where the poller runs outside one. The state volume must support `flock`: a poller that cannot take the lock exits with `Polling lock:` and the reason. Versions before the lock do not take it, so stop a poller of such a version completely before the first start of this one. Started beside it, this version would mark the review the old poller is running as failed.

### Request a visual review

Post a new comment containing just `/visual-review`. The same accounts, polling, queue, cache and head check apply. Where `/review` hunts for defects, a visual review shows a reviewer where to spend attention. The idea and the three-part layout come from THORChain's [visual-review](https://gitlab.com/thorchain/misc/visual-review), an agent skill that publishes a page with narrated video. This is an independent implementation for one forge comment, with no video, page or host, so nothing is published outside the forges.

The comment has three parts.

1. **Where the diff is**, at zero model tokens. Every changed line is counted by kind, in the same order on every comment: Logic, Schema (migrations and interface definitions), Tests, Config & CI, Docs, Dependencies (lockfiles), Generated and Fixtures. A file's kind follows its path, except Generated: apart from vendored third-party directories, a file is Generated only when its patch shows a generated-code header in its leading comment block (Go's `// Code generated ... DO NOT EDIT.`, `@generated`, protoc's notice), or, for a `.min.js` or `.min.css` file, minified lines. A generated-looking name alone, such as `query_string.go`, is read; so is a generated file whose header the patch does not include, at the cost of its hunks in the budget. Each file kept from the model by its own header is named in the comment. The headline number is **lines to read**, the Logic and Schema lines. Under it are the largest files to read and the tests that changed. A deleted file or a binary is counted and does not count against coverage.
2. **What changed**: a summary of one or two sentences and at most eight one-line changes, each naming its files.
3. **What to check**: at most six places, ranked `verify`, `careful`, `look`. A place says where to look, not that the code is wrong. Each cites head lines the model was shown; a citation outside the shown hunks or the change fails the whole answer.

The reading of the diff is labelled AI-generated. Paths and model text are quoted as code, as for `/review`, and the static checks and Qualgen advisories follow in a collapsed section.

A visual review runs the same scanners, Qualgen history and head-bound publication as `/review`, and makes at most one provider call. The call has its own budget, `REVIEW_VISUAL_MAX_INPUT_TOKENS`: **12,000 estimated input tokens** by default, with the same precedence as the review budget (environment, saved setup, then the default) and the same `--max-input-tokens` override. Its instructions take about 470 tokens where the review rubric takes about 3,300, and each hunk is shown once instead of as a new-and-old pair, so the larger budget reads several times more of the change: a 342-line change that `/review` estimates at 13,300 tokens estimates about 11,100 as a visual prompt. Dependencies, generated code, fixtures and licence files are counted in the map and their hunks are never sent to the model; each costs only its one line in the prompt's file inventory. Over the limit the comment still carries the map and says the model was skipped. `--estimate` and the cache behave as for `/review`, and the two commands never share a cache entry. A visual review has no tiers, no context beyond the patch and no findings, so `--visual` takes neither `--tier` nor `--max-findings`; the queue records it beside the tiers, as the kind of review a request asked for.

### Docker setup

The image includes all three provider CLIs, git-sync, Qualgen, the Go forge helper, ShellCheck, actionlint, Hadolint, zizmor and OSV-Scanner. Use an **amd64 Linux** runner or Docker’s amd64 emulation. To build from source:

```sh
docker build --platform linux/amd64 -t assay-pr-agent .
```

Add `--build-arg REVIEW_BUILD_SHA=$(git rev-parse HEAD)` to include the agent commit in the startup banner. The Dockerfile pins the Assay source revision used to compile the helper. To build the helper against a local Assay checkout, run `./forge-helper/build.sh /path/to/assay /absolute/path/on/PATH/pr-agent-forge`.

### Interactive setup

Run the setup command once with a writable state volume and your checkouts mounted at the paths the reviewer will use:

```sh
docker run --rm -it --platform linux/amd64 \
  -v review-state:/state -v /path/to/checkouts:/repos:ro assay-pr-agent setup
```

Enter the projects, checkout paths inside the container, visibility, the GitHub or GitLab usernames (or numeric IDs) allowed to request reviews, and the responsible operator’s username. Setup looks the numeric account IDs up on the forge; GitLab lookups use the reviewer token. With both forges configured, enter the operator as `username:numeric-id`. Setup asks for the selected authentication method and credentials for the forges you selected. For GitHub, have the App ID, installation ID and private PEM ready. For GitLab, have the reviewer token ready. You can accept the defaults of 120 seconds between polls, 6,000 estimated input tokens per review, 12,000 per visual review and the selected CLI’s default model.

For each credential, choose **paste** to enter it with terminal echo disabled, **import** to copy a readable file into the volume, or **mount** to keep using an external file. For import or mount, add the source volume to the setup command (for example, `-v /path/to/secrets:/run/secrets:ro`) and enter the file’s absolute path **inside** the container. With **mount**, provide the same file path when running the service. With **import**, the source mount is only needed during setup.

Start the reviewer with the same volumes. No configuration environment variables are required:

```sh
docker run --rm --platform linux/amd64 \
  -v review-state:/state -v /path/to/checkouts:/repos:ro assay-pr-agent
```

The active settings and imported or pasted credentials are in `/state/setup/`. Credential files are plaintext, owner-readable and writable (0600), inside a private directory (0700). Protect the volume and its backups. Setup checks local files and credential formats; Cursor team selection requires account service access. The poller checks forge access during normal operation.

To change one setting or rotate a credential after the first save, open the installer again; the page starts on the settings described under Install with Docker Compose. To re-run the full wizard, click **Run the setup wizard** there, or run `docker exec -it <container> cursor-review-setup` for the terminal version. Existing settings and credentials are kept without displaying their values. The Compose workers reload on save. For plain Docker, restart the sync and reviewer containers. Cancelling before the save keeps the previous configuration active. An explicit environment setting takes precedence over its saved value.

### Standalone Docker credentials

Mount the checkout for each project read-only and a writable volume at `/state` for the request queue, cache and publication receipts. The container runs as UID 10001. Supply the credentials and project settings at startup or save them with the setup wizard. The helper needs no Assay installation or home-directory settings.

| Docker setting | What to supply |
|---|---|
| `CURSOR_API_KEY` | Cursor API key for headless inference. |
| `REVIEW_OPERATOR` | Optional responsible operator’s forge `login:numeric-id` for the comment attribution check. The project’s `requester_ids` control who can request reviews. |
| `REVIEW_GITHUB_APP_ID`, `REVIEW_GITHUB_INSTALLATION_ID` | Numeric IDs for the GitHub App and the installation covering your repositories. Use a reviewer installation with access to read PRs and diffs and write PR conversation comments. |
| `REVIEW_GITHUB_PRIVATE_KEY` **or** `REVIEW_GITHUB_PRIVATE_KEY_FILE` | The App’s unencrypted RSA PEM or an absolute path to the mounted PEM. The value accepts actual line breaks or literal `\n`. |
| `REVIEW_GITLAB_TOKEN` **or** `REVIEW_GITLAB_TOKEN_FILE` | A dedicated GitLab reviewer token with API permission to read MRs and post notes, or an absolute path to the mounted token. |
| `REVIEW_GITLAB_BASE_URL` | HTTPS GitLab installation URL, including optional port and path prefix. An entry in `REVIEW_PROJECTS` can specify `gitlab_base`. |
| `REVIEW_GITLAB_CA_FILE`, `REVIEW_GITLAB_CRL_FILE` | Optional absolute paths to a PEM private CA bundle and PEM CRL. The corresponding variables without `_FILE` accept inline PEM. Supply the same paths to setup, sync and reviewer. |
| `REVIEW_WITHHELD_IDENTIFIERS` | Optional comma-separated identifiers to block in comments on internal and public projects. |
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
REVIEW_VISUAL_MAX_INPUT_TOKENS=12000
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

The agent uses mode-0600 runtime copies of the supplied credentials. In Docker these live in `/dev/shm`; incoming keys are excluded from the project settings and review database. Keep one service instance per state volume. The normal `/review` path uses GitLab’s token without rotating it. `REVIEW_MODEL` selects the Cursor model.

On an unconfigured interactive first start, the container runs setup before polling. If your deployment supplied credentials through a mounted Assay roster, move them into the setup wizard or the environment variables above.

**Upgrading from v0.1.0.** Stop the old receiver and start the polling image with the same state volume. It reads the existing project settings, request queue and cache. You can remove the forge webhooks, reverse proxy, published port, `REVIEW_PUBLIC_URL` and webhook secrets. The polling container only needs outbound connections.

After a rollback, the earlier polling image can resume regular reviews on the same state volume. A visual request still waiting then is not kept for the newer image: an image from before `/visual-review` marks it failed, and one from before the review tiers runs it as a plain `/review`. Post `/visual-review` again once the newer image is back.

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
cursor-review --repo /path/to/checkout --base main --visual --estimate
cursor-review --repo /path/to/checkout --base main --max-input-tokens 4000
cursor-review --repo /path/to/checkout --change 'org/repo#42' --estimate
cursor-review --repo /path/to/checkout --change 'org/repo#42' --model MODEL
```

The local form compares the merge base to committed `HEAD`, or to an explicit `--head`. Both commits and their common ancestor must be available locally. The forge form uses an `owner/repo#number` address from the configured projects and requires `pr-agent-forge` on `PATH`. GitLab subgroup projects can use CI mode with local Git objects. It reads metadata before and after the file list, checks head and file-count consistency, and records missing patches as incomplete coverage when the reviewer cannot establish that the change has no reviewable head-side text.

The prompt can include messages from up to 32 recent commits in a local or CI review, or a forge change's title and description when the forge helper reports them. The title and description are cut at 256 and 1,500 characters, labelled untrusted, and counted in the token estimate. The reviewer drops this optional context when doing so allows it to review more files within the token budget. Commit messages explain the history of a change. A complete forge description can guide checks for material discrepancies against the patch within the same cap as code findings. When the shown patch excludes changed files, the model needs visible contradictory lines to report a mismatch. The reviewer uses a truncated description for intent only.

For each code finding, the prompt asks for a concrete scenario, the behavior on the base and head, and the change that introduced, worsened or exposed the problem. It asks the model to mark incomplete comparisons as unverified. Mid and max batch the findings in their existing verification pass and fit the source excerpts into the tier's remaining token budget. The JSON and posted summary carry the verifier's confidence for retained findings.

### Cost and coverage

- `--estimate` makes zero Cursor calls. It performs the same diff preparation and static checks as a review.
- The limit is **6,000 estimated input tokens** by default for the complete user-supplied prompt. The saved setup's `REVIEW_MAX_INPUT_TOKENS` setting (asked during setup, or set as an environment variable) changes it; `--max-input-tokens` overrides both for one run. An oversized review is degraded instead of skipped: whole files leave the prompt until it fits, lock and minified files first, then tests, then documentation, then the remaining files from the largest down. The review reports `degraded`, the JSON lists the files in `degraded` with the full estimate in `full_input_tokens`, and a posted comment says `degraded` and names each file the model did not review (the first 50, then a count). Deterministic checks still cover those files. A review returns `over-budget` before invoking Cursor only when no single file fits. Repeat `--path path/to/file` to explicitly select a smaller scope. The JSON lists the omitted paths.
- `/visual-review` has its own limit, **12,000 estimated input tokens** by default, from `REVIEW_VISUAL_MAX_INPUT_TOKENS` in the same three ways. The hunks of lockfiles, generated code, fixtures and licence files never count against it; each such file costs one inventory line. It is one prompt: over its limit it is skipped as `over-budget`, and the comment still maps where the diff is.
- A regular review can call its provider **once**, with a 120-second timeout and at most `REVIEW_MAX_FINDINGS` accepted findings (six by default). Higher tiers can send additional prompts under the tier budget. The configured provider controls its own system context, output and billing. The report records usage supplied by each fresh pass and totals its numeric counters in `model_usage`.
- A malformed finding, a finding outside the shown hunks, or a finding beyond a pass's limit is dropped. The summary gives the count and reason without repeating the rejected text. When the model returned findings and none passed validation, the review reports `findings-dropped` and exits 2. An invalid review envelope still fails the whole pass.
- The prompt asks for a `relevant_code` quote of the cited patch lines. The reviewer checks each quoted line and drops a finding whose explicit quote contradicts those lines. Evidence is verified when the quote accounts for every non-blank cited line through exact quotes or standalone elisions such as `...`, and includes at least one line with three visible characters and a letter or digit. Raw quotes may shift their overall indentation while preserving relative indentation. The first fenced code block is the fallback when there is no explicit quote. Incomplete, trivial or missing quotes and quotes that exceed the verification work budget leave the finding unverified and are counted in the summary.
- Successful results are cached under `~/.cache/cursor-pr-review` (or `XDG_CACHE_HOME`). The container image sets `XDG_CACHE_HOME=/tmp/.cache` because its root filesystem is read-only, and its poller passes `--cache-dir /state/cache` for the reviews it runs. Identical evidence, scope identity, model selection and prompt reuse the result. The lock serializes duplicate local requests. For CI reuse across jobs, provide a persistent, trusted `--cache-dir`. Each ephemeral runner otherwise starts cold. Use an explicit model for reproducible comparisons.
- JSON keeps scanner findings separate from model findings. A defect review lists whole-file deletions and files with no reviewable head-side text under `not_reviewable`. A missing forge patch counts as incomplete coverage unless the reviewer verifies an unchanged rename, an empty head blob, or a binary head blob (a NUL byte in its first 8000 bytes, with the default diff rule verified from readable attributes). An unchanged rename has the same blob and mode at the old and new paths. An inferred base requires complete history. A checkout-local attributes file makes binary classification inconclusive. Missing or unreadable objects and paths outside a `--path` selection are listed under `omitted`. Missing scanner evidence stays visible even when the model finds nothing.
- Exit 0 means the requested full review/estimate completed, including a review with findings. Exit 2 means partial or degraded coverage, budget refusal, a model/tool error, a model answer whose findings were all dropped (`findings-dropped`) or a publication failure. Inspect `state`, `checks` and `findings` when using this in CI.

### Review tiers

`--tier` (or the comment command) sets the depth. Each tier's whole budget is a multiple of the configured limit, `REVIEW_MAX_INPUT_TOKENS` or `--max-input-tokens`, summed over the prompts prepared for its passes. A max review uses the same first prompt as mid when that prompt fits.

| Tier | Command | Budget | Model calls | What the model sees |
|---|---|---|---|---|
| `regular` | `/review` | 1× | 1 | The patch and scanner evidence. Up to `REVIEW_MAX_FINDINGS` findings. |
| `mid` | `/review-mid` | 2× | up to 2 | The patch and scanner evidence, together with context from outside the patch: the change description (or its commit messages), what the base changed in the same files, 25 lines around each hunk, and where the changed functions and types are used. Up to `REVIEW_MAX_FINDINGS_MID` findings, then a verification call that sees the findings, their patch context and nearby base and head lines when available. |
| `max` | `/review-max` | 10× | up to 3 | The mid prompt, unchanged when it fits, so a cached mid result is reused. An independently budgeted deep pass can include files that the first pass left out, plus the whole changed source files, 15 lines around each use of a changed name, existing tests for the touched modules, and eligible earlier comments. Then verification of the findings across the completed passes. The report shows up to `REVIEW_MAX_FINDINGS_MAX` findings and prioritizes findings from the deep pass when the cap applies. |

Code context comes from Git objects at the reviewed head. Earlier comments come from configured review requesters. The reviewer uses only visible GitHub comments and public GitLab notes. The reviewer sends both as untrusted data, and every finding must cite patch lines. It truncates or omits context to stay within the budget and reports each block’s status. The delivered tier is the highest whose context reached the model in a completed pass: mid needs a mid-tier block, and max also needs a block that only the deep pass adds. A block planned for a pass that failed is reported as `not sent`, and a review that reached no model reports no delivered tier. If the requested tier cannot be delivered, a planned pass fails, or findings cannot be verified, the review reports `degraded` and keeps available findings.

Each tier's findings cap is a setting: `REVIEW_MAX_FINDINGS` for regular, `REVIEW_MAX_FINDINGS_MID` for mid and `REVIEW_MAX_FINDINGS_MAX` for max, each an integer from 1 to 12 and 6 by default. The container environment overrides the saved setup, which the settings page edits, and `--max-findings` overrides the requested tier's cap for one run. Each review pass keeps the first valid findings up to its limit. A max review's first and deep passes ask for the mid cap, and the report keeps up to the max cap across them. The cap is part of the prompt text, so a cached answer is reused only under the same cap. The upper bound of 12 keeps every finding in the 16 KiB comment even when each is shortened to its compact form and written in four-byte characters.

The first prompt of a mid or max review can use up to 1.75 times the configured limit. Max planning reserves half the configured limit for verification. Verification can use all remaining tier tokens once the review prompts are sized. The first call may run for 240 seconds, the deep pass for 480 and verification for 120. Every provider reads its prompt on standard input. The JSON includes `tier`, `delivered`, `base_input_tokens`, `passes`, `context` and `verification`. The `max_input_tokens` field is the tier’s whole budget. Any allowed requester can use any tier.

### Static analysis

ShellCheck and actionlint run on the reviewed head’s Git blobs in a temporary directory. The runner uses stdin and the tool defaults. To avoid duplicate shell findings, actionlint’s embedded ShellCheck integration is disabled. Standalone ShellCheck covers changed `.sh` and `.bash` files. If a forge head is absent locally, an applicable scanner reports `could-not-check`. For modified files with an available immutable base, the same tool checks the base blob when a head finding maps to unchanged source. The JSON and model evidence keep the head diagnostics and mark matches with `baseline_match`. A complete review groups matches that were shown to the model. The comment lists each match the model could not see. A matched warning can still lead to a model finding if the patch changes its reachability or impact. A failed or unavailable comparison leaves each head diagnostic visible on its own. The extra scan uses local runtime only.

To reuse Assay’s Go linters, pass `--go-report path/to/report.json` from its [pinned analysis action](https://github.com/medici-finance/assay/tree/main/actions/go-analysis). Supply an artifact from a trusted producer. The runner checks the `go-analysis/v1` schema, source SHA, coverage count, totals and gate, then imports new and worsened findings for the selected files. Scanner diagnostics outside the shown hunks remain in the report. Only diagnostics located in the supplied patch consume model context.

### Use the Go report in a comment-triggered review

In container setup, enter the project's trusted Go analysis workflow or job. Alternatively, add the optional `go_analysis` field to its entry in `REVIEW_PROJECTS`. Leave it blank for projects without an Assay producer. Only enable a producer whose CI definition and artifact publication your team controls. A contributor who can replace the producer could also fabricate its findings.

| Project | Example setting | CI setup |
|---|---|---|
| GitHub | `"go_analysis": "go-lint.yml"` | Use the workflow filename or numeric ID. Run the [Assay action](https://github.com/medici-finance/assay/tree/v1.0.94/actions/go-analysis) on the PR source head. Give the existing reviewer App **Actions: read**. The worker reads the latest run of that workflow for the source SHA and its exact attempt's `go-analysis-lint-<sha>-<run-id>-<attempt>` artifact. |
| GitLab | `"go_analysis": "go-lint"` | Use the exact job name in the MR head pipeline in the target project. Run the Assay scanner against the MR source head with `GITHUB_REPOSITORY="$CI_PROJECT_PATH"` (the existing report format reads this variable). Have that job publish the sanitized report at the artifact root as `report.json`, using `artifacts: {when: always, paths: [report.json]}`. The reviewer needs access to read the pipeline, job and artifact. |

The worker looks up the report only when processing an authorized `/review`. It accepts a completed scan from a failed lint job, verifies the repository and analyzed source SHA, then imports new and worsened findings for changed paths. Findings inside the shown hunks share the existing Cursor prompt and token budget. The summary includes imported findings outside the hunks as well.

If CI is still running or the report is missing, expired, unreadable, invalid or for a different head, the summary marks Go analysis as `could not check`. The rest of the review proceeds under the selected tier’s budget. A new `/review` after CI finishes can pick up the report. Ordinary polls and CI completion never trigger a model call.

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

Qualgen uses local CPU, disk and Git history. Its advisories go into the review prompt as untrusted data, at most 12 entries for files with reviewable hunks in the prompt, with each detail cut at 300 characters, so they count toward the token estimate and are part of the model cache key. The reviewer fits the patch first. In higher tiers, the advisories share space with optional context while keeping each included context block and preserving the token allowance for verification. The model checks these hints against the patch. The advisories are also added to the posted summary, and an estimate, an over-budget review or a model failure still reports them.

### Base drift

Every review compares the change with the tip of its base, using local Git objects and no model tokens. The JSON `drift` object and the posted summary give the number of base commits the head lacks, the changed files that the base has also changed since the branch point, and stale references: a name quoted in backticks on an added documentation line (`.md`, `.rst`, `.txt`, `.adoc`) that was in the tree at the branch point and is no longer anywhere on the base. A merge can be textually clean and still leave such a sentence describing something the base removed. At most 60 names are looked up per review and the comment shows 10 rows of each kind.

Local reviews use the requested base. GitHub CI uses the event’s base SHA. GitLab CI reports drift when it supplies a target-branch SHA. A forge review resolves the forge's base branch name as `origin/<branch>` or a local branch in the checkout. A managed checkout has neither, so its synced `HEAD` is reported as a default-branch comparison when the target ref is unavailable. For a managed project the reviewer now fetches every reviewed head, 50 commits deep, into a temporary directory; before, it fetched one commit and only when a scanner needed the source. A change whose branch point is more than 50 commits behind its head, or whose base or head is not available locally, reports `Base drift could not be checked`. That does not change the review state or exit code.

### GitHub Actions and GitLab CI

To run the CLI explicitly in a PR or MR pipeline, use the pipeline event's commit references. This command invokes a review. Use the polling service above for comment-only operation:

```sh
cursor-review --ci --max-input-tokens 4000 > review.json
```

GitHub uses a `pull_request` event and its recorded base and head SHAs. Fetch sufficient history, for example `fetch-depth: 0` with `actions/checkout`. GitLab uses a merge request pipeline and `CI_MERGE_REQUEST_DIFF_BASE_SHA`. It uses the source branch SHA for merged-results pipelines and the commit SHA for detached MR pipelines. Configure `GIT_DEPTH: '0'` or otherwise supply those Git objects. Keep the executable and its environment in a trusted install separate from the change under review. In both systems, save `review.json` as a job artifact even on exit 2.

### Publish a summary

Add `--post` to the `--change` command to post the reviewed commit and findings as a single ordinary comment. The helper checks visibility and the current head before publishing under the configured reviewer credential. The agent stores a receipt on its state volume to suppress identical retries for the same head, including after restart. A failed post can be retried with the same model and cache without paying again for an unchanged prompt. No write happens by default.

Every posted summary includes a receipt with model calls, cache status, prompt estimates, reported token usage, and the counts of findings and completed checks. The coverage percentage uses confirmed text patches across the whole change. The receipt also reports unavailable patches and files skipped for budget, outside the path selection, or counted without showing their hunks. The same numbers are in the JSON report under `receipt`.

The standalone poller supports `/review` and `/visual-review`. For additional upstream PR-Agent chat commands such as `/ask` and `/improve`, see [GitHub integration](https://docs.pr-agent.ai/installation/github/) and [GitLab integration](https://docs.pr-agent.ai/installation/gitlab/). GitLab pipeline jobs alone do not receive comment events. The upstream comment server and this CLI are separate entry points.

### Export SARIF

Add `--sarif review.sarif` to any review or estimate to write the same findings as a [SARIF 2.1.0](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html) log after the JSON report is printed. The log has one run whose driver is `assay-pr-agent` and one result per model finding and per ShellCheck, actionlint, Go analysis, hadolint, zizmor or OSV-Scanner finding. `ruleId` is the scanner rule or `model-finding`. The report keeps no scanner severity, so `level` is a fixed mapping: `warning` for model findings and OSV-Scanner vulnerabilities, `note` for every other scanner finding. Each result names a repository-relative, percent-encoded `artifactLocation.uri` and, when the finding has a line, `region.startLine`, plus `region.endLine` for a model finding. OSV-Scanner findings carry no line, so they name the lockfile without a region. Messages are cut at 1,000 characters and a finding without a safe relative path is left out. `partialFingerprints.primaryLocationLineHash` starts with the same line hash that the `upload-sarif` action computes: a hash of the 100 characters from the start of the finding's line, ignoring spaces and tabs, so the text that follows the line is part of the value, with a number that keeps identical lines apart. A suffix based on the finding’s full message and span length distinguishes results on the same line independently of their order. A rerun that rephrases the finding can produce a new fingerprint because the model report has no stable issue ID. Findings the log cannot tell apart, such as two ShellCheck SC2086 on one line with one message, keep one identity. A line added above a finding leaves its fingerprint unchanged if the hashed context and its occurrence count stay the same. The text is read from the reviewed head in `--repo` or from the verified temporary checkout for a managed `--change` review. When the reviewed source is unavailable, including files larger than 1 MB, the fingerprint uses the line number, span length and full message. A change in source availability or line number can change that fingerprint. A finding without a line is identified by its path, rule and message. `invocations[0].executionSuccessful` is `true` only for the `reviewed`, `empty` and `estimate` states. The path is resolved against the directory you run the command from, not `--repo`. If the file cannot be written, the JSON report is still printed, the reason goes to stderr and the exit code is 2. The poller does not request the export.

GitHub code scanning accepts SARIF 2.1.0 files from third-party tools. It is available for public repositories on GitHub.com and for organization-owned repositories with GitHub Code Security enabled. The job needs `contents: read` to check out the source. Uploading with `upload-sarif` needs `security-events: write` and, for private repositories, `actions: read`. Actions skips the steps after a failed one, and `cursor-review` exits 2 for partial coverage, an over-budget prompt or a failed model, so the upload states that it also runs after a failure when the file exists. In a `pull_request` workflow, check out the source head with full history so annotation lines match the commit reviewed by `--ci`. Both examples remove old report files before the review so a reused workspace cannot upload a stale log.

```yaml
    permissions:
      contents: read
      security-events: write
      actions: read
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          fetch-depth: 0
          persist-credentials: false
      - run: rm -f review.sarif review.json
      - id: review
        run: cursor-review --ci --max-input-tokens 4000 --sarif review.sarif > review.json
      - uses: github/codeql-action/upload-sarif@v4
        if: ${{ !cancelled() && (steps.review.outcome == 'success' || steps.review.outcome == 'failure') && hashFiles('review.sarif') != '' }}
        with:
          sarif_file: review.sarif
```

GitLab ingests SARIF 2.1.0 from an `artifacts:reports:sarif` artifact on GitLab Ultimate: generally available in GitLab 19.2, and from 18.11 with the `sarif_ingestion` feature flag, which is on by default from 19.1. The findings appear in the pipeline Security tab and in the merge request security widget. GitLab deduplicates findings with the same rule, path and line range. Exact-span model findings with the same `model-finding` rule and multiple occurrences of one OSV advisory in a single lockfile can therefore appear as one entry. Use `review.json` for the complete result list. For this example, use a detached merge request pipeline so the working tree matches the reviewed source head. GitLab ingests the findings only when the job succeeds, and `cursor-review` exits 2 for partial coverage, an over-budget prompt or a failed model, so keep that exit status from failing the job and read the outcome from `review.json`:

```yaml
review:
  rules:
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event" && $CI_MERGE_REQUEST_EVENT_TYPE == "detached"'
  variables:
    GIT_DEPTH: "0"
  script:
    - rm -f review.sarif review.json
    - cursor-review --ci --max-input-tokens 4000 --sarif review.sarif > review.json || review_exit=$?
    - test -s review.sarif
  artifacts:
    when: always
    paths: [review.json]
    reports:
      sarif: review.sarif
```

The test suite checks the log's contents and never uploads it to a forge. [Upload a SARIF file to GitHub](https://docs.github.com/en/code-security/code-scanning/integrating-with-code-scanning/uploading-a-sarif-file-to-github) · [SARIF support for code scanning](https://docs.github.com/en/code-security/code-scanning/integrating-with-code-scanning/sarif-support-for-code-scanning) · [GitLab SARIF reports](https://docs.gitlab.com/user/application_security/detect/sarif/)

### Report a check on the head

Add `--forge-status`, or set `REVIEW_FORGE_STATUS=1` in the reviewer's environment, to report a review of a `--change` on the commit it reviewed. It is off by default, works with or without `--post`, and never runs for `--estimate`. The poller passes its environment through to each review, so it needs no change to pick the variable up.

GitHub receives one check run named `assay-pr-agent/review` on the reviewed head. Its conclusion follows the exit code: `success` when the review completed (`reviewed` or `empty`), and `failure` when the model could not complete the review, when the prompt was over budget or when coverage was partial or degraded. Findings alone never fail the check, because the CLI also exits 0 for a review with findings. The title and summary hold fixed sentences and counts. Each model finding with a line also becomes a `warning` annotation, with its title and message quoted as code so nothing a change author or the model wrote can render as a link or a mention.

GitLab receives one commit status with the same name on the reviewed head: `success` or `failed` by the same rule, with a fixed description of at most 255 characters and the change as its target. The status targets the reviewed SHA. When the merge request's head pipeline belongs to the configured project and ran that SHA, the status names that pipeline, so it appears on the merge request even if a branch pipeline exists for the same commit. With no head pipeline it names the source branch and GitLab selects or creates the pipeline. When the head pipeline ran another commit, as merged-results and merge-train pipelines do, or cannot be named from the configured project, no status is sent and `forge_status` is `failed`: a status sent with the branch alone could start a new pipeline that GitLab shows in place of the real one. Status publication is limited to merge requests whose source and target use the configured project. When GitLab answers 409 because another status for the commit is being written at that moment, the reviewer asks twice more, after one and two seconds.

The forge helper checks the current head, repository visibility and status text against the same publication policy used for comments before either status is sent. The outcome is in the JSON report as `forge_status`: `posted`, `failed` or `skipped`. A refusal, a missing permission included, logs one line without the credential and changes neither the exit code nor the summary comment. When `--post` itself fails, no status is attempted and the report has no `forge_status` key.

The default reviewer credentials cannot post it, and setup still requests the least privilege. Creating a check run takes a GitHub App with **Checks** write permission (`checks: write`); the wizard requests only contents read, pull requests write and Actions read. A commit status takes a GitLab token with the `api` scope and the Developer role; the wizard creates a Reporter token. Add the permission to the reviewer credential before turning this on.

GitHub identifies the check by commit SHA. Pull requests that share a head can share its result, and retargeting a pull request can change the diff without changing the check. Use it as a commit-level status. A merge gate that requires a review of the current pull request diff needs a separate check of the base and head. [REST API endpoints for check runs](https://docs.github.com/en/rest/checks/runs#create-a-check-run) · [GitLab commit statuses](https://docs.gitlab.com/api/commits/#set-commit-pipeline-status)

## Verification

```sh
uv run --frozen python -m unittest discover -s tests -v
```

The fixtures exercise GitHub and GitLab polling, restart handling, the Assay forge envelope/command contract, caching, the budget cutoff, incomplete inputs, Cursor failure handling, and the visual review's map, prompt, answer checks and comment. Install ShellCheck to run the integration fixtures. Tests substitute a local executable for inference and never post externally.

Before proposing a change to this repository, coding agents follow [AGENTS.md](AGENTS.md), which includes running this reviewer's own prompt on the branch.

## License

This repository is licensed under the [Apache License 2.0](LICENSE). The license covers this project's own source. Third-party software that the image downloads or bundles keeps its own license and terms: the provider CLIs, PR-Agent, Assay’s forge library and Qualgen, and the scanners. The `/visual-review` command takes its format from THORChain's MIT-licensed [visual-review](https://gitlab.com/thorchain/misc/visual-review); none of its code is included.
