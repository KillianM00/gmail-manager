import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from email.utils import parseaddr
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import subs
from .auth import gmail_service
from .messages import (
    add_label,
    batch_archive,
    batch_mark_read,
    batch_permanent_delete,
    batch_restore,
    batch_trash,
    create_block_filter,
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
    return JSONResponse(
        status_code=500,
        content={"error": f"{type(exc).__name__}: {exc}"},
    )


_senders_cache: dict[tuple, tuple[float, dict]] = {}
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


def _aggregate_senders(
    meta: dict,
    *,
    metric: str,
    group: str,
    top: int,
) -> tuple[list[dict], int]:
    """Group `meta` by address or domain, ranked by count or total bytes.

    Returns (rows, unique_count). When grouping by domain, each row's
    `address` is the domain and `addresses` lists all original senders folded
    into that domain.
    """
    by_key_count: Counter[str] = Counter()
    by_key_bytes: dict[str, int] = defaultdict(int)
    name_for: dict[str, str] = {}
    addrs_in_key: dict[str, set[str]] = defaultdict(set)

    for m in meta.values():
        hdrs = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
        name, addr = parseaddr(hdrs.get("from", ""))
        addr = addr.lower().strip()
        if not addr:
            continue
        key = addr.split("@", 1)[1] if (group == "domain" and "@" in addr) else addr
        size = int(m.get("sizeEstimate") or 0)
        by_key_count[key] += 1
        by_key_bytes[key] += size
        addrs_in_key[key].add(addr)
        if name and key not in name_for:
            name_for[key] = name

    if metric == "size":
        ranked = sorted(by_key_bytes.items(), key=lambda kv: kv[1], reverse=True)
    else:
        ranked = by_key_count.most_common()

    rows = []
    for key, _ in ranked[:top]:
        rows.append({
            "address": key,
            "count": by_key_count[key],
            "bytes": by_key_bytes[key],
            "name": name_for.get(key, ""),
            "addresses": sorted(addrs_in_key[key]) if group == "domain" else [key],
            "is_domain": group == "domain",
        })
    return rows, len(by_key_count)


@app.get("/api/senders")
def senders(
    query: str = "",
    top: int = 100,
    limit: int | None = None,
    metric: str = "count",
    group: str = "address",
):
    if metric not in ("count", "size"):
        metric = "count"
    if group not in ("address", "domain"):
        group = "address"

    cache_key = (query, top, limit, metric, group)
    cached = _senders_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _SENDERS_TTL:
        return {**cached[1], "cached": True}

    svc = gmail_service()
    ids = list_message_ids(svc, query=query, max_results=limit)
    if not ids:
        result = {"senders": [], "scanned": 0, "unique": 0, "metric": metric, "group": group}
        _senders_cache[cache_key] = (time.time(), result)
        return result

    # For size metric we don't need any extra fields — sizeEstimate is on the
    # message envelope itself, returned by both metadata and full formats.
    meta = fetch_metadata(svc, ids, ["From"])
    rows, unique = _aggregate_senders(meta, metric=metric, group=group, top=top)
    result = {
        "senders": rows,
        "scanned": len(meta),
        "unique": unique,
        "metric": metric,
        "group": group,
    }

    # Best-effort registry update (address-only rows)
    if group == "address":
        try:
            subs.upsert_seen([
                {"address": r["address"], "name": r["name"], "count": r["count"], "bytes": r["bytes"]}
                for r in rows
            ])
        except Exception:
            pass

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
    block: bool = False  # also create Gmail filter that auto-trashes future mail


def _expand_to_addresses(items: list[str]) -> list[str]:
    """Items can be either single addresses (`foo@bar.com`) or domains (`bar.com`).

    For domains, expand to a Gmail-search-friendly form so we still match every
    sender on that domain.
    """
    return [s.strip().lower() for s in items if s and s.strip()]


def _ids_for_targets(svc, targets: list[str]) -> tuple[list[str], dict[str, int]]:
    """Find message IDs for a list of address/domain targets, with per-target counts."""
    all_ids: list[str] = []
    per: dict[str, int] = {}
    for t in targets:
        # `from:` operator accepts both `user@domain` and just `domain` in Gmail
        ids = list_message_ids(svc, query=f"from:{t}")
        per[t] = len(ids)
        all_ids.extend(ids)
    return list(dict.fromkeys(all_ids)), per


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
    targets = _expand_to_addresses(req.senders)
    all_ids, per = _ids_for_targets(svc, targets)
    if not all_ids:
        if req.block:
            blocked = _block_targets(svc, targets)
            subs.set_status(targets, "blocked", note="auto-trash filter created")
            return {"trashed": 0, "per_sender": per, "blocked": blocked}
        return {"trashed": 0, "per_sender": per}
    trashed = batch_trash(svc, all_ids)
    subs.set_status(targets, "trashed")
    blocked = 0
    if req.block:
        blocked = _block_targets(svc, targets)
        subs.set_status(targets, "blocked", note="auto-trash filter created")
    _cache_invalidate()
    return {"trashed": trashed, "per_sender": per, "blocked": blocked}


@app.post("/api/archive-senders")
def archive_senders(req: SendersReq):
    svc = gmail_service()
    targets = _expand_to_addresses(req.senders)
    all_ids, per = _ids_for_targets(svc, targets)
    if not all_ids:
        return {"archived": 0, "per_sender": per}
    archived = batch_archive(svc, all_ids)
    subs.set_status(targets, "archived")
    _cache_invalidate()
    return {"archived": archived, "per_sender": per}


@app.post("/api/mark-read-senders")
def mark_read_senders(req: SendersReq):
    svc = gmail_service()
    targets = _expand_to_addresses(req.senders)
    all_ids, per = _ids_for_targets(svc, targets)
    if not all_ids:
        return {"marked_read": 0, "per_sender": per}
    marked = batch_mark_read(svc, all_ids)
    _cache_invalidate()
    return {"marked_read": marked, "per_sender": per}


@app.post("/api/restore-senders")
def restore_senders(req: SendersReq):
    """Restore from trash for a list of sender addresses."""
    svc = gmail_service()
    targets = _expand_to_addresses(req.senders)
    all_ids: list[str] = []
    per: dict[str, int] = {}
    for t in targets:
        ids = list_message_ids(svc, query=f"in:trash from:{t}")
        per[t] = len(ids)
        all_ids.extend(ids)
    all_ids = list(dict.fromkeys(all_ids))
    if not all_ids:
        return {"restored": 0, "per_sender": per}
    restored = batch_restore(svc, all_ids)
    subs.set_status(targets, "active", note="restored from trash")
    _cache_invalidate()
    return {"restored": restored, "per_sender": per}


def _block_targets(svc, targets: list[str]) -> int:
    """Create Gmail filter rules to auto-trash future mail from each target."""
    n = 0
    for t in targets:
        try:
            fid = create_block_filter(svc, t)
            if fid:
                n += 1
        except Exception:
            continue
    return n


@app.post("/api/block-senders")
def block_senders(req: SendersReq):
    svc = gmail_service()
    targets = _expand_to_addresses(req.senders)
    blocked = _block_targets(svc, targets)
    subs.set_status(targets, "blocked", note="auto-trash filter created")
    return {"blocked": blocked}


# ---------- subscription registry ----------

@app.get("/api/subs")
def list_subs(status: str | None = None, domain: str | None = None, limit: int = 500):
    rows = subs.list_senders(status=status, domain=domain, limit=limit)
    return {"subs": rows, "stats": subs.stats()}


# ---------- unsubscribe ----------

class UnsubscribeReq(BaseModel):
    query: str | None = None
    senders: list[str] | None = None
    limit: int | None = None
    label: bool = True
    allow_body_links: bool = False  # default off — see README security section


def _run_unsubscribe(service, message_ids: list[str], label: bool, allow_body_links: bool) -> dict:
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
                if allow_body_links:
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
        subs.set_status(successful_senders, "unsubscribed")

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
    result = _run_unsubscribe(svc, ids, req.label, req.allow_body_links)
    _cache_invalidate()
    return result
