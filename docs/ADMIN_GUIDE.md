# CalMove Workspace Admin Guide

How to set up the CalMove web service so staff can migrate their calendars from the old
Google Workspace to the new one with nothing but a browser and their two logins.

What the service guarantees (binding rules: [`CONSTITUTION.md`](../CONSTITUTION.md) art. 6,
details: [`SPECIFICATION.md`](SPECIFICATION.md) §10):

- Every migration runs solely under the OAuth grants of the user driving it. The service
  holds no admin, service-account, or domain-wide-delegation credentials, and you as admin can
  never read anyone's calendar through it.
- Nothing is persisted. Tokens and calendar data live only in per-session server RAM,
  erased on "Finish" or after 30 minutes idle. There is no database and nothing to back up.
- Sessions are isolated; one user can never see another session's data.

---

## 1. Google Cloud project (one-time, ~15 min)

Do this in a project you control (either Workspace's organization works; the app serves both).

1. [console.cloud.google.com](https://console.cloud.google.com) → create a project, e.g. `calmove`.
2. **APIs & Services → Library** → enable the **Google Calendar API**.
3. **OAuth consent screen** (newer console: *Google Auth Platform*):
   - User type **External**: logins come from *both* Workspaces, so *Internal* is not an option.
   - App name, support e-mail, developer contact.
   - Scopes: `calendar.readonly`, `calendar.events.owned` (source: the token *could* write
     to any calendar the user owns; CalMove performs exactly one write with it: appending
     the new address to an invited event's guest list), `calendar.events` (destination: the
     token *could* write to any calendar the user can write; CalMove writes only to the
     destination calendar the user chose). When deciding whether to mark the app **Trusted**
     (below), assess that blast radius, not only CalMove's intended behavior.
4. **Credentials → Create credentials → OAuth client ID**:
   - Application type **Web application**.
   - Authorized redirect URI: `https://<your-calmove-host>/oauth/callback`; it must match
     `CALMOVE_WEB_BASE_URL` exactly.
   - Download the JSON (`client_secret_….json`). Treat it like a password; never commit it.

### Pilot vs. rollout

| Phase | Consent screen | Who can log in |
|---|---|---|
| Pilot | **Testing** + both pilot accounts under *Test users* | listed test users only (≤100); Google shows an "unverified app" notice to click through |
| Rollout | **Publish app** to Production, then mark the client ID **Trusted** in *both* Workspace admin consoles (below) | all staff of both domains, no warnings, no user cap, no Google review |

Trusting the client (do this in both Workspaces):
[admin.google.com](https://admin.google.com) → **Security → Access and data control → API
controls → App access control → Manage third-party app access → Configure new app** → search by
the client ID from the JSON → select → **Trusted**.

---

## 2. Mapping files (optional but recommended)

All three mappings are operator config; staff never see them. Create a config directory, e.g.
`/srv/calmove/`:

### `rooms.json`: room resources

Room resource e-mails differ between Workspaces. List them old → new:

```json
{
  "room-100@resource.calendar.google.com": "hq-room-100@resource.calendar.google.com",
  "room-101@resource.calendar.google.com": "hq-room-101@resource.calendar.google.com"
}
```

Find the resource e-mails in each Admin console under **Directory → Buildings and resources →
Manage resources** (column *Resource email*). Unmapped rooms are kept verbatim (the plan
page says exactly that, "kept verbatim") and flagged in every user's migration report;
use those flags to grow this file during the pilot.

### Domain rule: company-wide address change

If the whole organization changes domain (e.g. temporarily to a transition domain), one rule
rewrites every internal attendee on events the user organizes, keeping the local part
(`john.doe@old-company.com` → `john.doe@new-company.com`):

```
CALMOVE_WEB_DOMAIN_MAP="old-company.com=new-company.com"
```

Comma-separate multiple rules. Because `sendUpdates=none`, colleagues get no invitation
e-mails; events appear in their new calendars. External addresses and rooms are
untouched. Adding or changing a rule later is safe: the next run patches already-migrated
events in place (same event IDs, still no e-mails).

### `attendees.json`: people whose local part changes

Exceptions the domain rule can't express; these take precedence over domain rules:

```json
{
  "j.doe@old-company.com": "john.doe@new-company.com"
}
```

---

## 3. Deploy the container

Build from the repo (or your registry copy):

```sh
docker build -t calmove-web .
```

Run with no volumes except read-only config and no persistence:

```sh
docker run -d --name calmove-web --restart unless-stopped \
  -p 127.0.0.1:8080:8080 \
  -v /srv/calmove/client_secret.json:/run/secrets/client_secret.json:ro \
  -v /srv/calmove/rooms.json:/run/config/rooms.json:ro \
  -v /srv/calmove/attendees.json:/run/config/attendees.json:ro \
  -e CALMOVE_WEB_CLIENT_SECRET=/run/secrets/client_secret.json \
  -e CALMOVE_WEB_BASE_URL=https://calmove.example.com \
  -e CALMOVE_WEB_ROOM_MAP=/run/config/rooms.json \
  -e CALMOVE_WEB_ATTENDEE_MAP=/run/config/attendees.json \
  -e CALMOVE_WEB_DOMAIN_MAP="old-company.com=new-company.com" \
  calmove-web
```

### Baking the client secret into the image (optional)

Instead of mounting the secret at runtime you can bake it in at build time with BuildKit:

```sh
docker build --secret id=client_secret,src=/srv/calmove/client_secret.json -t calmove-web .
```

Then run without the `client_secret.json` volume and without `-e CALMOVE_WEB_CLIENT_SECRET`;
the image defaults to the baked `/app/client_secret.json`.

> WARNING: the image now *contains the credential*. Push it only to a private
> registry, never a public one. The runtime secret mount remains the recommended default.

### Environment reference

| Variable | Required | Meaning |
|---|---|---|
| `CALMOVE_WEB_CLIENT_SECRET` | yes | Path to the OAuth **Web application** client JSON |
| `CALMOVE_WEB_BASE_URL` | yes | External HTTPS URL; `<base>/oauth/callback` must be the registered redirect URI |
| `CALMOVE_WEB_ROOM_MAP` | no | Path to `rooms.json` |
| `CALMOVE_WEB_ATTENDEE_MAP` | no | Path to `attendees.json` |
| `CALMOVE_WEB_DOMAIN_MAP` | no | `old=new[,old=new…]` domain rules |
| `CALMOVE_WEB_PORT` | no | Container port (default 8080) |

### Reverse proxy (required for production)

Terminate TLS in front of the container (nginx, Caddy, Traefik, …). Two hard requirements:

1. Proxy `https://calmove.example.com` → `http://127.0.0.1:8080`, forwarding as-is, with no
   path rewriting; the OAuth callback must arrive at `/oauth/callback`.
2. Scrub query strings of `/oauth/callback` from the proxy's access logs: the single-use
   OAuth authorization code arrives there ([`SECURITY.md`](../SECURITY.md)). nginx example:

   ```nginx
   location /oauth/callback { access_log off; proxy_pass http://127.0.0.1:8080; }
   location /               {                 proxy_pass http://127.0.0.1:8080; }
   ```

Run exactly one container instance: session state is in-process RAM, and a second replica
would split sessions (spec §10). One instance is sufficient for an office: migrations are
per-user, short-lived, and capped at 1000 concurrent sessions (requests beyond that get a
clear "at capacity" page).

---

## 4. What staff do (share this)

1. Open `https://calmove.example.com`, click **Start migration**.
2. Sign in with the old account → pick the source calendar.
3. Sign in with the new account → pick the destination calendar.
4. Choose a date range → review the dry-run plan (nothing written yet).
5. Type `migrate` → watch progress → **Finish** (erases the session). The checkbox above
   the confirmation field sends invitation e-mails to attendees; it also re-sends for
   events migrated by an earlier run without notifications. Leave it unchecked for silent
   trial runs.

Re-running later is safe and encouraged: only changed, new, or deleted events are touched.
Events they were merely invited to are not copied; the invitation is forwarded to the
chosen destination calendar's address (their new account for a personal migration) and,
for personal migrations, their RSVP is carried over.

### Group calendars

1. Create the new group calendar in the new Workspace; grant the migrating person
   `writer`/`owner` access on it. Calendar sharing is not event data; re-share it with the team
   yourself afterwards.
2. Designate one runner, ideally the owner of the old group calendar (forwarding
   writes to the old calendar's event copies, and the source scope covers only calendars
   the user owns; mere writers see those forwards in the failure report).
3. The runner picks the old group calendar as source and the new one as destination.
   Events created on the calendar are fully re-created; events the calendar was invited to
   are forwarded to the new group calendar's address (no RSVP is mirrored, since calendars
   don't RSVP; Google tracks their copies automatically).
4. Re-runs are team-safe as long as everyone targets the same destination calendar; the
   sync markers prevent duplicates.

---

## 5. Operations & troubleshooting

- Logs: lifecycle only; never event titles, attendees, or tokens. `docker logs calmove-web`.
- Updates: `docker build …` (or pull) → `docker rm -f calmove-web` → run again. In-flight
  sessions are lost (users restart the wizard); nothing else to migrate or back up.
- Nothing to back up: the service stores no state.

| Symptom | Cause / fix |
|---|---|
| "Access blocked: … has not completed the Google verification process" | Consent screen still in Testing and the account is not a test user → add it, or publish + trust the client ID in both admin consoles |
| "Google hasn't verified this app" interstitial | Normal in Testing / before admin trust; *Advanced → continue* works for test users |
| `redirect_uri_mismatch` during login | `CALMOVE_WEB_BASE_URL` and the client's authorized redirect URI disagree; they must match scheme, host, and port exactly |
| Events under *Failed* with HTTP 403 on forwarding | That event's organizer disabled "guests can invite others"; the user keeps attending via the old account, or asks the organizer to re-invite the new address |
| Destination calendar not selectable | The account only has `reader` access there; destination needs `writer`/`owner` |
| Unmapped rooms flagged in reports | Add the pair to `rooms.json`, restart the container; the next re-run patches affected events in place |
| "The service is at capacity" | More than 1000 live sessions (or an abuse flood); rate-limit `/start` at the proxy, or restart to clear |
| Many failures with 401 after a long session | Google access tokens expire after ~1 h; finish the wizard within an hour or restart from Start |
| Shared destination calendars | Any co-writer of the destination calendar could tamper with CalMove sync markers; CalMove refuses to update/delete events it did not create, flagging them in the report |
