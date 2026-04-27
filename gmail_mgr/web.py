import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from email.utils import parseaddr
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .auth import gmail_service
from .messages import (
    add_label,
    batch_permanent_delete,
    batch_trash,
    ensure_label,
    fetch_full,
    fetch_metadata,
    list_message_ids,
)
from .unsubscribe import (
    USER_AGENT,
    extract_body,
    find_body_unsubscribe_links,
    http_unsubscribe,
    mailto_unsubscribe,
    parse_list_unsubscribe,
)

UNSUB_LABEL = "gmail-mgr/unsubscribed"
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="gmail-manager")


@app.exception_handler(Exception)
def json_error_handler(request: Request, exc: Exception):
    """Return JSON for unhandled exceptions so the frontend can surface a real message."""
    return JSONResponse(
        status_code=500,
        content={"error": f"{type(exc).__name__}: {exc}"},
    )


# Senders cache: short TTL, invalidated on writes.
_senders_cache: dict[tuple[str, int, int | None], tuple[float, dict]] = {}
_SENDERS_TTL = 60.0


def _cache_invalidate():
    _senders_cache.clear()


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/profile")
def profile():
    svc = gmail_service()
    p = svc.users().getProfile(userId="me").execute()
    return {
        "email": p["emailAddress"],
        "messages_total": p["messagesTotal"],
        "threads_total": p["threadsTotal"],
    }


@app.get("/api/counts")
def counts():
    """Counts for sidebar: trash, unread, etc."""
    builtin = [
        ("trash", "TRASH"),
        ("unread", "UNREAD"),
        ("inbox", "INBOX"),
        ("spam", "SPAM"),
    ]

    def label_total(label_id: str) -> int:
        try:
            svc = gmail_service()
            resp = svc.users().labels().get(userId="me", id=label_id).execute()
            return int(resp.get("messagesTotal", 0))
        except Exception:
            return 0

    def find_unsub_total() -> int:
        try:
            svc = gmail_service()
            labels = svc.users().labels().list(userId="me").execute().get("labels", [])
            for lbl in labels:
                if lbl["name"] == "gmail-mgr/unsubscribed":
                    full = svc.users().labels().get(userId="me", id=lbl["id"]).execute()
                    return int(full.get("messagesTotal", 0))
        except Exception:
            return 0
        return 0

    out: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {key: pool.submit(label_total, lid) for key, lid in builtin}
        futures["unsubscribed"] = pool.submit(find_unsub_total)
        for key, fut in futures.items():
            out[key] = fut.result()
    return out


@app.post("/api/empty-trash")
def empty_trash():
    svc = gmail_service()
    ids = list_message_ids(svc, label_ids=["TRASH"], include_spam_trash=True)
    if not ids:
        _cache_invalidate()
        return {"deleted": 0}
    deleted = batch_permanent_delete(svc, ids)
    _cache_invalidate()
    return {"deleted": deleted}


@app.get("/api/senders")
def senders(query: str = "", top: int = 100, limit: int | None = None):
    cache_key = (query, top, limit)
    cached = _senders_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _SENDERS_TTL:
        return {**cached[1], "cached": True}

    svc = gmail_service()
    ids = list_message_ids(svc, query=query, max_results=limit)
    if not ids:
        result = {"senders": [], "scanned": 0, "unique": 0}
        _senders_cache[cache_key] = (time.time(), result)
        return result
    meta = fetch_metadata(svc, ids, ["From"])
    counter: Counter[str] = Counter()
    name_for: dict[str, str] = {}
    for m in meta.values():
        hdrs = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
        name, addr = parseaddr(hdrs.get("from", ""))
        addr = addr.lower().strip()
        if not addr:
            continue
        counter[addr] += 1
        if name and addr not in name_for:
            name_for[addr] = name
    out = [
        {"address": addr, "count": cnt, "name": name_for.get(addr, "")}
        for addr, cnt in counter.most_common(top)
    ]
    result = {"senders": out, "scanned": len(meta), "unique": len(counter)}
    _senders_cache[cache_key] = (time.time(), result)
    return result


@app.get("/api/messages")
def messages(query: str, limit: int = 100):
    svc = gmail_service()
    ids = list_message_ids(svc, query=query, max_results=limit)
    if not ids:
        return {"messages": []}
    meta = fetch_metadata(svc, ids, ["From", "Subject", "Date"])
    out = []
    for mid, m in meta.items():
        hdrs = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
        _, addr = parseaddr(hdrs.get("from", ""))
        out.append({
            "id": mid,
            "from": addr or hdrs.get("from", ""),
            "subject": hdrs.get("subject", ""),
            "date": hdrs.get("date", ""),
            "snippet": m.get("snippet", ""),
        })
    return {"messages": out}


