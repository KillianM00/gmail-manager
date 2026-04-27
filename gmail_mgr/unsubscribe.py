import base64
import email.message
import ipaddress
import re
import socket
import urllib.parse
from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup


def _is_private_host(host: str) -> bool:
    """True if `host` resolves to a loopback / private / link-local / multicast IP."""
    if not host:
        return True
    host = host.strip().rstrip(".").lower()
    # localhost variants without DNS
    if host in {"localhost", "ip6-localhost", "ip6-loopback"}:
        return True
    # bracketed IPv6 literals
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    # try as literal IP first
    try:
        ip = ipaddress.ip_address(host)
        return (
            ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified
        )
    except ValueError:
        pass
    # resolve hostname; reject if any returned address is private
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True  # can't resolve = don't trust it
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
            if (
                ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified
            ):
                return True
        except ValueError:
            return True
    return False


def is_safe_unsub_url(url: str) -> bool:
    """Refuse URLs that point at loopback / RFC1918 / link-local / cloud-metadata."""
    try:
        u = urllib.parse.urlparse(url)
    except Exception:
        return False
    if u.scheme not in ("http", "https"):
        return False
    if not u.hostname:
        return False
    return not _is_private_host(u.hostname)

UNSUB_TEXT_RE = re.compile(
    r"unsubscribe|opt[\s\-]?out|stop\s+receiving|email\s+preferences|manage\s+(subscription|preferences)|remove\s+me",
    re.IGNORECASE,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 gmail-mgr/0.1"
)


@dataclass
class UnsubAttempt:
    message_id: str
    sender: str
    subject: str
    method: str | None = None
    success: bool = False
    detail: str = ""
    candidates: dict = field(default_factory=dict)


def parse_list_unsubscribe(header_value: str) -> dict:
    """Return {'mailto': [...], 'http': [...]} from a List-Unsubscribe header."""
    out = {"mailto": [], "http": []}
    if not header_value:
        return out
    for m in re.finditer(r"<([^>]+)>", header_value):
        url = m.group(1).strip()
        low = url.lower()
        if low.startswith("mailto:"):
            out["mailto"].append(url[7:])
        elif low.startswith(("http://", "https://")):
            out["http"].append(url)
    return out


def extract_body(payload: dict) -> tuple[str, str]:
    """Return (text, html) decoded from a Gmail message payload, walking parts."""
    text_parts: list[str] = []
    html_parts: list[str] = []

    def walk(part):
        mime = part.get("mimeType", "") or ""
        body = part.get("body", {}) or {}
        data = body.get("data")
        if data:
            try:
                decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
                    "utf-8", errors="replace"
                )
            except Exception:
                decoded = ""
            if mime == "text/plain":
                text_parts.append(decoded)
            elif mime == "text/html":
                html_parts.append(decoded)
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload or {})
    return "\n".join(text_parts), "\n".join(html_parts)


_GENERIC_LINK_TEXT = {"here", "click here", "click", "this link", "this", "link", ""}


