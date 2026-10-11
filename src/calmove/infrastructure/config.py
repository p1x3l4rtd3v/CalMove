"""Loading and validation of user-supplied configuration (address maps, domain rules)."""

import json
from collections.abc import Sequence
from pathlib import Path

from calmove.domain.addresses import is_domain, is_email
from calmove.domain.errors import InvalidConfigError


def load_address_map(path: Path, label: str) -> dict[str, str]:
    """Load a ``{"old@a.example": "new@b.example"}`` JSON mapping from ``path``.

    Raises :class:`InvalidConfigError` (mentioning ``label`` and ``path``) when the
    file is unreadable, not valid JSON, not a string-to-string object, or when any
    key or value is not a plausible e-mail address.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise InvalidConfigError(f"{label}: cannot read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise InvalidConfigError(f"{label}: invalid JSON in {path}: {error}") from error
    if not isinstance(raw, dict):
        raise InvalidConfigError(f"{label}: {path} must contain a JSON object")
    mapping: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str):
            raise InvalidConfigError(
                f"{label}: {path}: value for {key!r} must be a string, got {type(value).__name__}"
            )
        if not is_email(key) or not is_email(value):
            raise InvalidConfigError(
                f"{label}: {path}: entry {key!r} -> {value!r} is not a valid e-mail mapping"
            )
        mapping[key] = value
    return mapping


def parse_domain_rules(rules: Sequence[str]) -> dict[str, str]:
    """Parse ``old=new`` domain rewrite rules (each argument may be comma-joined).

    Leading/trailing whitespace and a leading ``@`` are tolerated on both sides.
    Raises :class:`InvalidConfigError` on any malformed rule or implausible domain.
    """
    parsed: dict[str, str] = {}
    for argument in rules:
        for item in argument.split(","):
            rule = item.strip()
            if not rule:
                continue
            old, sep, new = rule.partition("=")
            if not sep:
                raise InvalidConfigError(f"invalid domain rule {rule!r}: expected 'old=new'")
            old = old.strip().lstrip("@")
            new = new.strip().lstrip("@")
            if not is_domain(old) or not is_domain(new):
                raise InvalidConfigError(
                    f"invalid domain rule {rule!r}: both sides must be plain domains"
                )
            parsed[old] = new
    return parsed
