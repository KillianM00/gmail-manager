# gmail-manager

A local-first tool for taking control of a Gmail account: bulk-delete by query
or sender, run a real RFC 8058 unsubscribe sweep across every list you're on,
and clear out trash without clicking through Google's UI a hundred times.

Comes with both a CLI and a small web GUI that runs on `localhost`. Your data
never leaves your machine — it talks straight to the Gmail API with an OAuth
token cached in this directory.

## What it does

- **Group senders** — scan any Gmail query (Inbox, last year, a label, anything)
  and see who is actually filling your mailbox, ranked by message count.
- **Mass delete** — trash everything from a sender, a list of senders, or any
  Gmail query. Recoverable from Trash for 30 days.
- **Empty Trash** — permanently delete everything in Trash in one click.
  Bypasses the 30-day window.
- **Real unsubscribe sweep** — for each sender:
  1. RFC 8058 one-click POST (`List-Unsubscribe-Post: List-Unsubscribe=One-Click`)
  2. `List-Unsubscribe` HTTP GET fallback
  3. `List-Unsubscribe` `mailto:` (sends an unsubscribe email on your behalf)
  4. Body-link scraping (parses HTML for "unsubscribe" anchors, including
     parent-context "click here" links)
  Successfully-unsubscribed senders get tagged with the
  `gmail-mgr/unsubscribed` label so you can review or trash them later.
- **GUI** — simple sidebar nav (Inbox / Unread / Promotions / Social / Updates /
  Older 1y / All mail / Spam / Trash / Unsubscribed), per-sender or bulk
  Unsub/Trash actions, custom query input, instant client-side filter.

## Install

Requires Python 3.10+.

```bash
# Clone
git clone https://github.com/KillianM00/gmail-manager.git
cd gmail-manager

# With uv (recommended)
uv sync
uv run gmail-mgr --help

# Or with pip + venv
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -e .
gmail-mgr --help
```

## Google OAuth setup (one-time)

This app uses your own Google Cloud project so you don't have to trust anyone
else with your Gmail. Takes ~5 minutes:

1. Go to <https://console.cloud.google.com/> and create a new project (or pick
   an existing one).
2. **APIs & Services → Library** → search for **Gmail API** → click **Enable**.
3. **APIs & Services → OAuth consent screen**
   - User type: **External**
   - Fill in the required fields (app name, support email, developer email).
   - **Test users**: add your own Gmail address. While the app is in "Testing"
     mode, only listed test users can sign in — that's fine for personal use.
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Download the JSON. Rename it to `credentials.json` and drop it in this
     directory (next to `pyproject.toml`).
5. Run any command — a browser tab will open for consent. Google may show a
   "this app isn't verified" screen; click **Advanced → Go to (your app
   name)**. The granted token is cached in `token.json`.

`credentials.json` and `token.json` are gitignored. Don't commit them.

### What permissions does it ask for?

A single OAuth scope: `https://mail.google.com/` — full mailbox access.
That's required because permanently deleting messages (Empty Trash) is gated
behind the full scope; the narrower `gmail.modify` scope cannot do it.

## CLI

All commands accept `--help`.

```bash
# One-time auth
gmail-mgr auth

# Who am I and how much mail do I have
gmail-mgr whoami

# Top 50 senders in the inbox
gmail-mgr senders --query "in:inbox" --top 50

# Top senders across the whole mailbox (slower; full scan)
gmail-mgr senders --query "" --top 100

# Preview what a query matches without deleting
gmail-mgr list --query "from:promo@example.com" --limit 20

# Trash everything matching a query
gmail-mgr delete --query "older_than:1y category:promotions"

# Trash everything from a list of senders
gmail-mgr delete-from --sender noreply@a.com --sender promos@b.com
gmail-mgr delete-from --from-file senders.txt   # one address per line

# Run a real unsubscribe sweep across every sender in the inbox
gmail-mgr unsubscribe --query "in:inbox"

# Same, but plan-only (no requests sent)
gmail-mgr unsubscribe --query "in:inbox" --dry-run

# Skip senders you've already unsubscribed from
gmail-mgr unsubscribe --query "in:inbox -label:gmail-mgr/unsubscribed"
```

### Useful Gmail query operators

