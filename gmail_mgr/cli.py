import sys
from collections import Counter, defaultdict
from email.utils import parseaddr

import click
import httpx
from rich.console import Console

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from .auth import TOKEN_PATH, gmail_service
from .messages import (
    add_label,
    batch_trash,
    ensure_label,
    fetch_full,
    fetch_metadata,
    list_message_ids,
)
from .unsubscribe import (
    USER_AGENT,
    UnsubAttempt,
    extract_body,
    find_body_unsubscribe_links,
    http_unsubscribe,
    mailto_unsubscribe,
    parse_list_unsubscribe,
)

console = Console()
UNSUB_LABEL = "gmail-mgr/unsubscribed"


def _progress(description: str, total: int):
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )


@click.group()
def main():
    """gmail-mgr — bulk Gmail management."""


@main.command()
def auth():
    """Run the OAuth flow and cache the token locally."""
    svc = gmail_service()
    profile = svc.users().getProfile(userId="me").execute()
    console.print(f"[green]Signed in as[/green] {profile['emailAddress']}")
    console.print(f"Total messages: {profile['messagesTotal']}")
    console.print(f"Total threads:  {profile['threadsTotal']}")
    console.print(f"Token cached at: {TOKEN_PATH}")


@main.command()
def whoami():
    """Show the currently authenticated account."""
    svc = gmail_service()
    profile = svc.users().getProfile(userId="me").execute()
    console.print(profile)


@main.command()
@click.option("--port", default=8000, type=int, help="Port to bind (default: 8000).")
@click.option("--no-browser", is_flag=True, help="Don't auto-open the browser.")
def serve(port, no_browser):
    """Start the local web GUI at http://localhost:PORT."""
    import threading
    import webbrowser

    import uvicorn

    url = f"http://localhost:{port}"
    if not no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    console.print(f"[green]gmail-manager UI running at[/green] [cyan]{url}[/cyan]")
    console.print("[dim]Press Ctrl+C to stop.[/dim]")
    uvicorn.run("gmail_mgr.web:app", host="127.0.0.1", port=port, log_level="warning")