def find_body_unsubscribe_links(text: str, html: str) -> list[str]:
    """Find unsubscribe URLs in body.

    Three passes:
      1. Anchor whose text or URL contains an unsub keyword (strong).
      2. Anchor with generic text ("here", "click here") inside a parent block whose text mentions unsubscribing (weak).
      3. Plain-text URLs that contain an unsub keyword.
    """
    candidates: list[str] = []
    matched_anchor_ids: set[int] = set()

    if html:
        try:
            soup = BeautifulSoup(html, "html.parser")

            # Pass 1: strong matches.
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if not href.lower().startswith(("http://", "https://")):
                    continue
                link_text = (a.get_text(" ") or "").strip()
                href_clean = href.split("#", 1)[0]
                if UNSUB_TEXT_RE.search(link_text) or UNSUB_TEXT_RE.search(href_clean):
                    candidates.append(href)
                    matched_anchor_ids.add(id(a))

            # Pass 2: weak matches — generic anchor text inside an unsub-y parent.
            for a in soup.find_all("a", href=True):
                if id(a) in matched_anchor_ids:
                    continue
                href = a["href"].strip()
                if not href.lower().startswith(("http://", "https://")):
                    continue
                link_text = (a.get_text(" ") or "").strip().lower()
                if link_text not in _GENERIC_LINK_TEXT:
                    continue

                parent = a.find_parent()
                for _ in range(4):
                    if parent is None:
                        break
                    parent_text = parent.get_text(" ", strip=True)
                    if parent_text and len(parent_text) < 1500 and UNSUB_TEXT_RE.search(parent_text):
                        candidates.append(href)
                        break
                    parent = parent.find_parent()
        except Exception:
            pass

    if text:
        for m in re.finditer(r"https?://[^\s<>\"']+", text):
            url = m.group(0).rstrip(".,;:>)\"'")
            if UNSUB_TEXT_RE.search(url):
                candidates.append(url)

    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def http_unsubscribe(
    client: httpx.Client,
    url: str,
    one_click_post: bool,
) -> tuple[bool, str]:
    """Hit the URL. For one_click_post, send RFC 8058 POST. Otherwise GET, then follow a confirm form if present."""
    if not is_safe_unsub_url(url):
        return False, "blocked: private / loopback / non-http URL"
    try:
        if one_click_post:
            r = client.post(
                url,
                content=b"List-Unsubscribe=One-Click",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=True,
                timeout=20,
            )
            return (r.status_code < 400, f"POST {r.status_code}")

        r = client.get(url, follow_redirects=True, timeout=20)
        if r.status_code >= 400:
            return False, f"GET {r.status_code}"

        ctype = r.headers.get("content-type", "").lower()
        if "html" not in ctype:
            return True, f"GET {r.status_code}"

        # Look for a confirmation form on the response page.
        try:
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception:
            return True, f"GET {r.status_code} (no parse)"

        for form in soup.find_all("form"):
            form_text = form.get_text(" ").lower()
            buttons = " ".join(
                (b.get("value", "") + " " + b.get_text(" "))
                for b in form.find_all(["button", "input"])
            ).lower()
            haystack = form_text + " " + buttons
            if not (UNSUB_TEXT_RE.search(haystack) or "confirm" in haystack):
                continue

            action = (form.get("action") or str(r.url)).strip()
            method = (form.get("method") or "post").lower()
            payload: dict[str, str] = {}
            for inp in form.find_all(["input", "textarea", "select"]):
                name = inp.get("name")
                if not name:
                    continue
                payload[name] = inp.get("value", "")
            if action.startswith("http"):
                action_url = action
            else:
                action_url = str(httpx.URL(str(r.url)).join(action))

            if not is_safe_unsub_url(action_url):
                return False, "blocked form action: private / loopback URL"

            try:
                if method == "post":
                    r2 = client.post(action_url, data=payload, follow_redirects=True, timeout=20)
                else:
                    r2 = client.get(action_url, params=payload, follow_redirects=True, timeout=20)
                return (r2.status_code < 400, f"GET {r.status_code} → form {r2.status_code}")
            except Exception as e:
                return False, f"form submit failed: {e}"

        return True, f"GET {r.status_code} (no form)"
    except httpx.TimeoutException:
        return False, "timeout"
    except Exception as e:
        return False, f"error: {type(e).__name__}: {e}"


def mailto_unsubscribe(service, mailto: str, from_addr: str) -> tuple[bool, str]:
    """Send an unsubscribe email via Gmail API."""
    if mailto.lower().startswith("mailto:"):
        mailto = mailto[7:]
    parsed = urllib.parse.urlparse("mailto:" + mailto)
    to_addr = parsed.path
    params = urllib.parse.parse_qs(parsed.query)
    subject = params.get("subject", ["unsubscribe"])[0] or "unsubscribe"
    body = params.get("body", [""])[0] or "unsubscribe"

    if not to_addr or "@" not in to_addr:
        return False, "no recipient"

    msg = email.message.EmailMessage()
    msg["To"] = to_addr
    msg["From"] = from_addr
    msg["Subject"] = subject
    msg.set_content(body)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    try:
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return True, f"emailed {to_addr}"
    except Exception as e:
        return False, f"send failed: {type(e).__name__}: {e}"
