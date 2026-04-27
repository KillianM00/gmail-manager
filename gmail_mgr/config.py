"""Per-user config stored at ~/.gmail-mgr/config.json.

Currently tracks:
  - browser preference (used by setup wizard, OAuth flow, `serve` GUI launch)
"""
import json
import os
import platform
import shutil
import webbrowser
from pathlib import Path

CONFIG_DIR = Path.home() / ".gmail-mgr"
CONFIG_PATH = CONFIG_DIR / "config.json"
SUBS_DB_PATH = CONFIG_DIR / "subs.db"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def get(key: str, default=None):
    return load_config().get(key, default)


def set_(key: str, value) -> None:
    cfg = load_config()
    cfg[key] = value
    save_config(cfg)


# ---------- browser handling ----------

# Friendly name -> list of candidate executables / windows registry app names
# in priority order. We probe each to find what's actually installed.
_BROWSER_CANDIDATES: dict[str, list[str]] = {
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "google-chrome",
        "chrome",
    ],
    "edge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "msedge",
    ],
    "firefox": [
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
        r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
        "firefox",
    ],
    "brave": [
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
        "brave",
    ],
    "safari": [
        "/Applications/Safari.app/Contents/MacOS/Safari",
        "safari",
    ],
    "opera": [
        r"C:\Users\%USERNAME%\AppData\Local\Programs\Opera\opera.exe",
        "opera",
    ],
    "arc": [
        r"C:\Users\%USERNAME%\AppData\Local\Arc\Arc.exe",
        "arc",
    ],
}


def _resolve_browser_path(name: str) -> str | None:
    """Return absolute path to the browser executable, or None if not found."""
    candidates = _BROWSER_CANDIDATES.get(name.lower(), [])
    for cand in candidates:
        expanded = os.path.expandvars(cand)
        if os.path.isabs(expanded) and os.path.exists(expanded):
            return expanded
        which_result = shutil.which(expanded)
        if which_result:
            return which_result
    return None


def detect_installed_browsers() -> list[tuple[str, str]]:
    """List of (friendly_name, exe_path) for browsers we can find on this machine."""
    found = []
    for name in _BROWSER_CANDIDATES:
        path = _resolve_browser_path(name)
        if path:
            found.append((name, path))
    return found


def open_url(url: str) -> bool:
    """Open `url` using the user's preferred browser, falling back to system default.

    Returns True if a browser was launched successfully.
    """
    pref = get("browser")  # may be friendly name, absolute path, or None

    # Try the user's preferred browser
    if pref:
        path = pref if os.path.isabs(pref) and os.path.exists(pref) else _resolve_browser_path(pref)
        if path:
            try:
                # %s is the standard webbrowser placeholder for the URL
                ctrl = webbrowser.get(f'"{path}" %s')
                if ctrl.open(url):
                    return True
            except Exception:
                pass

    # Fall back to system default
    try:
        return webbrowser.open(url, new=1, autoraise=True)
    except Exception:
        return False


def configure_oauth_environment() -> None:
    """Make google-auth-oauthlib's flow.run_local_server use the configured browser.

    InstalledAppFlow.run_local_server() calls webbrowser.open() under the hood.
    We register the user's preferred browser as the default in the webbrowser
    module so that call lands on it.
    """
    pref = get("browser")
    if not pref:
        return
    path = pref if os.path.isabs(pref) and os.path.exists(pref) else _resolve_browser_path(pref)
    if not path:
        return
    # Register a controller and put it at the front of webbrowser's preference list
    try:
        webbrowser.register("gmail-mgr-browser", None, webbrowser.BackgroundBrowser(path), preferred=True)
    except Exception:
        pass


def platform_label() -> str:
    return f"{platform.system()} {platform.release()}".strip()
