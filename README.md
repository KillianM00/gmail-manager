# gmail-manager

A local-first tool for taking control of a Gmail account: bulk-delete by query
or sender, run a real RFC 8058 unsubscribe sweep across every list you're on,
archive / mark-read / restore in bulk, and clear out trash without clicking
through Google's UI a hundred times.

Comes with both a CLI and a small web GUI that runs on `localhost`. Your data
never leaves your machine — it talks straight to the Gmail API with an OAuth
token cached on your filesystem.

---

## Get started in 60 seconds

```bash
git clone https://github.com/KillianM00/gmail-manager.git
cd gmail-manager
pip install -e .          # or: uv sync
gmail-mgr setup           # ← do this once
gmail-mgr serve           # ← open the GUI at http://localhost:8000
```

`gmail-mgr setup` is an **interactive wizard** that handles the entire
first-run experience for you. It will:

1. **Detect every browser installed on your machine** (Chrome, Edge, Firefox,
   Brave, Arc, Opera, Safari) and ask which one you want gmail-mgr to use.
   Every future browser pop — OAuth consent, GUI launch — uses your pick.
2. **Walk you through creating a Google Cloud OAuth client** (~5 minutes,
   one-time). It opens the Cloud Console for you, lists exact menu paths,
   and waits while you drop the downloaded `credentials.json` into the right
   place.
3. **Run the OAuth consent flow** in your chosen browser and cache the token.

Everything lives under `~/.gmail-mgr/` — the credentials, token, browser
preference, and subscription database are scoped per-user, not per-clone, so
you can run gmail-mgr from any directory after the first setup.

> Already have `credentials.json` from a previous install? Drop it at
> `~/.gmail-mgr/credentials.json` (or leave it in the project root — both work)
> and `gmail-mgr setup` will skip the Cloud Console step.

To change the browser later: `gmail-mgr config browser`.

To redo OAuth later: delete `~/.gmail-mgr/token.json` and run any command.

---

## What it does

- **Group senders** — scan any Gmail query (Inbox, last year, a label, anything)
  and see who is actually filling your mailbox. Rank by **message count** or
  **storage size**, group by **address** or **domain**.
- **Mass delete** — trash everything from a sender, a list of senders, or any
  Gmail query. Recoverable from Trash for 30 days.
- **Auto-block** — when you trash a sender, optionally create a Gmail filter
  that auto-trashes every future message from them. One checkbox.
- **Archive / mark read / restore** — same selection model, different action.
  Archive removes INBOX label. Restore pulls things back out of Trash.
- **Empty Trash** — permanently delete everything in Trash in one click.
  Bypasses the 30-day window.
