"""Credential locations and secret-bearing environment names Agent tools must not reach silently.

This is a conservative name-based fence, not a sandbox: shell globbing or a
recursive search started above a credential file can still read it. Configure
LKA_WORKSPACE_ROOTS to bound what tools can reach at all.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath

# Environment names that look like credentials. Matched case-insensitively.
_SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSW|CREDENTIAL|COOKIE|PRIVATE)", re.IGNORECASE)
# Sockets and helpers that grant credential use without exposing a value.
_SECRET_ENV_EXACT = {"SSH_AUTH_SOCK", "GPG_AGENT_INFO", "GIT_ASKPASS", "SSH_ASKPASS"}

_SECRET_FILE_NAMES = {
    ".netrc", "_netrc", ".git-credentials", ".pgpass", ".npmrc", ".pypirc",
    "credentials", "credentials.json", "service-account.json", "secrets.json",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "known_hosts", "authorized_keys",
}
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx", ".dpapi", ".keystore", ".jks")
_ENV_TEMPLATES = {".env.example", ".env.sample", ".env.template", ".env.dist"}
_SECRET_DIRS = {".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker"}


def is_secret_env_name(name: str) -> bool:
    return name.upper() in _SECRET_ENV_EXACT or bool(_SECRET_ENV.search(name))


def scrub_secret_env(env: dict[str, str], *, keep: set[str] | None = None) -> dict[str, str]:
    """Drop credential-looking variables; ``keep`` lists names the caller injects on purpose."""

    keep = keep or set()
    return {key: value for key, value in env.items() if key in keep or not is_secret_env_name(key)}


def sensitive_path_reason(value: str) -> str | None:
    """Explain why a path names a credential location, or return None."""

    text = str(value or "").strip().strip("'\"")
    if not text:
        return None
    lowered = text.replace("\\", "/").lower()
    if re.search(r"(^|/)proc/(\d+|self|\*)/environ$", lowered):
        return "Reading a process environment can expose credentials."
    parts = PureWindowsPath(text).parts if "\\" in text else PurePosixPath(text).parts
    names = [part.lower() for part in parts]
    if not names:
        return None
    name = names[-1]
    if name == ".env" or (name.startswith(".env.") and name not in _ENV_TEMPLATES):
        return f"`{text}` is an environment file that usually holds API keys."
    if name in _SECRET_FILE_NAMES or name.startswith(("id_rsa", "id_ed25519", "id_ecdsa")):
        return f"`{text}` is a credential or key file."
    if name.endswith(_SECRET_SUFFIXES):
        return f"`{text}` looks like a private key or keystore."
    if any(part in _SECRET_DIRS for part in names):
        return f"`{text}` is inside a credential directory."
    if name == "local.toml" and len(names) > 1 and names[-2] == "config":
        return f"`{text}` is the private local configuration."
    return None
