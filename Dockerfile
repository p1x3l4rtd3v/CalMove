# CalMove web — stateless migration wizard (spec §10).
# No volumes, no writable state: tokens and event data live only in process RAM.
# Bases are digest-pinned (supply-chain); bump digests deliberately when updating.

# --- build stage: uv resolves the locked environment into /app/.venv ---------
# ghcr.io/astral-sh/uv:python3.12-bookworm-slim
FROM ghcr.io/astral-sh/uv@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 AS builder

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependency layer (cached independently of source changes).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# --- runtime stage: plain Python, no uv, only the venv -----------------------
# python:3.12-slim
FROM python@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv

RUN useradd --system --no-create-home calmove
USER calmove
ENV PATH="/app/.venv/bin:$PATH"

# Required at runtime:
#   CALMOVE_WEB_CLIENT_SECRET  path to the OAuth *web* client JSON (mount as a secret)
#   CALMOVE_WEB_BASE_URL       external HTTPS URL of this service
# Optional: CALMOVE_WEB_ROOM_MAP, CALMOVE_WEB_PORT (default 8080)
EXPOSE 8080
CMD ["calmove-web"]