class QueryReq(BaseModel):
    query: str


class SendersReq(BaseModel):
    senders: list[str]


@app.post("/api/delete-query")
def delete_query(req: QueryReq):
    svc = gmail_service()
    ids = list_message_ids(svc, query=req.query)
    if not ids:
        return {"trashed": 0}
    trashed = batch_trash(svc, ids)
    _cache_invalidate()
    return {"trashed": trashed}


@app.post("/api/delete-senders")
def delete_senders(req: SendersReq):
    svc = gmail_service()
    all_ids: list[str] = []
    per_sender: dict[str, int] = {}
    for sender in req.senders:
        ids = list_message_ids(svc, query=f"from:{sender}")
        per_sender[sender] = len(ids)
        all_ids.extend(ids)
    all_ids = list(dict.fromkeys(all_ids))
    if not all_ids:
        return {"trashed": 0, "per_sender": per_sender}
    trashed = batch_trash(svc, all_ids)
    _cache_invalidate()
    return {"trashed": trashed, "per_sender": per_sender}


class UnsubscribeReq(BaseModel):
    query: str | None = None
    senders: list[str] | None = None
    limit: int | None = None
    label: bool = True


def _run_unsubscribe(service, message_ids: list[str], label: bool) -> dict:
    me = service.users().getProfile(userId="me").execute()["emailAddress"]
    msgs = fetch_full(service, message_ids)

    by_sender: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for mid, m in msgs.items():
        hdrs = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
        _, sender = parseaddr(hdrs.get("from", ""))
        sender = sender.lower()
        if sender:
            by_sender[sender].append((mid, m))

    results: list[dict] = []
    successful_senders: list[str] = []

    with httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True) as http:
        for sender, group in by_sender.items():
            lu_http: list[str] = []
            lu_mailto: list[str] = []
            body_links: list[str] = []
            lu_post_one_click = False

            for _, m in group:
                payload = m.get("payload", {})
                hdrs = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
                lu = parse_list_unsubscribe(hdrs.get("list-unsubscribe", ""))
                for u in lu["http"]:
                    if u not in lu_http:
                        lu_http.append(u)
                for u in lu["mailto"]:
                    if u not in lu_mailto:
                        lu_mailto.append(u)
                if "one-click" in hdrs.get("list-unsubscribe-post", "").lower():
                    lu_post_one_click = True
                text, html = extract_body(payload)
                for u in find_body_unsubscribe_links(text, html):
                    if u not in body_links:
                        body_links.append(u)

            success = False
            method: str | None = None
            detail = ""

            if lu_post_one_click and lu_http:
                for url in lu_http:
                    success, detail = http_unsubscribe(http, url, one_click_post=True)
                    method = "header-post"
                    if success:
                        break
            if not success and lu_http:
                for url in lu_http:
                    success, detail = http_unsubscribe(http, url, one_click_post=False)
                    method = "header-get"
                    if success:
                        break
            if not success and lu_mailto:
                for mt in lu_mailto:
                    success, detail = mailto_unsubscribe(service, mt, me)
                    method = "mailto"
                    if success:
                        break
            if not success and body_links:
                for url in body_links[:3]:
                    success, detail = http_unsubscribe(http, url, one_click_post=False)
                    method = "body-link"
                    if success:
                        break

            if method is None:
                method = "none"
                detail = "no candidates"

            results.append({
                "sender": sender,
                "method": method,
                "success": success,
                "detail": detail,
            })
            if success:
                successful_senders.append(sender)

    labeled = 0
    if label and successful_senders:
        label_id = ensure_label(service, UNSUB_LABEL)
        all_to_label: set[str] = set()
        for sender in successful_senders:
            try:
                all_to_label.update(list_message_ids(service, query=f"from:{sender}"))
            except Exception:
                pass
        if all_to_label:
            add_label(service, list(all_to_label), label_id)
            labeled = len(all_to_label)

    return {"results": results, "labeled": labeled}


@app.post("/api/unsubscribe")
def unsubscribe(req: UnsubscribeReq):
    svc = gmail_service()
    if req.senders:
        query = " OR ".join(f"from:{s}" for s in req.senders)
    elif req.query:
        query = req.query
    else:
        return {"results": [], "labeled": 0}
    ids = list_message_ids(svc, query=query, max_results=req.limit)
    if not ids:
        return {"results": [], "labeled": 0}
    result = _run_unsubscribe(svc, ids, req.label)
    _cache_invalidate()
    return result
