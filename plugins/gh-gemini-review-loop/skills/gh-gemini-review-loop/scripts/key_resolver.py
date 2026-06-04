"""Tiered OpenAI API-key resolver for the judge.

Resolution order (first hit wins):

1. ``~/.config/gh-gemini-review-loop/.env`` — a chmod-600 dotfile with
   ``OPENAI_API_KEY=sk-...``. Gitignored, survives shell reloads, easy to
   inspect, and the canonical store for interactive local use.
2. macOS Keychain (``security find-generic-password -s gh-gemini-review-loop
   -a openai -w``) — system-protected, prompts Touch ID / password for access
   on first read per process.
3. Linux Secret Service (``secret-tool lookup service gh-gemini-review-loop
   key openai``) — D-Bus secret backend used by GNOME Keyring, KWallet,
   etc. Skipped when ``secret-tool`` is absent.
4. ``OPENAI_API_KEY`` environment variable — last-resort fallback for CI/CD
   pipelines, Docker containers, and headless environments where env vars are
   the standard injection mechanism. Checked last so a stale project-level env
   var never shadows a dotfile or OS-keystore key stored via ``--set``.

Rationale: the dotfile is the canonical store for interactive local use.
Placing the env var last means CI/CD workflows continue to work while local
developers are never surprised by a stale ``OPENAI_API_KEY`` in their shell
overriding the key they stored with ``--set``.

This module is the ONE place that uses ``subprocess`` in the judge tree.
``judge.py`` consults a pure function exposed here so the
"judge-is-read-only" invariant (no GH-mutation surface in judge.py) stays
intact.

CLI:
    python3 key_resolver.py --print-source     # which source is active
    python3 key_resolver.py --set              # interactive store
    python3 key_resolver.py --set --from-stdin # read key from stdin
    python3 key_resolver.py --clear            # remove from all writeable sources
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import typing as t
from pathlib import Path

KEYCHAIN_SERVICE = "gh-gemini-review-loop"
KEYCHAIN_ACCOUNT = "openai"
SECRET_TOOL_SCHEMA = ("service", KEYCHAIN_SERVICE, "key", KEYCHAIN_ACCOUNT)

# Sources, in resolution order. Each entry: (label, reader_callable).
# Tests mock individual readers via monkeypatch on the module-level
# callables — keeps the resolver pure for the happy path.
SOURCE_LABELS = ("dotenv", "macos_keychain", "linux_secret_service", "env")


def dotenv_path() -> Path:
    """Where the chmod-600 fallback ``.env`` lives. Override with ``GGRL_STATE_DIR``."""
    base = os.environ.get("GGRL_STATE_DIR") or os.path.expanduser(
        "~/.config/gh-gemini-review-loop"
    )
    return Path(base) / ".env"


def _read_env() -> str | None:
    """Read OPENAI_API_KEY from the environment. Last-resort fallback for CI/CD."""
    val = os.environ.get("OPENAI_API_KEY")
    return val.strip() if val and val.strip() else None


def _read_dotenv() -> str | None:
    """Parse OPENAI_API_KEY out of the dotfile. Tolerates KEY=value and KEY="value"."""
    path = dotenv_path()
    if not path.exists():
        return None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() != "OPENAI_API_KEY":
                continue
            v = v.strip().strip("'").strip('"')
            return v or None
    except OSError:
        return None
    return None


def _read_macos_keychain() -> str | None:
    """Read from macOS Keychain. Silent if no entry, no security binary, or non-darwin."""
    if platform.system() != "Darwin":
        return None
    security = shutil.which("security")
    if not security:
        return None
    try:
        proc = subprocess.run(
            [
                security,
                "find-generic-password",
                "-s", KEYCHAIN_SERVICE,
                "-a", KEYCHAIN_ACCOUNT,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out or None


def _read_linux_secret_service() -> str | None:
    """Read from secret-tool. Silent if no binary, no entry, or non-linux."""
    if platform.system() != "Linux":
        return None
    secret_tool = shutil.which("secret-tool")
    if not secret_tool:
        return None
    try:
        proc = subprocess.run(
            [secret_tool, "lookup", *SOURCE_LABELS_SECRET_TOOL_ARGS()],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out or None


def SOURCE_LABELS_SECRET_TOOL_ARGS() -> list[str]:
    # Flatten ("service", "gh-...", "key", "openai") into the form secret-tool
    # expects: a sequence of attr/value pairs.
    return list(SECRET_TOOL_SCHEMA)


# Reader dispatch — exposed at module level so tests can monkeypatch each.
_READERS: dict[str, t.Callable[[], str | None]] = {
    "dotenv": _read_dotenv,
    "macos_keychain": _read_macos_keychain,
    "linux_secret_service": _read_linux_secret_service,
    "env": _read_env,
}


def resolve_api_key() -> tuple[str | None, str]:
    """Walk the tiered sources. Return (key, source-label) or (None, "missing")."""
    for label in SOURCE_LABELS:
        reader = _READERS[label]
        try:
            val = reader()
        except Exception:  # noqa: BLE001 — never let a backend crash the resolver
            continue
        if val:
            return val, label
    return None, "missing"


def store_api_key(key: str) -> str:
    """Persist key. Returns the source label where it was written.

    Mac: Keychain (preferred). If ``security`` is missing for any reason,
    fall back to chmod-600 dotfile.
    Linux: ``secret-tool`` if available; otherwise chmod-600 dotfile.
    Anywhere else: dotfile.
    """
    # Defensive type check: callers can come from CLI (always str),
    # programmatic use, or a future settings.json plumbing path. Bail
    # with a clear TypeError instead of an opaque AttributeError on
    # `.strip()` if a bool / int / None ever slips in.
    if not isinstance(key, str):
        raise TypeError(f"key must be str, got {type(key).__name__}")
    key = key.strip()
    if not key:
        raise ValueError("empty key")
    sys_name = platform.system()
    if sys_name == "Darwin" and shutil.which("security"):
        return _store_macos_keychain(key)
    if sys_name == "Linux" and shutil.which("secret-tool"):
        return _store_linux_secret_service(key)
    return _store_dotenv(key)


def _store_macos_keychain(key: str) -> str:
    # -U updates the entry if it already exists; without it, security
    # errors with "already exists".
    subprocess.run(
        [
            "security",
            "add-generic-password",
            "-U",
            "-s", KEYCHAIN_SERVICE,
            "-a", KEYCHAIN_ACCOUNT,
            "-w", key,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return "macos_keychain"


def _store_linux_secret_service(key: str) -> str:
    # No trailing newline: secret-tool reads stdin until EOF, so any \n
    # we add is persisted as part of the secret. A retrieval would then
    # return "sk-...\n" and a downstream comparison or Bearer header
    # would silently break.
    subprocess.run(
        ["secret-tool", "store", "--label", "gh-gemini-review-loop OpenAI key",
         *SOURCE_LABELS_SECRET_TOOL_ARGS()],
        input=key,
        check=True,
        capture_output=True,
        text=True,
    )
    return "linux_secret_service"


def _store_dotenv(key: str) -> str:
    path = dotenv_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Read-modify-write so we don't clobber other keys the user added.
    # Use partition("=") + .strip() to match _read_dotenv's parsing — a
    # plain startswith("OPENAI_API_KEY=") misses `OPENAI_API_KEY = "v"`
    # with spaces, which would leak the old key past this filter and
    # leave _read_dotenv returning the stale value forever.
    lines: list[str] = []
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            k, _, _ = raw.partition("=")
            if k.strip() == "OPENAI_API_KEY":
                continue
            lines.append(raw)
    lines.append(f'OPENAI_API_KEY="{key}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return "dotenv"


def clear_api_key() -> list[str]:
    """Remove the key from every writeable source. Returns labels cleared."""
    cleared: list[str] = []
    if platform.system() == "Darwin" and shutil.which("security"):
        proc = subprocess.run(
            ["security", "delete-generic-password",
             "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            cleared.append("macos_keychain")
    if platform.system() == "Linux" and shutil.which("secret-tool"):
        proc = subprocess.run(
            ["secret-tool", "clear", *SOURCE_LABELS_SECRET_TOOL_ARGS()],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            cleared.append("linux_secret_service")
    path = dotenv_path()
    if path.exists():
        # Mirror _store_dotenv's partition-based filter so `KEY = "v"` with
        # spaces is also cleared, not silently left behind.
        kept: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            k, _, _ = line.partition("=")
            if k.strip() == "OPENAI_API_KEY":
                continue
            kept.append(line)
        if kept:
            path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        else:
            path.unlink()
        cleared.append("dotenv")
    return cleared


def _redact(key: str) -> str:
    if len(key) <= 8:
        return "***"
    return f"{key[:6]}...{key[-4:]}"


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Tiered OpenAI API-key resolver for the gh-gemini-review-loop judge."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--print-source", action="store_true",
                       help="Print which source the key was resolved from (key value is redacted).")
    group.add_argument("--set", dest="set_key", action="store_true",
                       help="Store an OpenAI API key. Reads from stdin if --from-stdin, else prompts.")
    group.add_argument("--clear", action="store_true",
                       help="Remove the key from every writeable source.")
    parser.add_argument("--from-stdin", action="store_true",
                        help="With --set, read the key from stdin instead of an interactive prompt.")
    args = parser.parse_args(argv)

    if args.print_source:
        key, source = resolve_api_key()
        if not key:
            print("source: missing")
            print("checked: dotenv (~/.config/gh-gemini-review-loop/.env), "
                  "macos_keychain, linux_secret_service, env")
            return 1
        print(f"source: {source}")
        print(f"key:    {_redact(key)}")
        return 0

    if args.set_key:
        if args.from_stdin:
            key = sys.stdin.read().strip()
        else:
            # getpass keeps the key out of shell history and off the terminal.
            import getpass  # noqa: PLC0415
            key = getpass.getpass("Paste OpenAI API key (input hidden): ").strip()
        if not key:
            print("error: empty key", file=sys.stderr)
            return 2
        try:
            written = store_api_key(key)
        except subprocess.CalledProcessError as exc:
            print(f"error: failed to store ({exc.stderr or exc})", file=sys.stderr)
            return 3
        except (OSError, ValueError) as exc:
            # OSError: dotfile write blocked by perms / read-only fs.
            # ValueError: store_api_key("") via a future code path. Catch
            # explicitly (not bare Exception) so genuine bugs still surface.
            print(f"error: failed to store ({exc})", file=sys.stderr)
            return 3
        print(f"stored in: {written}")
        return 0

    if args.clear:
        cleared = clear_api_key()
        if not cleared:
            print("nothing to clear")
            return 0
        print("cleared: " + ", ".join(cleared))
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
