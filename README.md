# mkfix

A FIX protocol testing engine for capital markets connectivity, built on
[mkio](https://github.com/markuskimius/mkio) and
[mkui](https://github.com/markuskimius/mkui).

## Features

- **Session Management** -- Configure and run FIX sessions as initiator or
  acceptor from a live Sessions blotter showing status, sequence numbers and
  the last error,
  created and edited through modal dialogs. Buttons follow the session's state:
  Start, Edit, Delete, Reset Seq, and Change Seq only while a session is
  down, Stop only while it runs. Reset Seq resets a session in one click:
  both sequence numbers return to 1 and a resend request from the
  counterparty never replays anything sent before the reset; Change Seq
  opens a dialog prefilled with the current sequence numbers for setting
  them to arbitrary values. Multiple sessions on the same port with
  different CompIDs.
  Outgoing timestamp granularity is configurable per session: protocol
  standard by default (seconds through FIX 4.1, milliseconds from 4.2), or
  forced to second, millisecond, microsecond, nanosecond, or picosecond. The
  session dialog also sets the logout timeout (0 = twice the heartbeat
  interval) and whether a TestRequest precedes the Logout on Stop.
- **Message Viewer** -- Live virtualized table of FIX messages in tag=value
  format, streaming live by default, with time-based paging, per-column filters
  (value checklists with exclude/include intent, plus numeric and time-range
  bounds with Today / Last hour / Last 15 min presets on timestamp columns),
  sorting, and clipboard copy. Heartbeats are hidden by default (every other
  message type shows); the header filter on the message type column restores
  them. The same filtering applies across every blotter, and the order and
  trade blotters open showing today's activity by default.
- **Message Detail** -- Field-by-field breakdown of the message selected in
  the Messages viewer, translated through the owning session's dictionary:
  collapsible header/body/trailer sections and repeating-group trees,
  drag-resizable columns, and UTC timestamps rendered in a selectable
  timezone (defaulting to the browser's).
- **FIX Dictionaries** -- Standard FIX 4.0 through 5.0SP2 dictionaries ship
  built in; create tweaked copies per test scenario -- either a delta that
  stays linked to its base version or a standalone document -- edit tag
  names, enum values, message types, repeating groups, and header/trailer
  layout in a dedicated editor pane, import/export them as JSON, and bind
  one per session: both what the session sends and how its messages are
  displayed follow the bound dictionary.
- **Sent Orders & Received Trades Blotters** -- Live order state machine driven by
  ExecutionReports; fill and partial fill records. Send NewOrderSingle from
  the blotter's New dialog; Replace (Cancel/Replace) and Cancel working orders
  directly from the blotter -- the Replace dialog opens on the order's full
  form, prefilled with the terms last entered (the New dialog's or the previous
  replace's) -- and a fully filled order can still be replaced up to revive it.
  The form offers Market / Limit / Market on Close / Limit on Close / Funari
  orders, Buy / Sell / Sell Short / Sell Short Exempt, and Day / GTC / At the
  Opening / IOC / FOK / GTX / GTD / At the Close, plus an Expire field
  drawn as the browser's own date and time pickers: pick a date alone and
  the order carries ExpireDate; add a time (entered in your local zone) and
  it goes out as ExpireTime in UTC at the session's timestamp precision.
  Values that only some FIX versions define (Market/Limit on Close through
  4.3, At the Close from 4.2) say so in the dropdown, but every value can be
  sent on every session -- an invalid combination is a test scenario.
  Every order and trade dialog names the FIX tag on each field, lists
  dropdown values as `code - name`, and ends with a live "Terms as tags"
  line showing the entered terms as `tag=value` pairs before they are sent.
  Anything else rides as an extra tag. An accepted cancel or replace moves the order to
  the request's ClOrdID (per the FIX chain), while an immutable Order ID keeps
  the order recognizable across the chain; a trade is likewise one blotter
  row under an immutable Trade ID, and a correction or bust rewrites it as a
  new version carrying the fresh ExecID and the ExecRefID it answered, with
  the fill and earlier corrections kept in its history. Every blotter row is written before its message
  leaves, so a counterparty that answers instantly -- a reject arriving while
  the order is still going out -- lands on the blotter as a reject and stays
  there, under the Order ID mkfix minted rather than the counterparty's.
- **Prefixed IDs** -- Every generated ID states its kind: `RT` ClOrdIDs
  (routed), `OR` Order IDs, `EX` ExecIDs, `TR` Trade IDs -- followed by a
  2-character instance code (the first two letters of the username, so
  concurrent mkfix users against the same counterparty mint distinguishable
  IDs; `-i CODE` sets it explicitly, e.g. for two instances run by one user,
  and the database remembers it until `-i` is given again -- `-i ''` goes back
  to the username) and an 8-digit counter that persists across restarts.
- **Received Orders & Sent Trades Blotters** -- The other side of the same flow:
  new orders and incoming cancel and cancel/replace requests all appear on the
  Received Orders blotter as a pending action, and a single Accept/Reject pair
  acts on whatever is pending (ExecutionReport New/Canceled/Replaced on accept;
  ExecutionReport Rejected or OrderCancelReject on reject); orders stay
  fillable while a request is pending and even after a full fill (overfills
  are a scenario worth testing), and sent trades can be corrected and busted
  from the Sent Trades blotter (ExecTransType Correct/Cancel through FIX 4.2,
  ExecType TradeCorrect/TradeCancel from 4.3 on, always with ExecRefID) --
  including trades filled before a replace renamed the order's ClOrdID chain.
  On the client side, a received trade can be DK'd from the Received Trades
  blotter (DontKnowTrade with the counterparty's OrderID/ExecID, a DKReason
  and optional Text); the trade row stays as received.
  Every message goes out version-correct: fills report ExecType F from FIX
  4.3, tags a version does not define are withheld, FIX 4.0 cancels carry
  CxlType, and FIXT.1.1 Logons carry DefaultApplVerID.
- **Color-Coded Blotters** -- Conditional cell and row styling throughout:
  buy/sell sides, order statuses, exec types, TX/RX direction, and pending
  requests are colored; heartbeat chatter is dimmed in the Messages viewer.
  Order and trade action buttons disable while the owning FIX session is
  down, the session's live status joined onto each row as it changes.
- **Templates, in every dialog** -- Every order and trade dialog (New,
  Replace, Cancel, Accept, Reject, Fill, DK, Correct, Bust) opens on a
  Template dropdown and closes on a "Save as template" name. Pick a template
  and its terms fill the form -- a blank term leaves the field to the row (a
  fill's leaves, the order's price) -- edit what differs, and send; type a
  name and the terms you sent are kept under it for next time, replacing a
  template of that name. Each dialog reopens on the template last picked
  or saved in it (the saved one when both happened), remembered per
  browser. Each dialog keeps its own kind of template (an
  order template holds symbol, side, quantity, type, price, TIF, extra tags
  and optionally the session; a fill template quantity and price; a DK
  template its reason and text; Cancel, Accept and Bust their extra tags),
  and the dialog's pin keeps it open for a run. The Templates pane under the
  Trading menu lists every kind for editing and deleting. Templates live in
  the database and are shared by everyone on the server, like layouts.
- **Extra Tags on Anything** -- Every send action (New, Replace, Cancel,
  Accept, Reject, Fill, Correct, Bust, DK) takes an optional Extra Tags field in
  pipe-delimited FIX format (`528=A|382=2|375=BRK1|375=BRK2`). Custom tags on
  received orders and cancel/replace requests are captured and prefill the
  Accept/Reject/Fill dialogs' Extra Tags field, so they can be confirmed or
  edited and echoed back on the answering ExecutionReport. Pairs are
  placed by wire position -- header tags in the header, trailer tags before
  the checksum -- duplicates go out in the order given (that's a repeating
  group, nested ones included), a tag the message already carries is
  overridden in place (even 34, 52, or 9/10 for deliberately corrupt
  messages), and an empty value (`21=`) deletes the tag for
  missing-required-field tests.
- **Message Replay** -- Load production FIX logs and replay them into a test
  session with speed control, message filtering, and pause/resume.
- **Saved Layouts** -- The Layout menu saves the window arrangement (frame
  positions, tabs, and each open table's filters, sort, and visible columns)
  on the server and restores the newest save at startup; earlier saves stay
  restorable from the Restore Layout submenu, and Reset to Default returns to
  the shipped arrangement. mkfix has no login, so the history is shared by
  everyone using the same server.
- **Record History** -- Sessions, orders and trades are versioned: every
  change to a row is recorded, and each blotter's History button opens a
  History pane showing the selected record's versions with a Diff and Blame
  of what changed between them -- a trade's versions being its fill,
  corrections and bust. An As of… button reads a blotter as it stood at a
  moment. Session config edits can be undone and redone (Undo Session Change
  / Redo Session Change in the Edit menu); the engine reloads the session to
  match. Orders and trades are read-only history, since the
  counterparty's view of them cannot be rewound.
- **Archiving** -- `mkfix archive` moves the running data (messages, orders,
  trades, IOIs, allocations) from before a cutoff -- midnight at the start of
  today by default -- into CSV files and deletes it, so a test bed starts
  fresh with its sessions intact and the old data on disk. Run it while the
  server is up and the blotters drop the rows live; the config tables
  (sessions with their state, dictionaries, settings, ID counters, replay
  jobs, layouts) go only when named, and a running session refuses. `mkfix
  restore` puts an archive back exactly as it was, history included.
- **IOI & Allocation Viewers** -- Indications of Interest and Allocation message
  tracking.
- **Session Protocol** -- Logon, Logout, Heartbeat, TestRequest, SequenceReset,
  GapFill, PossDupFlag handling, and heartbeat timeout detection. A
  ResendRequest is answered from the recorded messages: application messages
  go out again as PossDups under their original sequence numbers, admin runs
  collapse into GapFills. Stopping a session logs out the way the spec asks:
  a TestRequest to confirm the counterparty is caught up (optional per
  session), then Logout, then a wait for the confirming Logout -- answering
  any ResendRequest in the meantime -- bounded by a per-session logout
  timeout that defaults to twice the heartbeat interval.
- **Exact Message Storage** -- Every message is stored as its wire bytes (SOH
  delimiters included), so a value containing a literal `|` displays and
  retransmits correctly; the pipe form is only a rendering.

## Installation

```bash
pip install mkfix
```

Runs on Linux, macOS and Windows with the standard CPython 3.11+
interpreter. On Windows, Ctrl+C stops the server the same way as
elsewhere: every session is logged out, then the process exits. The
server runs on mkio's selector loop there, as mkio 1.1.1 does, so a
browser's dropped connections print no tracebacks and Ctrl+C is not
kept waiting; mkio's `event_loop`
config key overrides it. The
`-i` default there comes from the account name's first two ASCII letters
or digits, so a name starting with a space or a non-ASCII letter still
yields a valid code.

Or from source:

```bash
git clone https://github.com/markuskimius/mkfix.git
cd mkfix
pip install -e .
```

Upgrading from 0.33 or earlier: the first start drops the session-status
columns the old engine mirrored onto `fix_sessions`, `fix_orders` and
`fix_executions` (the blotters now read the state table through a join),
and `mkfix restore` accepts archives that still carry them.

## Usage

```bash
mkfix                        # start with defaults (port 8080, mkfix.db)
mkfix -p 9090                # override port
mkfix -d mytest              # use mytest.db
mkfix -d :memory:            # in-memory database
mkfix -i Q7                  # stamp Q7 into generated IDs (remembered by later runs on this database)
mkfix -i ''                  # forget the saved code, back to the username default
mkfix myconfig.toml          # custom config file
```

### Archiving old data

```bash
mkfix archive --dry-run                 # what would go: the data tables, from before today
mkfix archive                           # archive it (asks first; -y skips the question)
mkfix archive --cutoff 2026-09-01       # everything from before that date (local time)
mkfix archive --cutoff 7d --tables orders,trades
mkfix archive --all --cutoff 0m         # every table, config included, from before now
mkfix restore archive/mkfix_20260912-020000
```

`mkfix archive` writes one directory per run under `./archive` (`--out` to
change it): a `manifest.json`, a CSV per table with every column, the version
history of the orders, trades and sessions archived, and the session state
rows alongside their sessions. The running-data tables are the default;
`--tables` takes the short names `messages`, `orders`, `trades`, `iois`,
`allocations`, `sessions`, `dictionaries`, `settings`, `ids`, `replay_jobs`,
`layouts`, and `--group config` or `--all` reaches the config tables, which
are archived whole rather than by cutoff. Give the same `-d`, `-p` and
`--host` as the server: when a server answers on that port the archive runs
through it, the engine refuses to archive a running session, a dictionary a
remaining session uses, or the ID counters, and the blotters drop the rows
as they go; otherwise the database file is archived directly, which needs
the server stopped. `mkfix restore` is offline only and refuses while a
server answers; a row that already exists blocks the restore of a data
table, while a config table's row is replaced.

On startup mkfix prints where to find it, along with the config and database in
use and the enabled FIX sessions:

```
mkfix <version>
  Web UI:    http://localhost:8080/
  Listening: 0.0.0.0:8080 (all interfaces)
  Config:    /path/to/mkfix.toml
  Database:  /path/to/mkfix.db
  IDs:       RT/OR/EX/TR + MA + 8-digit counter (from username)
  Sessions:  1 enabled
    acc: MKFIX -> BROKER (FIX.4.2, acceptor on port 9876)
  Press Ctrl+C to stop.
```

Open the Web UI URL in your browser. If the port is already taken, mkfix exits
with an error instead of starting.

## Quick Start

1. **Create two sessions** with the New button on the Sessions blotter -- one
   initiator pointing at the other as acceptor on the same port.
2. **Start both sessions** -- Logon and Heartbeat messages will stream in the
   Messages pane.
3. **Send an order** with the New button on the Sent Orders blotter -- see
   it in the Messages pane and the blotter itself; click any message row to
   break it out field by field in the Detail pane. On the acceptor side it
   appears in Received Orders, where it can be accepted, rejected, or filled;
   fills land in Sent Trades, where they can be corrected or busted, and on
   the client side in Received Trades, where they can be DK'd.
4. **Replay a log** from Tools > Replay Control -- load a production FIX log and
   replay it into a test session.

## Configuration

mkfix uses a TOML config file (`mkfix.toml`). The built-in default is used when
no config is specified. Key settings:

```toml
port = 8080
host = "0.0.0.0"
db_path = "mkfix.db"
```

Tables, services, and static routes are also configured in the TOML file. See
the built-in `mkfix.toml` for the full schema.

## Dependencies

- [mkio](https://github.com/markuskimius/mkio) >= 1.1.1, < 2 -- async microservice
  framework (aiohttp + aiosqlite)
- [mkui](https://github.com/markuskimius/mkui) >= 1.0.0, < 2 -- Web Components UI
  framework

## License

GPL-2.0
