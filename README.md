# CalMove

Migrate a Google Calendar from an old Google Workspace to a new one — without losing the data
a plain ICS export/import strips. Exporting a calendar as `.ics` and re-importing it loses
invited attendees, booked rooms (resource attendees), conference links, and recurrence
exception details. CalMove instead reads events through the Google Calendar API from the
source workspace and re-creates them through the same API in the destination workspace, so
attendees, rooms, RRULEs, exceptions, and event settings survive.

Details: [docs/SPECIFICATION.md](docs/SPECIFICATION.md) ·
architectural rules: [CONSTITUTION.md](CONSTITUTION.md)

## Prerequisites

- Python ≥ 3.12
- [uv](https://docs.astral.sh/uv/)
- A Google Cloud OAuth **Desktop app** client:
  1. Open the [Google Cloud Console](https://console.cloud.google.com/) and create (or select)
     a project.
  2. **APIs & Services → Library** → enable the **Google Calendar API**.
  3. **APIs & Services → OAuth consent screen** → configure it (internal or testing is fine)
     and add the scopes `calendar.readonly` and `calendar.events`.
  4. **APIs & Services → Credentials → Create credentials → OAuth client ID** → application
     type **Desktop app**.
  5. Download the resulting `client_secret.json`. Keep it out of the repo (it is gitignored).

## Install

```sh
uv sync
```

## Usage

```sh
uv run calmove migrate --client-secret path/to/client_secret.json
```

(or set `CALMOVE_CLIENT_SECRET=path/to/client_secret.json` instead of the flag)

The interactive wizard then:

1. Opens your system browser to log into the **old** workspace (read-only scope) and asks you
   to pick the source calendar by number.
2. Opens the browser again to log into the **new** workspace (events scope) and asks for the
   destination calendar.
3. Asks for a date range:
   - `1` — from today onward
   - `2` — everything
   - `3` — custom `from`/`to` dates
4. Shows a dry-run plan table (create / skip counts, unmapped rooms) and asks for a typed
   confirmation before writing anything.
5. Creates the events and prints a final report (created / skipped / failed).

### Options

| Option | Effect |
|---|---|
| `--dry-run` | Compute and show the plan only; zero writes. |
| `--room-map rooms.json` | Translate room resource e-mails between workspaces (see below). |
| `--send-updates [none\|all\|externalOnly]` | Invitation e-mails for created events. Default `none` — attendees are not spammed. |

Room map example (`rooms.json`):

```json
{
  "old-room@resource.calendar.google.com": "new-room@resource.calendar.google.com"
}
```

Unmapped rooms are kept verbatim and flagged in the report.

Re-running a migration is safe: created events carry a `calmove-source-id` marker, so
already-migrated events are skipped (interrupted runs can simply be resumed).

### Logout

```sh
uv run calmove logout
```

Removes the stored OAuth tokens from the OS keyring.

## Known limitations

| Item | Behavior |
|---|---|
| Google Meet links | Cannot be transferred across workspaces. CalMove requests a fresh Meet conference on the new event and appends the old link to the description for reference. |
| Organizer | The migrating user becomes organizer of the new events (API constraint). |
| Attendee RSVP status | Sent best-effort; Google may reset it to `needsAction`. |
| Shared calendars | Destination write requires `writer`/`owner` access role (validated before migration). |

## Development

```sh
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
```

Security policy: [SECURITY.md](SECURITY.md)
