"""EOD Dhan access-token renewal.

Dhan access tokens are valid for 24 hours. Rather than pasting a fresh token by
hand every day, this runs at end-of-day (after the snapshot collection + retention
prune finish) to rotate the token: it asks Dhan for a new one via the single HTTP
client (`DhanApiService.renew_token`) and writes it back into `.env` so the next
day's manual app start picks it up.

The `.env` writeback replaces only the ``ACCESS_TOKEN`` line, preserving every other
line byte-for-byte, and verifies the write by reading the file back — restoring the
in-memory original on any mismatch so the file can never be left corrupted. No backup
file is created.

Importable: the snapshot automation thread calls ``renew_access_token`` after prune.
Manual:     ``python -m services.token_renewal_service``
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from config import get_app_config
from services.dhan_api_service import DhanApiService

logger = logging.getLogger(__name__)

_TOKEN_KEY = "ACCESS_TOKEN"


def _write_access_token_to_env(env_file_path: Path, new_token: str) -> None:
    """Replace the ``ACCESS_TOKEN`` line in ``.env`` in place; preserve every other line.

    Rewrites in place (truncate + write + fsync), then reads back and restores the
    in-memory original if the new token isn't present. Raises on a verified-failed write.
    """
    if not env_file_path.exists():
        raise FileNotFoundError(f".env not found at {env_file_path}")

    original = env_file_path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    new_line = f"{_TOKEN_KEY}={new_token}\n"

    replaced = False
    for index, line in enumerate(lines):
        if line.lstrip().startswith(f"{_TOKEN_KEY}="):
            lines[index] = new_line
            replaced = True
            break
    if not replaced:
        logger.warning("%s line not found in .env; appending", _TOKEN_KEY)
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        lines.append(new_line)

    new_contents = "".join(lines)
    with env_file_path.open("r+", encoding="utf-8") as handle:
        handle.seek(0)
        handle.truncate()
        handle.write(new_contents)
        handle.flush()
        os.fsync(handle.fileno())

    if new_token not in env_file_path.read_text(encoding="utf-8"):
        with env_file_path.open("r+", encoding="utf-8") as handle:
            handle.seek(0)
            handle.truncate()
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
        raise RuntimeError("Token writeback verification failed; restored original .env")


def renew_access_token(api_service: DhanApiService | None, env_file_path: Path) -> str | None:
    """Renew the Dhan token and persist it to ``.env``. Returns the new token, or None.

    Never raises — failures are logged so the EOD pipeline continues regardless.
    """
    if api_service is None:
        logger.error("Token renewal skipped: no Dhan API service (missing CLIENT_ID/ACCESS_TOKEN).")
        return None

    new_token = api_service.renew_token()
    if not new_token:
        return None  # api_service.renew_token already logged why

    try:
        _write_access_token_to_env(env_file_path, new_token)
    except Exception as exc:  # pragma: no cover - defensive: token rotated but file write failed
        logger.error("Token renewed but .env writeback failed: %s", exc)
        return None

    logger.info("Dhan access token renewed and written to .env.")
    return new_token


def main() -> int:
    config = get_app_config()
    api_service = DhanApiService.from_config(config)
    token = renew_access_token(api_service, config.env_file_path)
    return 0 if token else 1


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)s  %(name)s  %(message)s",
    )
    raise SystemExit(main())
