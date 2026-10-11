"""Lightweight syntactic validation of e-mail addresses and domains.

Deliberately minimal (no RFC 5322 parsing): the goal is to catch obvious
configuration typos early, not to adjudicate deliverability.
"""


def is_email(value: str) -> bool:
    """True when ``value`` looks like ``local@domain`` (exactly one ``@``, no whitespace)."""
    if value.count("@") != 1 or any(c.isspace() for c in value):
        return False
    local, _, domain = value.partition("@")
    return bool(local) and bool(domain)


def is_domain(value: str) -> bool:
    """True when ``value`` is a plausible domain: non-empty dot-separated labels."""
    if not value or "@" in value or any(c.isspace() for c in value):
        return False
    return all(value.split("."))
