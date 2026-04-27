import json
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from . import config as user_config

SCOPES = [
    "https://mail.google.com/",
]

ROOT = Path(__file__).resolve().parent.parent
# credentials.json: project root for legacy, $XDG_CONFIG_HOME/gmail-mgr for new installs
_LEGACY_CREDENTIALS = ROOT / "credentials.json"
_LEGACY_TOKEN = ROOT / "token.json"
_USER_CREDENTIALS = user_config.CONFIG_DIR / "credentials.json"
_USER_TOKEN = user_config.CONFIG_DIR / "token.json"


def _pick_path(legacy: Path, user: Path) -> Path:
    """Prefer the user-config-dir path; fall back to project-root legacy path."""
    if user.exists():
        return user
    if legacy.exists():
        return legacy
    return user  # use user dir for new files


def credentials_path() -> Path:
    return _pick_path(_LEGACY_CREDENTIALS, _USER_CREDENTIALS)


def token_path() -> Path:
    return _pick_path(_LEGACY_TOKEN, _USER_TOKEN)


# Backwards compat: old code imports CREDENTIALS_PATH / TOKEN_PATH directly.
CREDENTIALS_PATH = credentials_path()
TOKEN_PATH = token_path()


def _token_has_scopes(token: Path, required: list[str]) -> bool:
    try:
        data = json.loads(token.read_text())
    except Exception:
        return False
    granted = set(data.get("scopes") or [])
    return set(required).issubset(granted)


def load_credentials() -> Credentials:
    token = token_path()
    creds_file = credentials_path()
    creds: Credentials | None = None
    if token.exists() and _token_has_scopes(token, SCOPES):
        try:
            creds = Credentials.from_authorized_user_file(str(token), SCOPES)
        except Exception:
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token.write_text(creds.to_json())
            return creds
        except Exception:
            creds = None

    if not creds_file.exists():
        raise FileNotFoundError(
            f"Missing OAuth client file. Run `gmail-mgr setup` to walk through "
            f"the one-time Google Cloud Console setup, or drop your downloaded "
            f"credentials.json at: {creds_file}"
        )

    # Honor user's preferred browser for the consent screen.
    user_config.configure_oauth_environment()

    # Make sure the destination dir exists (first run with user-config path).
    token.parent.mkdir(parents=True, exist_ok=True)

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    token.write_text(creds.to_json())
    return creds


def gmail_service():
    return build("gmail", "v1", credentials=load_credentials(), cache_discovery=False)