| Query | Matches |
| --- | --- |
| `in:inbox` | messages currently in the inbox |
| `is:unread` | unread messages |
| `category:promotions` | the Promotions tab |
| `older_than:1y` | older than one year |
| `from:foo@bar.com` | from a specific address |
| `label:gmail-mgr/unsubscribed` | senders we've unsubscribed from |
| `-label:foo` | exclude a label (negation) |
| `has:list` | messages with a `List-Unsubscribe` header |

Combine freely: `in:inbox older_than:6m category:promotions -is:starred`.

## GUI

```bash
gmail-mgr serve              # opens http://localhost:8000
gmail-mgr serve --port 8766  # custom port
gmail-mgr serve --no-browser # don't auto-open
```

The sidebar has built-in views (Inbox / Unread / Promotions / Social / Updates
/ Older 1y / All mail / Spam / Trash / Unsubscribed) and an "Empty trash"
button. The toolbar has a custom-query input (any Gmail syntax) and a live
sender filter box.

Workflow:

1. Pick a view from the sidebar.
2. Senders are listed by message count. Use the filter box to narrow down.
3. Per-row **Unsub** / **Trash** buttons act on a single sender.
4. Tick checkboxes to bulk-select; the floating action bar shows the total
   message count and offers **Unsubscribe** / **Move to trash** for everything
   at once.

The senders endpoint is cached for 60 seconds and invalidated automatically
after any delete/unsubscribe/empty-trash, so re-clicking views feels instant.

## How the unsubscribe sweep works

For each unique sender in the scan:

1. **Aggregate candidates across every message from that sender.** A
   transactional message might have no `List-Unsubscribe` header, but a
   marketing email from the same address will — so the sweep unions all
   candidates before trying anything.
2. Try methods in order: RFC 8058 one-click POST → header GET → mailto →
   body-link scrape.
3. On success, label every message from that sender with
   `gmail-mgr/unsubscribed`.

Senders that genuinely cannot be unsubscribed via API tend to be transactional
or security senders (PayPal alerts, GitHub security, Spotify login alerts).
Those don't *have* a real unsubscribe — they're notifications you opted into by
having an account. For those, just trash and move on, or filter them out
client-side.

## Troubleshooting

**"Access blocked: app has not completed Google verification"**
You signed in with an account that isn't on the test-users list. Add it under
**OAuth consent screen → Test users** in the Cloud Console.

**`HttpError 403 ... insufficient authentication scopes`**
Your cached `token.json` was issued with a narrower scope than the app
currently requests. Delete `token.json` and run any command — you'll be sent
through the OAuth flow again to grant the full scope.

**Empty Trash returns 500**
Same root cause as the previous one — the full `https://mail.google.com/`
scope is required to permanently delete. Delete `token.json` and re-auth.

**The sender scan is slow on first hit**
Gmail metadata batches return ~20 messages per second worst-case under quota
pressure. ~1000 messages takes 30–60 s. Subsequent loads of the same view come
from cache (`cached: true` in the meta line) and are near-instant. Refresh
forces a re-fetch.

**Some unsubscribes report "no candidates"**
That sender's emails contain no `List-Unsubscribe` header *and* no parseable
body anchors. Common for transactional/security senders that don't really have
an unsubscribe — see [How the unsubscribe sweep works](#how-the-unsubscribe-sweep-works).

## Project layout

```
gmail_mgr/
├── auth.py          # OAuth flow + token caching
├── messages.py      # list / fetch / batch trash / batch permanent-delete
├── unsubscribe.py   # List-Unsubscribe parsing + body-link scrape + execute
├── cli.py           # click commands: auth, senders, delete, unsubscribe, serve, …
├── web.py           # FastAPI app: /api/profile, /api/counts, /api/senders, …
└── static/
    └── index.html   # single-page GUI
```

## Safety notes

- `delete` and `delete-from` move messages to **Trash**, not to the void.
  Recoverable for 30 days.
- `empty-trash` (and the GUI's **Empty trash** button) **permanently deletes**
  and is not recoverable. There's a confirmation modal in the GUI.
- The unsubscribe sweep performs real HTTP requests / sends real emails on
  your behalf. Use `--dry-run` (CLI) to plan first if you're unsure.
- Tokens and credentials live in `token.json` / `credentials.json`. Both are
  gitignored.

## License

MIT.
