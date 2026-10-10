"""OAuth flows (installed-app loopback and web-app PKCE) and OS-keyring token storage."""

import contextlib
import json
from collections.abc import Sequence
from pathlib import Path

import keyring
import keyring.errors
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow, InstalledAppFlow

SOURCE_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    # Write access confined to calendars the user owns; used for exactly one
    # operation: appending the new address to an invited event's guest list
    # (invitation forwarding, Constitution art. 6.1).
    "https://www.googleapis.com/auth/calendar.events.owned",
]
DESTINATION_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
]

_SERVICE_NAME = "calmove"


class KeyringTokenStore:
    """Stores OAuth tokens per role in the OS keychain, never in files."""

    def load(self, role: str) -> Credentials | None:
        raw = keyring.get_password(_SERVICE_NAME, role)
        if raw is None:
            return None
        return Credentials.from_authorized_user_info(json.loads(raw))

    def save(self, role: str, creds: Credentials) -> None:
        keyring.set_password(_SERVICE_NAME, role, creds.to_json())

    def delete(self, role: str) -> None:
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(_SERVICE_NAME, role)


def authenticate(
    role: str,
    scopes: Sequence[str],
    client_secret_path: Path,
    store: KeyringTokenStore,
) -> Credentials:
    """Return credentials for ``role``, reusing stored tokens when possible."""
    creds = store.load(role)
    if creds is not None and set(scopes) <= set(creds.scopes or ()):
        if creds.valid:
            return creds
        if creds.expired and creds.refresh_token:
            refreshed = _try_refresh(creds)
            if refreshed is not None:
                store.save(role, refreshed)
                return refreshed
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), list(scopes))
    creds = flow.run_local_server(port=0)
    store.save(role, creds)
    return creds


def web_authorization_url(
    scopes: Sequence[str],
    client_secret_path: Path,
    redirect_uri: str,
) -> tuple[str, str, str]:
    """Start the web-app PKCE flow; return ``(authorization_url, state, code_verifier)``.

    ``access_type=online`` keeps the grant session-scoped (no refresh token is ever
    issued), and ``prompt=select_account`` lets the user pick the right workspace
    account for each of the two logins.
    """
    flow = Flow.from_client_secrets_file(
        str(client_secret_path),
        scopes=list(scopes),
        redirect_uri=redirect_uri,
        autogenerate_code_verifier=True,
    )
    url, state = flow.authorization_url(access_type="online", prompt="select_account")
    return str(url), str(state), str(flow.code_verifier)


def web_exchange_code(
    scopes: Sequence[str],
    client_secret_path: Path,
    redirect_uri: str,
    *,
    code: str,
    state: str,
    code_verifier: str,
) -> Credentials:
    """Exchange the authorization ``code`` for credentials (held in memory only)."""
    flow = Flow.from_client_secrets_file(
        str(client_secret_path),
        scopes=list(scopes),
        redirect_uri=redirect_uri,
        state=state,
        code_verifier=code_verifier,
    )
    flow.fetch_token(code=code)
    credentials: Credentials = flow.credentials
    return credentials


def _try_refresh(creds: Credentials) -> Credentials | None:
    """Refresh ``creds`` in place; a stale refresh token falls back to a fresh login."""
    try:
        creds.refresh(Request())
    except Exception:
        return None
    return creds