- **Real unsubscribe sweep** — for each sender, in order:
  1. RFC 8058 one-click POST (`List-Unsubscribe-Post: List-Unsubscribe=One-Click`)
  2. `List-Unsubscribe` HTTP GET fallback
  3. `List-Unsubscribe` `mailto:` (sends an unsubscribe email on your behalf)
  4. *(opt-in)* Body-link scraping — disabled by default; see
     [Security](#security).

  Successfully-unsubscribed senders get tagged with the
  `gmail-mgr/unsubscribed` label so you can review or trash them later.
- **Subscription registry** — every sender you've ever scanned is recorded in a
  local SQLite db (`~/.gmail-mgr/subs.db`) along with the last action you took
  (active / unsubscribed / trashed / blocked / archived). View it with
  `gmail-mgr subs` or the **Subscriptions** sidebar tab.
- **Sweeps** — replay any action across every sender currently at a given
  status. Example: monthly "trash everything still arriving from senders I
  unsubscribed from" — one command, schedulable from cron / Task Scheduler.
- **GUI** — sidebar nav, per-row and bulk actions, custom query input, instant
  sender-name filter.

## Install

Requires Python 3.10+.

```bash
git clone https://github.com/KillianM00/gmail-manager.git
cd gmail-manager

# With uv (recommended)
uv sync

# Or with pip + venv
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -e .
```

Then run `gmail-mgr setup` — see [Get started in 60 seconds](#get-started-in-60-seconds).

## Manual Google OAuth setup (if you skip the wizard)

The wizard does all of this for you. If you prefer to do it by hand:

1. <https://console.cloud.google.com/> → create or pick a project.
2. **APIs & Services → Library** → enable **Gmail API**.
3. **APIs & Services → OAuth consent screen** → User type **External**, fill in
   the required fields, add yourself under **Test users**.
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID** →
   Application type **Desktop app** → download the JSON, save it to
   `~/.gmail-mgr/credentials.json`.
5. Run `gmail-mgr auth`.

### What permissions does it ask for?

A single OAuth scope: `https://mail.google.com/` (full mailbox access). That's
required because permanently deleting messages (Empty Trash) is gated behind
the full scope; the narrower `gmail.modify` scope cannot do it.

## CLI

All commands accept `--help`.

```bash
gmail-mgr setup                              # interactive setup wizard
gmail-mgr config browser                     # change which browser to use
gmail-mgr auth                               # one-time OAuth (also done by setup)
gmail-mgr whoami                             # who am I and how much mail do I have

# Senders
gmail-mgr senders --query "in:inbox" --top 50
gmail-mgr senders --query "" --top 100       # whole mailbox; slower

# Preview / delete
gmail-mgr list --query "from:promo@example.com" --limit 20
gmail-mgr delete --query "older_than:1y category:promotions"
gmail-mgr delete-from --sender noreply@a.com --sender promos@b.com
gmail-mgr delete-from --from-file senders.txt

# Other actions, all by sender
gmail-mgr archive   --sender newsletter@a.com
gmail-mgr mark-read --sender notify@b.com
gmail-mgr restore   --sender oops@c.com
gmail-mgr block     --sender spam@d.com      # creates Gmail filter, future-mail only

# Unsubscribe sweep
gmail-mgr unsubscribe --query "in:inbox"
gmail-mgr unsubscribe --query "in:inbox" --dry-run
gmail-mgr unsubscribe --query "in:inbox -label:gmail-mgr/unsubscribed"
# Default --methods is `header-post,header-get,mailto`. Body-link scraping is
# off by default. Add it explicitly with --methods (see Security):
gmail-mgr unsubscribe --query "in:inbox" --methods header-post,header-get,mailto,body-link

# Subscription registry
gmail-mgr subs                                # list everything we've seen
gmail-mgr subs --status unsubscribed
gmail-mgr subs --domain example.com

# Sweeps — re-apply an action to every sender at a status
gmail-mgr sweep --status unsubscribed --action trash
gmail-mgr sweep --status active        --action archive
gmail-mgr sweep --status unsubscribed --action block --yes
```

### Scheduling sweeps

The sweep command is the right thing to put in cron / Windows Task Scheduler.
Add `--yes` to skip confirmation in headless contexts.

Linux / macOS cron, weekly:

```cron
0 7 * * 1 /home/me/.local/bin/gmail-mgr sweep --status unsubscribed --action trash --yes
```

Windows Task Scheduler — point it at `gmail-mgr.exe` (in your venv's
`Scripts\`) with arguments `sweep --status unsubscribed --action trash --yes`.

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
gmail-mgr serve              # opens http://localhost:8000 in your preferred browser
gmail-mgr serve --port 8766
gmail-mgr serve --no-browser
```

Sidebar has built-in views (Inbox / Unread / Promotions / Social / Updates /
Older 1y / All mail / Unsubscribed / Spam / Trash / Subscriptions) and an
"Empty trash" button. The toolbar lets you switch grouping (sender / domain),
metric (count / size), filter senders, and run a custom Gmail query.

Workflow:

1. Pick a view from the sidebar.
2. Switch grouping/metric in the toolbar — domain mode folds
   `noreply@x.com` + `marketing@x.com` + ... into one row.
3. Per-row **Unsub** / **Trash** buttons act on a single sender. The Trash
   modal has an **Also block future mail** checkbox that creates a Gmail
   filter to auto-trash future messages from that sender.
4. Tick checkboxes for bulk select. The floating action bar offers
   **Unsubscribe**, **Move to trash**, **Archive**, **Mark read**, **Block**,
   and **Restore** (when viewing Trash).

The senders endpoint is cached for 60 seconds and invalidated automatically
after any delete / unsubscribe / archive / restore / empty-trash action.

## How the unsubscribe sweep works

For each unique sender in the scan:

1. **Aggregate candidates across every message from that sender.** A
   transactional message might have no `List-Unsubscribe` header, but a
   marketing email from the same address will — so the sweep unions all
   candidates before trying anything.
2. Try methods in order: RFC 8058 one-click POST → header GET → mailto →
   *(opt-in)* body-link.
3. On success, label every message from that sender with
   `gmail-mgr/unsubscribed`, and update the subscription registry.

Senders that genuinely cannot be unsubscribed via API tend to be transactional
or security senders (PayPal alerts, GitHub security, Spotify login alerts).
Those don't *have* a real unsubscribe — they're notifications you opted into by
having an account. For those, just trash + block, or filter them out
client-side.

## Security

A few things worth knowing before you run this on a real mailbox:

- **Body-link unsubscribe is off by default.** Anchors in HTML email can point
  anywhere — including local network addresses — and following them blindly is
  effectively a CSRF/SSRF risk. The sweep ships with body-link scraping
  disabled. To enable it, pass `--methods header-post,header-get,mailto,body-link`
  on the CLI, or set `allow_body_links: true` on the API request. Even when
  enabled, the HTTP client refuses to fetch URLs that resolve to loopback /
  RFC 1918 / link-local / cloud-metadata IPs (see `unsubscribe.is_safe_unsub_url`).
- **The local web GUI binds to `127.0.0.1`** and trusts every request that
  hits it. Don't expose the port on a network you don't control.
- **Your OAuth client and token are local files** in `~/.gmail-mgr/`. Treat
  `credentials.json` like a password — anyone with both the client file and
  your account can re-do consent and read your mail.
- **Trash is recoverable for 30 days.** Permanent deletion (Empty Trash) is
  not. There's a confirmation modal, but no second prompt.
- **Block filters are real Gmail filters.** They live in your account and
  apply to *all* future mail, including from the Gmail web UI. Remove them
  under Gmail Settings → Filters and Blocked Addresses if you change your
  mind.

## Troubleshooting

**"Access blocked: app has not completed Google verification"**
You signed in with an account that isn't on the test-users list. Add it under
**OAuth consent screen → Test users** in the Cloud Console.

**`HttpError 403 ... insufficient authentication scopes`**
Your cached `token.json` was issued with a narrower scope than the app
currently requests. Delete `~/.gmail-mgr/token.json` and run any command —
you'll be sent through the OAuth flow again.

**Empty Trash returns 500**
Same root cause — full `https://mail.google.com/` scope is required to
permanently delete. Delete `~/.gmail-mgr/token.json` and re-auth.

**The sender scan is slow on first hit**
Gmail metadata batches return ~20 messages per second worst-case under quota
pressure. ~1000 messages takes 30–60 s. Subsequent loads of the same view come
from cache (`cached: true` in the meta line) and are near-instant. Refresh
forces a re-fetch.

**Some unsubscribes report "no candidates"**
That sender's emails contain no `List-Unsubscribe` header and (since
body-link is off by default) no header-based candidates. Either accept it,
or opt into body-link with `--methods …,body-link`.

**The OAuth tab opens in the wrong browser**
Run `gmail-mgr config browser` to change your preference. Setting it to
"system default" reverts to whatever the OS picks.

## Project layout

```
gmail_mgr/
├── auth.py          # OAuth flow + token caching
├── config.py        # ~/.gmail-mgr config + browser detection
├── messages.py      # list / fetch / batch trash / archive / restore / block / permanent-delete
├── unsubscribe.py   # List-Unsubscribe parsing + body-link scrape + SSRF guard
├── subs.py          # SQLite subscription registry
├── cli.py           # click commands: setup, senders, delete, unsubscribe, sweep, …
├── web.py           # FastAPI app: /api/profile, /api/counts, /api/senders, …
└── static/
    └── index.html   # single-page GUI
```

## License

MIT.
