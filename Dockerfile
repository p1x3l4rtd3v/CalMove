# CalMove web — stateless migration wizard (spec §10).
# No volumes, no writable state: tokens and event data live only in process RAM.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependency layer (cached independently of source changes).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --system --no-create-home calmove
USER calmove
ENV PATH="/app/.venv/bin:$PATH"

# Required at runtime:
#   CALMOVE_WEB_CLIENT_SECRET  path to the OAuth *web* client JSON (mount as a secret)
#   CALMOVE_WEB_BASE_URL       external HTTPS URL of this service
# Optional: CALMOVE_WEB_ROOM_MAP, CALMOVE_WEB_PORT (default 8080)
EXPOSE 8080
CMD ["calmove-web"]
