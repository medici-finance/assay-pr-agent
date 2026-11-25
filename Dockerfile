FROM ghcr.io/medici-finance/assay/desk-tools:v1.0.91 AS assay
FROM ghcr.io/astral-sh/uv:0.9.26 AS uv
FROM rhysd/actionlint:1.7.7 AS actionlint
FROM registry.k8s.io/git-sync/git-sync:v4.8.0 AS sync
FROM node:22-bookworm-slim AS models
RUN npm install --prefix /opt/model-clis @openai/codex@0.171.0 @anthropic-ai/claude-code@2.1.331
# git-sync uses the Git 2.46+ config subcommands.
FROM python:3.12-slim-trixie
ARG TARGETARCH
RUN test "$TARGETARCH" = amd64 || (echo 'Assay release requires --platform linux/amd64' >&2 && exit 1)
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates openssl curl git gh shellcheck bash gzip tar && rm -rf /var/lib/apt/lists/*
RUN curl --fail --silent --show-error --location --retry 3 \
      https://github.com/medici-finance/assay/releases/download/v1.0.91/qualgen-linux-amd64 -o /usr/local/bin/qualgen \
    && echo '835d4188bc3d5f8d7c9ccc9ca49a3f907101a78c659020442e7783463f2b975f  /usr/local/bin/qualgen' | sha256sum -c - \
    && chmod 0755 /usr/local/bin/qualgen && qualgen --version
COPY --from=assay /usr/local/bin/deskmonitor /usr/local/bin/deskread /usr/local/bin/deskpost /usr/local/bin/desktoken /usr/local/bin/
COPY --from=actionlint /usr/local/bin/actionlint /usr/local/bin/actionlint
COPY --from=sync /git-sync /usr/local/bin/git-sync
COPY --from=models /usr/local/bin/node /usr/local/bin/node
COPY --from=models /opt/model-clis /opt/model-clis
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /opt/reviewer
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable && useradd --create-home --uid 10001 reviewer && install -d -m 700 -o reviewer -g reviewer /state /repos
ENV DESK_LOOP=pr-review-desk DESK_TOOLS_BIN=/usr/local/bin HOME=/home/reviewer PATH=/opt/model-clis/node_modules/.bin:/opt/reviewer/.venv/bin:/home/reviewer/.local/bin:$PATH REVIEW_STATE_DIR=/state
USER reviewer
RUN curl -fsSL https://cursor.com/install -o /tmp/install-cursor.sh && bash /tmp/install-cursor.sh && rm /tmp/install-cursor.sh && cursor-agent --version && claude --version && codex --version && git-sync --version
VOLUME /state
ENTRYPOINT ["cursor-review-container"]
