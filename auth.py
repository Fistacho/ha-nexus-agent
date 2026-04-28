import os
import secrets
from pathlib import Path
from fastapi import HTTPException, Security, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from dotenv import load_dotenv

load_dotenv()

_KEY_FILE = Path(os.getenv("HA_CONFIG_PATH", "/config")) / ".nexus_api_key"
_bearer = HTTPBearer(auto_error=False)

_supervisor_token: str | None = os.getenv("SUPERVISOR_TOKEN")
_ha_token: str | None = os.getenv("HA_TOKEN")


def get_ha_token() -> str:
    """Return whichever HA token is available. SUPERVISOR_TOKEN wins (running as add-on)."""
    token = _supervisor_token or _ha_token or ""
    if not token:
        raise RuntimeError(
            "No HA token found. Set HA_TOKEN in .env or run as a Home Assistant add-on."
        )
    return token


def get_or_create_api_key() -> str:
    """Load API key from disk, or generate and persist a new one."""
    env_key = os.getenv("NEXUS_API_KEY")
    if env_key:
        return env_key

    if _KEY_FILE.exists():
        return _KEY_FILE.read_text().strip()

    key = secrets.token_urlsafe(32)
    try:
        _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _KEY_FILE.write_text(key)
        _KEY_FILE.chmod(0o600)
    except OSError:
        pass
    return key


API_KEY = get_or_create_api_key()


def delete_api_key() -> None:
    """Delete the persisted key file so a new key is generated on next startup."""
    try:
        _KEY_FILE.unlink(missing_ok=True)
    except OSError:
        pass


async def verify_token(credentials: HTTPAuthorizationCredentials = Security(_bearer)) -> str:
    if credentials is None or credentials.credentials != API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
    return credentials.credentials
