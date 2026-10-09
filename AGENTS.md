# Repository Guidelines

## Project Overview
CalMove migrates a Google Calendar between two Google Workspaces by re-creating every event
through the Calendar API — preserving attendees, booked rooms (resource attendees), recurrences,
and event settings that an ICS export/import would lose. Interactive Python CLI for macOS and
Windows. Authoritative design: `docs/SPECIFICATION.md`; binding rules: `CONSTITUTION.md`.

## Architecture & Data Flow
Clean architecture with a strict inward dependency rule — enforce it in every change:

| Layer | Package | May import |
|---|---|---|
| Domain | `src/calmove/domain/` | stdlib only |
| Application | `src/calmove/application/` | domain |
| Infrastructure | `src/calmove/infrastructure/` | application ports, domain, Google SDKs, keyring |
| Presentation | `src/calmove/cli/`, `src/calmove/web/` | everything (the two composition roots) |

Flow (CLI): wizard authenticates both workspaces (OAuth loopback, tokens in OS keyring) →
`GoogleCalendarGateway` fetches source events (`singleEvents=False`, paginated) →
`MigrateCalendarUseCase` partitions recurring masters/singles vs. recurrence exceptions, skips
already-migrated events (idempotency via `extendedProperties.private["calmove-source-id"]`),
creates masters first, then patches exception instances by `originalStartTime`. Failures never
abort the run; they land in `MigrationResult.failed`.

The application layer talks only to `Protocol`s in `src/calmove/application/ports.py`
(`CalendarReader`, `CalendarWriter`, `MigrationReporter`). Never import Google SDK types inside
domain/application.

The web presentation (`src/calmove/web/`) serves the same wizard as a stateless Flask flow
(spec §10): web-app PKCE OAuth per browser session, all state (gateways, calendar lists,
results) in the RAM-only `SessionStore` — never on disk, never in logs, never shared between
sessions. Single-process threaded server (waitress); the migration itself runs in a
per-session background thread.

## Key Directories
- `src/calmove/domain/` — frozen dataclasses (`model.py`), `DateRange` presets (`date_range.py`), error hierarchy (`errors.py`). Pure, no I/O.
- `src/calmove/application/` — `migrate_calendar.py` (the one use case), `ports.py`.
- `src/calmove/infrastructure/google/` — `auth.py` (OAuth + `KeyringTokenStore`), `gateway.py` (API adapter), `mapping.py` (pure API-dict ⇄ domain conversion).
- `src/calmove/cli/` — Typer wizard (`main.py`), prompt helpers (`prompts.py`).
- `src/calmove/web/` — Flask wizard (`main.py`), RAM-only `SessionStore` (`sessions.py`), `templates/`.
- `tests/unit/` — offline unit tests; in-memory fakes live in `tests/conftest.py`.

## Development Commands
```sh
uv sync --dev              # install (creates .venv)
uv run pytest              # tests
uv run ruff format .       # format
uv run ruff check .        # lint
uv run mypy src            # strict type check
uv run pip-audit           # dependency audit
uv run calmove migrate --client-secret path/to/client_secret.json [--dry-run]
uv run calmove-web         # web mode; needs CALMOVE_WEB_CLIENT_SECRET + CALMOVE_WEB_BASE_URL
```
All five checks (format, lint, mypy, pytest, pip-audit) are CI merge gates (`.github/workflows/ci.yml`, 3-OS matrix).

## Code Conventions & Common Patterns
- Python ≥ 3.12, line length 100, ruff rules `E,F,I,UP,B,SIM,S,RUF` (`S101` allowed in tests).
- `mypy --strict`; no `# type: ignore` without a justification comment. Untyped Google SDK calls are confined to `infrastructure/google/auth.py` via a scoped pyproject override — do not widen it.
- Domain/application dataclasses: `@dataclass(frozen=True, slots=True, kw_only=True)`; collections as tuples. Only `MigrationResult` is mutable.
- Error handling: domain errors subclass `CalMoveError`. Per-event writer failures are caught, recorded as `(event_id, message)` tuples, and the migration continues.
- Dependency injection by constructor: the use case receives reader/writer/reporter protocols; `cli/main.py` and `web/main.py` (`create_app`) are the only places concrete adapters are wired.
- No async — the Google client is synchronous; keep it that way.
- Mapping functions in `mapping.py` are pure and round-trip-tested (`event_from_api(event_to_api(e))` must invert).

## Important Files
- `src/calmove/cli/main.py` — entry point (`calmove = "calmove.cli.main:app"` in pyproject).
- `src/calmove/web/main.py` — web entry point (`calmove-web = "calmove.web.main:main"`); `create_app` takes injectable `oauth`/`gateway_factory` for offline tests.
- `src/calmove/application/migrate_calendar.py` — core algorithm (skip/create/exception ordering).
- `src/calmove/infrastructure/google/mapping.py` — field coverage lives here; extend it when supporting new event attributes.
- `pyproject.toml` — single source for deps + ruff/mypy/pytest config.
- `docs/SPECIFICATION.md` — update it *before* behavior changes (Constitution art. 7).
- `CONSTITUTION.md`, `SECURITY.md` — binding architecture and security rules.

## Runtime/Tooling Preferences
- **uv** is the package manager; do not introduce pip/poetry workflows. Build backend: hatchling, src layout.
- Python 3.12 pinned via `.python-version`.
- Secrets never enter the repo: `client_secret*.json`, `token*.json`, `rooms*.json` are gitignored. Tokens live only in the OS keyring (service `calmove`, roles `source`/`destination`).
- Source workspace scope is read-only (`calendar.readonly`); destination adds `calendar.events`. Never broaden scopes.

## Testing & QA
- pytest, offline-only: no live Google API calls in tests or CI. Exercise the use case against `FakeReader`/`FakeWriter`/`RecordingReporter` from `tests/conftest.py`.
- New behavior ships with tests; bug fixes ship a regression test (Constitution art. 4).
- Naming: `tests/unit/test_<module>.py`; failure injection via `FakeWriter(fail_ids={...})`.
- The gateway (`gateway.py`) stays deliberately thin precisely so everything above it is unit-testable — put logic in mapping/use case, not in the gateway.
