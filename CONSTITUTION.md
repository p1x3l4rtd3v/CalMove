# CalMove Constitution

Binding architectural rules for this repository. RFC 2119 keywords (MUST, MUST NOT, SHOULD,
MAY) are normative. See `docs/SPECIFICATION.md` for the full technical specification.

## Article 1 — Clean Architecture

1. The codebase is organized in four layers: **domain** → **application** → **infrastructure**
   → **presentation** (`cli`, `web`). Dependencies MUST point strictly inward.
2. Allowed imports per layer (identical to spec §3):

   | Layer | Package | May import |
   |---|---|---|
   | Domain | `calmove/domain` | stdlib only |
   | Application | `calmove/application` | domain |
   | Infrastructure | `calmove/infrastructure` | application ports, domain, Google SDKs, keyring |
   | Presentation | `calmove/cli`, `calmove/web` | application, domain, infrastructure (composition roots) |

3. The domain MUST remain pure: standard library only, no I/O, no third-party imports.
4. Google SDKs (`googleapiclient`, `google_auth_oauthlib`, `google.*`) MUST be confined to
   `calmove/infrastructure`.
5. `calmove/cli` and `calmove/web` are the **only** composition roots. No other layer MUST
   instantiate adapters or wire dependencies.

## Article 2 — Ports and Adapters

1. The application layer MUST depend exclusively on the `typing.Protocol` ports defined in
   `calmove/application/ports.py` (`CalendarReader`, `CalendarWriter`, `MigrationReporter`).
2. New integrations MUST be added as adapters in the infrastructure layer implementing an
   existing or new port.
3. SDK types (API dicts, credential objects, HTTP errors) MUST NOT leak inward past the
   infrastructure boundary; adapters translate them to domain types and domain errors.

## Article 3 — Immutability

1. All domain types MUST be frozen dataclasses (`@dataclass(frozen=True, slots=True,
   kw_only=True)`); collections inside them MUST be immutable (`tuple`, not `list`).
2. Global mutable state MUST NOT exist. State flows through function arguments and return
   values.

## Article 4 — Testing

1. Every use case MUST be tested against in-memory fake port implementations.
2. CI MUST NOT make live Google API calls; the gateway stays thin so everything above it is
   testable offline.
3. New behavior MUST ship with tests in the same change.
4. Every bug fix MUST include a regression test reproducing the bug.

## Article 5 — Code Quality

1. `mypy --strict` and `ruff` (lint **and** format) are merge gates; a change MUST NOT merge
   with violations.
2. `# type: ignore` MUST NOT appear without an adjacent comment justifying it.
3. Maximum line length is 100 characters.

## Article 6 — Security

1. OAuth scopes MUST be least-privilege: the source login uses `calendar.readonly` plus
   `calendar.events.owned`, whose write access MUST be used solely to append the new
   account's address to the guest list of invited events (invitation forwarding, spec §6);
   the destination login uses `calendar.events` + `calendar.readonly`.
2. CLI mode: tokens MUST be stored only in the OS keyring (service `calmove`) — never in files.
   Web mode: tokens MUST exist only in server process memory, bound to one browser session,
   and MUST NOT be written to disk, database, or logs; they are erased when the session ends
   or expires.
3. Secrets (`client_secret*.json`, tokens) MUST NOT be committed; `.gitignore` blocks them.
4. API writes MUST happen only after explicit user confirmation; `--dry-run` performs zero
   writes.
5. `sendUpdates` MUST default to `none` so migrations do not spam attendees.
6. The web service MUST NOT hold admin, service-account, or domain-wide-delegation
   credentials; every migration runs solely under the OAuth grants of the user driving that
   session, and no session may read another session's state.
7. CI MUST run `pip-audit` against the pinned lockfile.

## Article 7 — Simplicity

1. Speculative abstraction MUST NOT be introduced; add indirection only when a second concrete
   use exists.
2. New features REQUIRE a prior update to `docs/SPECIFICATION.md`; implementation follows the
   spec, never the reverse.

## Article 8 — Amendments

1. This constitution changes only via pull request containing the amendment and an explicit
   rationale.
