FROM ghcr.io/medici-finance/assay/desk-tools:v1.0.91 AS assay
FROM ghcr.io/astral-sh/uv:0.9.26 AS uv
FROM rhysd/actionlint:1.7.7 AS actionlint
FROM python:3.12-slim-bookworm
ARG TARGETARCH
RUN test "$TARGETARCH" = amd64 || (echo 'Assay release requires --platform linux/amd64' >&2 && exit 1)
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates openssl curl git gh shellcheck bash gzip tar && rm -rf /var/lib/apt/lists/*
COPY --from=assay /usr/local/bin/deskmonitor /usr/local/bin/deskread /usr/local/bin/deskpost /usr/local/bin/desktoken /usr/local/bin/
COPY --from=actionlint /usr/local/bin/actionlint /usr/local/bin/actionlint
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /opt/reviewer
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable && useradd --create-home --uid 10001 reviewer && install -d -m 700 -o reviewer -g reviewer /state
ENV DESK_LOOP=pr-review-desk DESK_TOOLS_BIN=/usr/local/bin HOME=/home/reviewer PATH=/opt/reviewer/.venv/bin:/home/reviewer/.local/bin:$PATH REVIEW_STATE_DIR=/state
USER reviewer
RUN curl -fsSL https://cursor.com/install -o /tmp/install-cursor.sh && bash /tmp/install-cursor.sh && rm /tmp/install-cursor.sh && cursor-agent --version
VOLUME /state
ENTRYPOINT ["cursor-review-poll"]
