FROM golang:1.27.4-bookworm AS forge
ARG ASSAY_FORGE_COMMIT=273d62d941b34d8249a1e1889d838a3fc8e5010f
RUN git init -q /assay \
    && git -C /assay fetch --depth=1 https://github.com/medici-finance/assay.git "$ASSAY_FORGE_COMMIT" \
    && test "$(git -C /assay rev-parse FETCH_HEAD)" = "$ASSAY_FORGE_COMMIT" \
    && git -C /assay checkout --quiet --detach FETCH_HEAD
COPY forge-helper /src/forge-helper
RUN /src/forge-helper/build.sh /assay /out/pr-agent-forge
FROM ghcr.io/astral-sh/uv:0.9.26 AS uv
FROM rhysd/actionlint:1.7.7 AS actionlint
FROM registry.k8s.io/git-sync/git-sync:v4.8.0 AS sync
FROM node:22-bookworm-slim AS models
RUN npm install --prefix /opt/model-clis @openai/codex@0.171.0 @anthropic-ai/claude-code@2.1.331
# git-sync uses the Git 2.46+ config subcommands.
FROM python:3.12-slim-trixie
# pipefail: a failed download or checksum must fail the RUN, not hide behind a pipe.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG TARGETARCH
RUN test "$TARGETARCH" = amd64 || (echo 'Assay release requires --platform linux/amd64' >&2 && exit 1)
# System packages track the base image's Debian security updates; the tools whose
# exact versions matter are digest-pinned below.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates openssl haproxy curl git shellcheck bash gzip tar && rm -rf /var/lib/apt/lists/*
RUN curl --fail --silent --show-error --location --retry 3 \
      https://github.com/medici-finance/assay/releases/download/v1.0.94/qualgen-linux-amd64 -o /usr/local/bin/qualgen \
    && echo 'cc3cf51fb2ee2f03e9e704385b59ae4ea57757e6bd9b66e613fec2abd62ce01e  /usr/local/bin/qualgen' | sha256sum -c - \
    && chmod 0755 /usr/local/bin/qualgen && qualgen --version
RUN curl -fsSL --retry 3 https://github.com/hadolint/hadolint/releases/download/v2.15.1/hadolint-linux-x86_64 -o /usr/local/bin/hadolint \
    && echo 'c7187db94eeeeca956519a6af171adc31453941a1e777961f6e680f697c8c507  /usr/local/bin/hadolint' | sha256sum -c - \
    && curl -fsSL --retry 3 https://github.com/zizmorcore/zizmor/releases/download/v1.33.1/zizmor-x86_64-unknown-linux-gnu.tar.gz -o /tmp/zizmor.tgz \
    && echo '3e6a301c7a820a2501915f5a9de8c0486da82bdec7965f068477538a98743d43  /tmp/zizmor.tgz' | sha256sum -c - \
    && tar -xOzf /tmp/zizmor.tgz zizmor > /usr/local/bin/zizmor && rm /tmp/zizmor.tgz \
    && curl -fsSL --retry 3 https://github.com/google/osv-scanner/releases/download/v2.7.1/osv-scanner_linux_amd64 -o /usr/local/bin/osv-scanner \
    && echo 'fb7dab19a8bd9bbe5c3189f09d4b7ee1820c9553a23bac202648a56efd2f6ee8  /usr/local/bin/osv-scanner' | sha256sum -c - \
    && chmod 0755 /usr/local/bin/hadolint /usr/local/bin/zizmor /usr/local/bin/osv-scanner \
    && hadolint --version && zizmor --version && osv-scanner --version
COPY --from=forge /out/pr-agent-forge /usr/local/bin/pr-agent-forge
COPY --from=actionlint /usr/local/bin/actionlint /usr/local/bin/actionlint
COPY --from=sync /git-sync /usr/local/bin/git-sync
COPY --from=models /usr/local/bin/node /usr/local/bin/node
COPY --from=models /opt/model-clis /opt/model-clis
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /opt/reviewer
COPY pyproject.toml uv.lock LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable && useradd --create-home --uid 10001 reviewer && install -d -m 700 -o reviewer -g reviewer /state /repos /model-auth
# The root filesystem is read-only at runtime, so XDG caches default to the
# /tmp mount rather than $HOME/.cache. The poller passes --cache-dir /state/cache
# for its own reviews; this covers a hand-run `cursor-review` in the container.
ENV HOME=/home/reviewer XDG_CACHE_HOME=/tmp/.cache PATH=/opt/model-clis/node_modules/.bin:/opt/reviewer/.venv/bin:/home/reviewer/.local/bin:$PATH REVIEW_STATE_DIR=/state
USER 10001
# cursor.com/install always fetches the current release, so download that release's
# tarball by version and digest instead, in the layout the installer produces.
ARG CURSOR_AGENT_VERSION=2026.11.22-e9042b2
RUN curl -fsSL --retry 3 \
        "https://downloads.cursor.com/lab/${CURSOR_AGENT_VERSION}/linux/x64/agent-cli-package.tar.gz" -o /tmp/agent-cli.tgz \
    && echo '7663b1c5d1b56e095ef8e23bef38d66df6cfd665c4873470214e0e411f0f97b3  /tmp/agent-cli.tgz' | sha256sum -c - \
    && mkdir -p "$HOME/.local/share/cursor-agent/versions/${CURSOR_AGENT_VERSION}" "$HOME/.local/bin" \
    && tar --strip-components=1 -xzf /tmp/agent-cli.tgz -C "$HOME/.local/share/cursor-agent/versions/${CURSOR_AGENT_VERSION}" \
    && rm /tmp/agent-cli.tgz \
    && ln -s "$HOME/.local/share/cursor-agent/versions/${CURSOR_AGENT_VERSION}/cursor-agent" "$HOME/.local/bin/cursor-agent" \
    && test "$(cursor-agent --version)" = "${CURSOR_AGENT_VERSION}" \
    && claude --version && codex --version && git-sync --version
# Build record written by ci/publish-image.sh; local builds fall back to the build time.
USER 0
ARG REVIEW_BUILD_SHA=
ARG REVIEW_BUILD_TAGS=
ARG REVIEW_BUILD_TIMESTAMP=
RUN printf '{"sha":"%s","tags":"%s","built_at":"%s"}\n' \
        "${REVIEW_BUILD_SHA:-unknown}" "${REVIEW_BUILD_TAGS:-}" \
        "${REVIEW_BUILD_TIMESTAMP:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}" > build-info.json \
    && chmod 0644 build-info.json
USER 10001
VOLUME /state
ENTRYPOINT ["cursor-review-container"]