@main.command()
@click.option("--query", default="", help='Gmail search query (default: all non-trash mail). Example: --query "in:inbox"')
@click.option("--top", type=int, default=50, help="Show top N senders.")
@click.option("--limit", type=int, default=None, help="Cap how many messages to scan (for fast testing).")
@click.option("--include-spam-trash", is_flag=True, help="Include spam and trash in the scan.")
def senders(query, top, limit, include_spam_trash):
    """Group messages by sender and show counts."""
    svc = gmail_service()

    with console.status("[dim]Listing message IDs...[/dim]"):
        ids = list_message_ids(svc, query=query, include_spam_trash=include_spam_trash, max_results=limit)
    if not ids:
        console.print("[yellow]No messages matched.[/yellow]")
        return

    counter: Counter[str] = Counter()
    name_for: dict[str, str] = {}

    with Progress(
        SpinnerColumn(),
        TextColumn("Fetching headers"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as prog:
        task = prog.add_task("fetch", total=len(ids))

        def on_progress(done):
            prog.update(task, completed=done)

        meta = fetch_metadata(svc, ids, ["From"], progress_cb=on_progress)

    for m in meta.values():
        hdrs = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
        from_hdr = hdrs.get("from", "")
        name, addr = parseaddr(from_hdr)
        addr = addr.lower().strip()
        if not addr:
            continue
        counter[addr] += 1
        if name and addr not in name_for:
            name_for[addr] = name

    table = Table(title=f"Top {top} senders — {len(meta)} messages scanned")
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Count", justify="right")
    table.add_column("Sender")
    table.add_column("Name", overflow="fold")
    for i, (addr, cnt) in enumerate(counter.most_common(top), 1):
        table.add_row(str(i), str(cnt), addr, name_for.get(addr, ""))
    console.print(table)
    console.print(f"\n[dim]Total unique senders: {len(counter)}[/dim]")


@main.command(name="list")
@click.option("--query", required=True, help='Gmail search query. Example: --query "from:noreply@example.com"')
@click.option("--limit", type=int, default=50)
def list_cmd(query, limit):
    """Preview messages matching a query."""
    svc = gmail_service()
    ids = list_message_ids(svc, query=query, max_results=limit)
    if not ids:
        console.print("[yellow]No matches.[/yellow]")
        return
    meta = fetch_metadata(svc, ids, ["From", "Subject", "Date"])

    table = Table(title=f"{len(meta)} matches for: {query}")
    table.add_column("From", overflow="fold", max_width=35)
    table.add_column("Subject", overflow="fold")
    table.add_column("Date", overflow="fold", max_width=20)
    for m in meta.values():
        h = {x["name"].lower(): x["value"] for x in m.get("payload", {}).get("headers", [])}
        _, addr = parseaddr(h.get("from", ""))
        table.add_row(addr or h.get("from", ""), h.get("subject", ""), h.get("date", ""))
    console.print(table)


@main.command()
@click.option("--query", required=True, help='Gmail search query. Example: --query "from:noreply@example.com"')
@click.option("--yes", is_flag=True, help="Skip confirmation.")
def delete(query, yes):
    """Bulk-trash all messages matching a query (recoverable for 30 days)."""
    svc = gmail_service()
    with console.status("[dim]Finding matches...[/dim]"):
        ids = list_message_ids(svc, query=query)
    if not ids:
        console.print("[yellow]No matches.[/yellow]")
        return

    console.print(f"[red]About to trash {len(ids)} messages[/red] matching: [cyan]{query}[/cyan]")
    if not yes and not click.confirm("Continue?", default=False):
        console.print("Cancelled.")
        return

    with console.status(f"Trashing {len(ids)} messages..."):
        trashed = batch_trash(svc, ids)
    console.print(f"[green]Trashed {trashed} messages.[/green] Recoverable from Trash for 30 days.")


@main.command(name="delete-from")
@click.option("--sender", "senders", multiple=True, help="Sender email (repeatable, e.g. --sender foo@bar.com --sender baz@qux.com).")
@click.option("--from-file", type=click.Path(exists=True, dir_okay=False), help="File with one sender per line (# for comments).")
@click.option("--yes", is_flag=True, help="Skip confirmation.")
def delete_from(senders, from_file, yes):
    """Bulk-trash all messages from a list of senders."""
    sender_list = list(senders)
    if from_file:
        with open(from_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    sender_list.append(line)
    if not sender_list:
        console.print("[red]No senders provided. Use --sender or --from-file.[/red]")
        return

    svc = gmail_service()
    all_ids: list[str] = []
    table = Table(title=f"Looking up {len(sender_list)} senders")
    table.add_column("Sender", overflow="fold")
    table.add_column("Messages", justify="right")
    with console.status("Searching..."):
        for sender in sender_list:
            ids = list_message_ids(svc, query=f"from:{sender}")
            table.add_row(sender, str(len(ids)))
            all_ids.extend(ids)
    console.print(table)

    all_ids = list(dict.fromkeys(all_ids))  # dedupe, preserve order
    if not all_ids:
        console.print("[yellow]No messages found.[/yellow]")
        return

    console.print(f"\n[red]About to trash {len(all_ids)} messages[/red] from {len(sender_list)} senders.")
    if not yes and not click.confirm("Continue?", default=False):
        console.print("Cancelled.")
        return

    with console.status(f"Trashing {len(all_ids)} messages..."):
        trashed = batch_trash(svc, all_ids)
    console.print(f"[green]Trashed {trashed} messages.[/green] Recoverable from Trash for 30 days.")


@main.command()
@click.option("--query", default="in:inbox", help='Gmail search query (default: in:inbox).')
@click.option("--limit", type=int, default=None, help="Cap how many messages to process.")
@click.option("--per-sender/--per-message", default=True, help="Try each sender once vs. each message (default: per-sender).")
@click.option("--dry-run", is_flag=True, help="Plan only — do not send any requests.")
@click.option(
    "--methods",
    default="header-post,header-get,mailto,body-link",
    help="Comma-separated methods in priority order. Choices: header-post, header-get, mailto, body-link.",
)
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
@click.option("--label/--no-label", default=True, help="Add 'gmail-mgr/unsubscribed' label to processed messages (default on).")
def unsubscribe(query, limit, per_sender, dry_run, methods, yes, label):
    """Find unsubscribe headers and links, then execute them with a fallback chain."""
    svc = gmail_service()
    me = svc.users().getProfile(userId="me").execute()["emailAddress"]
    methods_list = [m.strip() for m in methods.split(",") if m.strip()]

    with console.status("[dim]Finding messages...[/dim]"):
        ids = list_message_ids(svc, query=query, max_results=limit)
    if not ids:
        console.print("[yellow]No matches.[/yellow]")
        return

    with Progress(
        SpinnerColumn(),
        TextColumn("Fetching full messages"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as prog:
        task = prog.add_task("fetch", total=len(ids))
        msgs = fetch_full(svc, ids, progress_cb=lambda done: prog.update(task, completed=done))

    # Plan attempts.
    plans: list[UnsubAttempt] = []

    if per_sender:
        # Group by sender and union all candidates across that sender's messages.
        by_sender: dict[str, list[tuple[str, dict]]] = defaultdict(list)
        for mid, m in msgs.items():
            payload = m.get("payload", {})
            headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
            _, sender = parseaddr(headers.get("from", ""))
            sender = sender.lower()
            if sender:
                by_sender[sender].append((mid, m))

        for sender, group in by_sender.items():
            lu_http: list[str] = []
            lu_mailto: list[str] = []
            body_links: list[str] = []
            lu_post_one_click = False
            subject = ""
            all_msg_ids = [mid for mid, _ in group]

            for mid, m in group:
                payload = m.get("payload", {})
                headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
                if not subject:
                    subject = (headers.get("subject", "") or "")[:60]
                lu = parse_list_unsubscribe(headers.get("list-unsubscribe", ""))
                for u in lu["http"]:
                    if u not in lu_http:
                        lu_http.append(u)
                for u in lu["mailto"]:
                    if u not in lu_mailto:
                        lu_mailto.append(u)
                if "one-click" in headers.get("list-unsubscribe-post", "").lower():
                    lu_post_one_click = True
                text, html = extract_body(payload)
                for u in find_body_unsubscribe_links(text, html):
                    if u not in body_links:
                        body_links.append(u)

            plans.append(
                UnsubAttempt(
                    message_id=group[0][0],
                    sender=sender,
                    subject=subject,
                    candidates={
                        "lu_http": lu_http,
                        "lu_mailto": lu_mailto,
                        "lu_post_one_click": lu_post_one_click,
                        "body_links": body_links,
                        "all_message_ids": all_msg_ids,
                    },
                )
            )
    else:
        # Per-message mode — one plan per message.
        for mid, m in msgs.items():
            payload = m.get("payload", {})
            headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
            _, sender = parseaddr(headers.get("from", ""))
            sender = sender.lower()
            if not sender:
                continue
            lu = parse_list_unsubscribe(headers.get("list-unsubscribe", ""))
            text, html = extract_body(payload)
            body_links = find_body_unsubscribe_links(text, html)
            plans.append(
                UnsubAttempt(
                    message_id=mid,
                    sender=sender,
                    subject=(headers.get("subject", "") or "")[:60],
                    candidates={
                        "lu_http": lu["http"],
                        "lu_mailto": lu["mailto"],
                        "lu_post_one_click": "one-click" in headers.get("list-unsubscribe-post", "").lower(),
                        "body_links": body_links,
                        "all_message_ids": [mid],
                    },
                )
            )

    if not plans:
        console.print("[yellow]No senders to unsubscribe from.[/yellow]")
        return

    # Show plan summary.
    have_any = sum(
        1
        for p in plans
        if p.candidates["lu_http"] or p.candidates["lu_mailto"] or p.candidates["body_links"]
    )
    console.print(
        f"\nPlanned: [bold]{len(plans)}[/bold] senders, "
        f"[green]{have_any}[/green] have at least one unsubscribe candidate."
    )

    if not yes and not dry_run:
        if not click.confirm(f"Execute unsubscribe attempts on {len(plans)} senders?", default=False):
            console.print("Cancelled.")
            return

    # Execute.
    label_id = ensure_label(svc, UNSUB_LABEL) if (label and not dry_run) else None
    successful_msg_ids: list[str] = []
    successful_senders: list[str] = []

    with httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True) as http:
        with Progress(
            SpinnerColumn(),
            TextColumn("Unsubscribing"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as prog:
            task = prog.add_task("unsub", total=len(plans))

            for p in plans:
                c = p.candidates
                tried_any = False
                for method in methods_list:
                    if p.success:
                        break

                    if method == "header-post" and c["lu_post_one_click"] and c["lu_http"]:
                        for url in c["lu_http"]:
                            tried_any = True
                            if dry_run:
                                p.success, p.detail = True, f"[dry-run] POST {url[:60]}"
                            else:
                                p.success, p.detail = http_unsubscribe(http, url, one_click_post=True)
                            p.method = "header-post"
                            if p.success:
                                break

                    elif method == "header-get" and c["lu_http"]:
                        for url in c["lu_http"]:
                            tried_any = True
                            if dry_run:
                                p.success, p.detail = True, f"[dry-run] GET {url[:60]}"
                            else:
                                p.success, p.detail = http_unsubscribe(http, url, one_click_post=False)
                            p.method = "header-get"
                            if p.success:
                                break

                    elif method == "mailto" and c["lu_mailto"]:
                        for mt in c["lu_mailto"]:
                            tried_any = True
                            if dry_run:
                                p.success, p.detail = True, f"[dry-run] mail {mt[:60]}"
                            else:
                                p.success, p.detail = mailto_unsubscribe(svc, mt, me)
                            p.method = "mailto"
                            if p.success:
                                break

                    elif method == "body-link" and c["body_links"]:
                        for url in c["body_links"][:3]:  # cap at 3 to avoid abuse
                            tried_any = True
                            if dry_run:
                                p.success, p.detail = True, f"[dry-run] body GET {url[:60]}"
                            else:
                                p.success, p.detail = http_unsubscribe(http, url, one_click_post=False)
                            p.method = "body-link"
                            if p.success:
                                break

                if not tried_any:
                    p.method = "none"
                    p.detail = "no candidates found"

                if p.success:
                    successful_msg_ids.extend(p.candidates.get("all_message_ids", [p.message_id]))
                    successful_senders.append(p.sender)

                prog.update(task, advance=1)

    # Label every message from each successful sender (not just the ones we scanned).
    labeled_count = 0
    if label_id and successful_senders:
        with console.status(f"Labeling messages from {len(successful_senders)} unsubscribed senders..."):
            all_to_label: set[str] = set(successful_msg_ids)
            for sender in successful_senders:
                try:
                    sender_ids = list_message_ids(svc, query=f"from:{sender}")
                    all_to_label.update(sender_ids)
                except Exception:
                    pass
            if all_to_label:
                add_label(svc, list(all_to_label), label_id)
                labeled_count = len(all_to_label)

    # Results table.
    table = Table(title=f"Unsubscribe results ({len(plans)} senders)")
    table.add_column("Sender", overflow="fold", max_width=35)
    table.add_column("Method")
    table.add_column("OK")
    table.add_column("Detail", overflow="fold", max_width=50)
    success_count = 0
    for p in plans:
        if p.success:
            success_count += 1
        marker = "[green]OK[/green]" if p.success else "[red]--[/red]"
        table.add_row(p.sender, p.method or "-", marker, p.detail)
    console.print(table)
    console.print(
        f"\n[bold]{success_count}/{len(plans)}[/bold] senders unsubscribed successfully."
    )
    if label and not dry_run and labeled_count:
        console.print(
            f"Labeled {labeled_count} messages with [cyan]{UNSUB_LABEL}[/cyan] "
            f"(across {len(successful_senders)} senders).\n"
            f"To trash them all: [cyan]gmail-mgr delete --query \"label:{UNSUB_LABEL}\"[/cyan]"
        )


if __name__ == "__main__":
    main()
