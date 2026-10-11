# CalMove

Migrate a Google Calendar from an old Google Workspace to a new one without losing the data
a plain ICS export/import strips. Exporting a calendar as `.ics` and re-importing it loses
invited attendees, booked rooms (resource attendees), conference links, and recurrence
exception details. CalMove instead reads events through the Google Calendar API from the
source workspace and re-creates them through the same API in the destination workspace, so
attendees, rooms, RRULEs, exceptions, and event settings survive.

Details: [docs/SPECIFICATION.md](docs/SPECIFICATION.md) ·
architectural rules: [CONSTITUTION.md](CONSTITUTION.md) ·
deployment: [docs/ADMIN_GUIDE.md](docs/ADMIN_GUIDE.md)

## Prerequisites

- Python ≥ 3.12
- [uv](https://docs.astral.sh/uv/)
- A Google Cloud OAuth **Desktop app** client:
  1. Open the [Google Cloud Console](https://console.cloud.google.com/) and create (or select)
     a project.
  2. **APIs & Services → Library** → enable the **Google Calendar API**.
  3. **APIs & Services → OAuth consent screen** → configure it (internal or testing is fine)
     and add the scopes `calendar.readonly`, `calendar.events.owned`, and `calendar.events`.
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

1. Opens your system browser to log into the old workspace (read-only scope) and asks you
   to pick the source calendar by number.
2. Opens the browser again to log into the new workspace (events scope) and asks for the
   destination calendar.
3. Asks for a date range:
   - `1`: from today onward
   - `2`: everything
   - `3`: custom `from`/`to` dates
4. Shows a dry-run plan table (create / skip counts, unmapped rooms) and asks for a typed
   confirmation before writing anything.
5. Creates the events and prints a final report (created / skipped / failed).

### Options

| Option | Effect |
|---|---|
| `--dry-run` | Compute and show the plan only; zero writes. |
| `--room-map rooms.json` | Translate room resource e-mails between workspaces (see below). |
| `--map-domain old=new` | Rewrite attendee domains on owned events, e.g. `--map-domain old-company.com=new-company.com` (repeatable). Local parts stay; rooms and other domains untouched. |
| `--attendee-map attendees.json` | Explicit old → new attendee e-mails for people whose local part changes (beats domain rules). |
| `--send-updates [none\|all\|externalOnly]` | Invitation e-mails for created events. Default `none`: attendees receive no invitation e-mails. |
| `--resend-invitations` | Re-write unchanged events so Google sends invitation e-mails for events migrated by an earlier silent run (combine with `--send-updates all`). |

Room map example (`rooms.json`):

```json
{
  "old-room@resource.calendar.google.com": "new-room@resource.calendar.google.com"
}
```

Unmapped rooms are kept verbatim and flagged in the report. Attendee rewriting plus the
default `sendUpdates=none` means colleagues' events appear in their new calendars without
invitation e-mails; adding a rule later patches already-migrated events in place.

Re-running a migration synchronizes: events carry a `calmove-source-id` marker and a
content fingerprint, so a re-run skips unchanged events, updates changed ones in place,
deletes destination copies whose source event is gone, and creates new ones. Events you were
merely *invited to* are never copied. CalMove forwards the invitation to your new address
(and mirrors your RSVP), so the new account joins the organizer's live event. Interrupted
runs can be resumed.

### Logout

```sh
uv run calmove logout
```

Removes the stored OAuth tokens from the OS keyring.

## Web service mode (Docker)

For staff who cannot install software: the same wizard as a browser flow
(spec [§10](docs/SPECIFICATION.md#10-web-deployment-mode-docker)). Each migration runs solely
under the OAuth grants of the user driving that browser session. The service never holds
admin or service-account credentials, persists nothing (tokens and event data live only in
per-session server RAM, erased on finish or after 30 minutes idle), and sessions cannot see
each other.

It needs a **Web application** OAuth client (not Desktop) with the redirect URI
`<base-url>/oauth/callback` and an *External* consent screen so accounts from both
workspaces can sign in.

```sh
docker build -t calmove-web .
docker run --rm -p 8080:8080 \
  -v /path/to/web_client_secret.json:/run/secrets/client_secret.json:ro \
  -e CALMOVE_WEB_CLIENT_SECRET=/run/secrets/client_secret.json \
  -e CALMOVE_WEB_BASE_URL=https://calmove.example.com \
  calmove-web
```

Alternatively, bake the secret into a privately-hosted image at build time:
`docker build --secret id=client_secret,src=/path/to/web_client_secret.json -t calmove-web .`
Then run without the volume and without `-e CALMOVE_WEB_CLIENT_SECRET` (it defaults to the
baked `/app/client_secret.json`).

Terminate TLS at a reverse proxy in front of the container. Optional:
`CALMOVE_WEB_ROOM_MAP` (room-map JSON path), `CALMOVE_WEB_ATTENDEE_MAP` (attendee-map JSON
path), `CALMOVE_WEB_DOMAIN_MAP` (e.g. `old-company.com=new-company.com,other.com=new.com`),
`CALMOVE_WEB_PORT` (default 8080).
Run it locally without Docker via `uv run calmove-web`.

## Known limitations

| Item | Behavior |
|---|---|
| Google Meet links | Cannot be transferred across workspaces. CalMove requests a fresh Meet conference on the new event and appends the old link to the description for reference. |
| Organizer | The migrating user becomes organizer of re-created events they owned. Invited events are forwarded, not copied, so the original organizer stays in charge. Forwarding needs the organizer's "guests can invite others" setting (Google default: on). |
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
