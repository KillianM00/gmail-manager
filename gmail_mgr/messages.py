import random
import time
from typing import Iterable

from googleapiclient.errors import HttpError


def _execute_with_retry(req, max_retries: int = 6):
    for attempt in range(max_retries):
        try:
            return req.execute()
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            if status in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                time.sleep((2**attempt) + random.random())
                continue
            raise


def list_message_ids(
    service,
    query: str = "",
    label_ids: list[str] | None = None,
    include_spam_trash: bool = False,
    max_results: int | None = None,
) -> list[str]:
    """List ALL message IDs matching the query, fully paginated."""
    ids: list[str] = []
    req = service.users().messages().list(
        userId="me",
        q=query,
        labelIds=label_ids,
        includeSpamTrash=include_spam_trash,
        maxResults=500,
    )
    while req is not None:
        resp = _execute_with_retry(req)
        for m in resp.get("messages", []):
            ids.append(m["id"])
            if max_results and len(ids) >= max_results:
                return ids
        req = service.users().messages().list_next(req, resp)
    return ids


def _batch_fetch(
    service,
    message_ids: list[str],
    fmt: str,
    metadata_headers: list[str] | None = None,
    progress_cb=None,
) -> dict[str, dict]:
    results: dict[str, dict] = {}

    def make_callback(per_call_errors):
        def callback(request_id, response, exception):
            if exception is not None:
                per_call_errors[request_id] = exception
                return
            results[response["id"]] = response
        return callback

    def run_pass(ids: list[str]) -> dict[str, Exception]:
        BATCH_SIZE = 50
        per_call_errors: dict[str, Exception] = {}
        cb = make_callback(per_call_errors)
        done_count = 0
        for i in range(0, len(ids), BATCH_SIZE):
            chunk = ids[i : i + BATCH_SIZE]
            for attempt in range(4):
                batch = service.new_batch_http_request(callback=cb)
                for mid in chunk:
                    kwargs = {"userId": "me", "id": mid, "format": fmt}
                    if metadata_headers and fmt == "metadata":
                        kwargs["metadataHeaders"] = metadata_headers
                    batch.add(service.users().messages().get(**kwargs), request_id=mid)
                try:
                    batch.execute()
                    break
                except HttpError as e:
                    if attempt < 3 and getattr(e.resp, "status", None) in (429, 500, 502, 503, 504):
                        time.sleep((2**attempt) + random.random())
                        continue
                    raise
            done_count += len(chunk)
            if progress_cb:
                progress_cb(done_count)
        return per_call_errors

    # First pass.
    run_pass(message_ids)

    # Retry any ids not in results, up to 3 follow-up rounds with backoff.
    for round_num in range(3):
        missing = [mid for mid in message_ids if mid not in results]
        if not missing:
            break
        time.sleep((2**round_num) + random.random())
        run_pass(missing)

    return results


def fetch_metadata(service, message_ids: list[str], headers: list[str], progress_cb=None) -> dict[str, dict]:
    return _batch_fetch(service, message_ids, fmt="metadata", metadata_headers=headers, progress_cb=progress_cb)


def fetch_full(service, message_ids: list[str], progress_cb=None) -> dict[str, dict]:
    return _batch_fetch(service, message_ids, fmt="full", progress_cb=progress_cb)


def batch_trash(service, message_ids: list[str]) -> int:
    """Move messages to Trash (recoverable for 30 days)."""
    BATCH_SIZE = 1000
    total = 0
    for i in range(0, len(message_ids), BATCH_SIZE):
        chunk = message_ids[i : i + BATCH_SIZE]
        req = service.users().messages().batchModify(
            userId="me",
            body={
                "ids": chunk,
                "addLabelIds": ["TRASH"],
                "removeLabelIds": ["INBOX", "UNREAD"],
            },
        )
        _execute_with_retry(req)
        total += len(chunk)
    return total


def batch_permanent_delete(service, message_ids: list[str]) -> int:
    """Permanently delete messages (requires https://mail.google.com/ scope). Bypasses Trash."""
    BATCH_SIZE = 1000
    total = 0
    for i in range(0, len(message_ids), BATCH_SIZE):
        chunk = message_ids[i : i + BATCH_SIZE]
        req = service.users().messages().batchDelete(
            userId="me",
            body={"ids": chunk},
        )
        _execute_with_retry(req)
        total += len(chunk)
    return total


def add_label(service, message_ids: Iterable[str], label_id: str) -> None:
    ids = list(message_ids)
    if not ids:
        return
    BATCH_SIZE = 1000
    for i in range(0, len(ids), BATCH_SIZE):
        req = service.users().messages().batchModify(
            userId="me",
            body={"ids": ids[i : i + BATCH_SIZE], "addLabelIds": [label_id]},
        )
        _execute_with_retry(req)


def ensure_label(service, name: str) -> str:
    """Get-or-create a Gmail label by name. Returns the label ID."""
    resp = _execute_with_retry(service.users().labels().list(userId="me"))
    for lbl in resp.get("labels", []):
        if lbl["name"] == name:
            return lbl["id"]
    created = _execute_with_retry(
        service.users().labels().create(
            userId="me",
            body={
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
    )
    return created["id"]
