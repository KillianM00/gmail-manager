import json
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://mail.google.com/",
]

ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS_PATH = ROOT / "credentials.json"
TOKEN_PATH = ROOT / "token.json"


def _token_has_scopes(required: list[str]) -> bool:
    try:
        data = json.loads(TOKEN_PATH.read_text())
    except Exception:
        return False
    granted = set(data.get("scopes") or [])
    return set(required).issubset(granted)


def load_credentials() -> Credentials:
    creds: Credentials | None = None
    if TOKEN_PATH.exists() and _token_has_scopes(SCOPES):
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        except Exception:
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json())
            return creds
        except Exception:
            creds = None

    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"Missing {CREDENTIALS_PATH}. Download an OAuth desktop client "
            "JSON from Google Cloud Console and place it there."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    TOKEN_PATH.write_text(creds.to_json())
    return creds


def gmail_service():
    return build("gmail", "v1", credentials=load_credentials(), cache_discovery=False)
