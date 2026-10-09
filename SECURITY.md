# Security Policy

## Supported Versions

Only the latest release on the `main` branch receives security fixes.

## How CalMove Handles Credentials

Mirrors `docs/SPECIFICATION.md` §7:

- **OAuth 2.0 installed-app loopback flow** with PKCE in the *system* browser
  (`http://127.0.0.1:<random port>`). No embedded webviews, no password handling.
- **Least-privilege scopes**: the source workspace login uses
  `https://www.googleapis.com/auth/calendar.readonly` only — CalMove is physically unable to
  modify the old workspace. The destination login uses `calendar.events` + `calendar.readonly`.
- **Token storage**: refresh/access tokens are stored in the OS keyring (macOS Keychain,
  Windows Credential Manager) under the service name `calmove` — never in plaintext files.
  `calmove logout` deletes them.
- **No secrets in the repository**: users supply their own OAuth Desktop client via
  `--client-secret` or the `CALMOVE_CLIENT_SECRET` environment variable; `.gitignore` blocks
  `client_secret*.json` and token files.
- **Read-only until confirmation**: all API writes happen strictly after the explicit user
  confirmation step; `--dry-run` performs the full plan with zero writes.
- Dependencies are pinned via the `uv` lockfile and audited with `pip-audit` in CI.

## Reporting a Vulnerability

Please do **not** open a public issue for security problems. Instead, open a **private GitHub
security advisory** on this repository (Security → Advisories → "Report a vulnerability").
You will receive a response there; fixes are coordinated and disclosed through the advisory.
