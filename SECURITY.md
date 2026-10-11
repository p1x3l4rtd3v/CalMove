# Security Policy

## Supported Versions

Only the latest release on the `main` branch receives security fixes.

## How CalMove Handles Credentials

Mirrors `docs/SPECIFICATION.md` §7:

- OAuth 2.0 with PKCE, two deployment modes. CLI: installed-app loopback flow in the
  *system* browser (`http://127.0.0.1:<random port>`). Web service (spec §10): web-app
  authorization-code flow with PKCE and `access_type=online`, so no refresh token is ever
  issued. No embedded webviews, no password handling.
- Least-privilege scopes (capability vs. behavior): the source workspace login uses
  `https://www.googleapis.com/auth/calendar.readonly` plus `calendar.events.owned`. That
  token *could* modify any calendar the user owns; CalMove's *behavior* is to perform
  exactly one write with it: appending the new address to an invited event's guest list
  (invitation forwarding). The destination login uses `calendar.events` +
  `calendar.readonly`; that token *could* write to any calendar the user can write, while
  CalMove writes only to the destination calendar the user chose. Neither token can touch
  other people's calendars. Admins evaluating the Trusted-app decision should assess that
  blast radius, not only CalMove's intended behavior.
- Token storage: CLI mode stores refresh/access tokens in the OS keyring (macOS Keychain,
  Windows Credential Manager) under the service name `calmove`, never in plaintext files;
  `calmove logout` deletes them. Web mode keeps tokens only in server process memory, bound
  to one browser session, erased on finish or after 30 minutes idle, never on disk, in a
  database, or in logs.
- No secrets in the repository: users supply their own OAuth Desktop client via
  `--client-secret` or the `CALMOVE_CLIENT_SECRET` environment variable; `.gitignore` blocks
  `client_secret*.json` and token files.
- Read-only until confirmation: all API writes happen strictly after the explicit user
  confirmation step; `--dry-run` performs the full plan with zero writes.
- Per-user authority only (web mode): the service holds no admin, service-account, or
  domain-wide-delegation credentials; each migration runs solely under the OAuth grants of
  the session driving it, and sessions are isolated by unguessable random IDs.
- Reverse-proxy requirement (web mode): the OAuth authorization code arrives as a query
  parameter on `/oauth/callback` (single-use, PKCE-bound, no refresh token). The fronting
  proxy must scrub or omit query strings for that path in its access logs.
- Dependencies are pinned via the `uv` lockfile and audited with `pip-audit` in CI.

## Reporting a Vulnerability

Do not open a public issue for security problems. Instead, open a private GitHub
security advisory on this repository (Security → Advisories → "Report a vulnerability").
You will receive a response there; fixes are coordinated and disclosed through the advisory.
