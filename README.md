# mkfix

A FIX protocol testing engine for capital markets connectivity, built on
[mkio](https://github.com/markuskimius/mkio) and
[mkui](https://github.com/markuskimius/mkui).

## Features

- **Session Management** -- Configure and run FIX sessions as initiator or
  acceptor from a live Sessions blotter showing status, sequence numbers and
  the last error,
  created and edited through dialogs. Buttons follow the session's state:
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
  built in; create tweaked copies per test macro -- either a delta that
  stays linked to its base version or a standalone document -- edit tag
  names, enum values, message types, repeating groups, and header/trailer
  layout in a dedicated editor pane, import/export them as JSON, and bind
  one per session: both what the session sends and how its messages are
  displayed follow the bound dictionary.
- **Sent Orders & Received Trades Blotters** -- Live order state machine driven by
  ExecutionReports; fill and partial fill records. Send NewOrderSingle from
  the blotter's New dialog, or Clone an order in any state -- the New form
  prefilled from the selected order, session included, ready to edit and
  send (the Clone on Received Orders sends a counterparty's order back out
  as your own, retargeted to any session); Replace (Cancel/Replace) and
  Cancel working orders directly from the blotter -- the Replace dialog
  opens on the order's full form, prefilled with the terms last accepted
  (the New dialog's or the last accepted replace's -- a rejected replace
  leaves them alone) -- and a fully filled order can still be replaced up
  to revive it.
  A Replace or Cancel stays on the row as Pending -- with the request's
  ClOrdID and terms under Pending ID, New Qty and New Px -- until the
  counterparty answers it: an accepting ExecutionReport moves the order to the request's
  ClOrdID, while an OrderCancelReject leaves the ClOrdID alone, puts the
  order back to the status the reject reports, and notes what was refused
  and why under Rej Reason (`Replace RTMA00000042: TooLateToCancel`). A
  PendingCancel/PendingReplace report only changes the status. The row
  tracks the latest request; fills keep arriving while one is pending.
  The form offers Market / Limit / Market on Close / Limit on Close / Funari
  orders, Buy / Sell / Sell Short / Sell Short Exempt, and Day / GTC / At the
  Opening / IOC / FOK / GTX / GTD / At the Close, plus an Expire field
  drawn as the browser's own date and time pickers: pick a date alone and
  the order carries ExpireDate; add a time (entered in your local zone) and
  it goes out as ExpireTime in UTC at the session's timestamp precision.
  Expire and "Save as template" sit in an Advanced section just above the
  tag preview, folded until clicked open.
  Values that only some FIX versions define (Market/Limit on Close through
  4.3, At the Close from 4.2) say so in the dropdown, but every value can be
  sent on every session -- an invalid combination is a test macro.
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
  are a macro worth testing). Unsol Cxl cancels a working order nobody
  asked to cancel -- ExecutionReport Canceled under the order's own ClOrdID,
  without OrigClOrdID -- and leaves a pending cancel or replace request
  parked, so it can still be rejected as too late. Restate changes a working
  order's terms unasked -- ExecutionReport Restated (150=D) under the order's
  own ClOrdID with the new OrderQty and Price (a blank price is withheld), an
  ExecRestatementReason (378) picked from a list and the order's working
  status; both sides' rows take the new terms, and a session whose
  dictionary lacks 150=D (FIX 4.0/4.1) refuses it. Sent trades can be
  corrected and busted from the Sent Trades blotter (ExecTransType
  Correct/Cancel through FIX 4.2, ExecType TradeCorrect/TradeCancel from 4.3
  on, always with ExecRefID) -- including trades filled before a replace
  renamed the order's ClOrdID chain.
  On the client side, a received trade can be DK'd from the Received Trades
  blotter (DontKnowTrade with the counterparty's OrderID/ExecID, a DKReason
  and optional Text); the trade's terms stay as received, and the blotter's
  DK and DK Text columns record the dispute as sent until the counterparty's
  correction or bust answers it (a re-notified fill arrives under a new
  ExecID without ExecRefID, so it is a new trade and the disputed one keeps
  its mark). Both trade blotters show each trade's Order ID. On the market side an
  inbound DK marks the sent trade it names -- the reason and text show in the
  Sent Trades blotter's DK column, and the trade can still be corrected or
  busted, which clears the mark. Re-notify answers the DK: the trade's
  current report (its fill, correction or bust) goes out again under a new
  ExecID, with the trade's terms, the order's state as it stands now, and
  the same ExecRefID a correction or bust carried; the row moves to the new
  ExecID with the mark cleared, so a DK of the re-notification shows again.
  Extra tags can recast it (`20=0|19=` restates a DK'd correction as a plain
  fill).
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
  Template dropdown and closes on a "Save as template" name (under New and
  Replace's folded Advanced section). Pick a template
  and its terms fill the form -- a blank term leaves the field to the row (a
  fill's leaves, the order's price) -- edit what differs, and send; type a
  name and the terms you sent are kept under it for next time, replacing a
  template of that name. Each dialog reopens on the template last picked
  or saved in it (the saved one when both happened), remembered per
  browser. Each dialog keeps its own kind of template (an
  order template holds symbol, side, quantity, type, price, TIF, client,
  extra tags and optionally the session; a fill template quantity and price; a DK
  template its reason and text; Cancel, Accept and Bust their extra tags),
  and the dialog's pin keeps it open for a run: after each send the form
  keeps the terms as entered (only the Save-as name clears, so a template
  is saved once), ready for the next order to vary one of them. The
  Templates pane under the Config menu lists every kind for editing,
  cloning (Edit's form under a new name, `<name> copy` proposed) and
  deleting; an order's Clone button with "Save as template" makes a
  template of an order already sent. Templates live in
  the database and are shared by everyone on the server, like layouts.
  No dialog blocks the application: it floats over a workspace that stays
  live, so you can scroll a blotter, open Details, or raise a second dialog
  while one is open. A dialog acts on the rows it was opened on -- its title
  names them -- whatever is selected afterwards.
- **Client Column** -- Orders, trades and messages carry the client they
  name, but the tag that carries it differs by counterparty: ClientID (109)
  through FIX 4.2, a PartyID with PartyRole 3 from 4.3, OnBehalfOfCompID
  (115) stamped by a hub, Account (1), or something custom. Each session
  therefore names where its client rides -- a comma-separated list such as
  `109,115` or `5001`, a group member qualified by a sibling as in
  `448[452=3]`, first present wins, blank meaning the default chain
  `448[452=3],109,115,1`. The New and Replace dialogs take a Client the
  engine stamps on the session's tag (an Extra Tag naming it wins), a
  received order's client rides back on every answer, trades inherit their
  order's client, and every blotter and the Messages pane show and filter
  the column. Rows recorded before the column are seeded once at startup.
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
  missing-required-field tests. Trades keep their tags too: a sent trade's
  Extra Tags as typed, a received trade's custom tags from its
  ExecutionReport, shown in both trade blotters and prefilled into the
  Correct, Bust and Re-notify dialogs.
- **Handling Instructions & Text** -- The New and Replace dialogs carry
  HandlInst (21; default 1), and every order and trade dialog a Text (58)
  field; an Extra Tag naming 21 or 58 still wins. The order blotters show
  Handl Inst, Extra Tags, and the text in both directions: Sent Text is the
  58 of the last message this side sent on the order (a New, Replace or
  Cancel on Sent Orders; an Accept, Reject, Fill, Unsol Cxl or Restate on
  Received Orders) and Rcvd Text the counterparty's last -- their ExecutionReports, or
  their New and each cancel or replace request. The trade blotters show each
  report's Text. Older orders and trades are seeded once at startup from
  their recorded messages (HandlInst and trade tags; not Sent Text).
- **Macros** -- Scripts, called macros, that act on orders as things happen
  to them, in a small language of their own. There are two kinds, each with its own menu,
  editor and run panes. A **market macro** (Market menu) answers the orders
  you receive (`on order`): **Arm…** it and it waits for orders to match. A
  **client macro** (Client menu) sends orders of its own and manages them
  (`run`), or minds the orders you send by hand (`on sent order`): **Run…** it
  on the session you choose, as many runs at once as you like, of one macro
  or of many. A macro is one kind or the other -- the editor underlines a
  block of the wrong side. Every order gets its own copy of the macro, so
  the same few lines handle one order or a thousand.

  ```
  macro slow-fill

  on order where symbol in ['IBM', 'MSFT']
      after 200ms
      accept
      when cancel
          accept
          stop
      while order.leaves_qty > 0
          after 1s ± 250ms
          fill qty: MIN(100, order.leaves_qty), price: order.price
  ```

  The actions are the blotter buttons -- `accept`, `reject`, `fill`,
  `unsol cxl`, `restate`, `correct`, `bust`, `renotify` on the market side;
  `new`, `replace`, `cancel`, `dk` on the client side -- with the dialogs'
  fields as terms. `repeat 20 at 5/s` around a `new` sends twenty orders,
  each its own macro; `when` reacts to events beside the main flow
  (cancel and replace requests and disputes on one side; acknowledgements,
  fills, `cancel rejected`, corrections and busts on the other); `wait` and `expect … within` wait for events,
  `after` for time (with seeded jitter, so a run repeats exactly); conditions
  are mkio expressions over the order's row, its trades, the recorded
  versions of its row (`history`) and the event in hand. **Client
  Macros** and **Market Macros** are editors that check as you type -- a misspelt
  column or an action on the wrong side of an order is underlined before
  anything runs -- completes words in context (Ctrl+Space), explains a word
  under the mouse or the cursor (hover, F1), folds blocks, and has an optional vim mode. Each
  side's **Macro Runs**, **Macro Orders** and **Macro Log** panes show its runs and each
  order's macro, the line it is on and what it is waiting for, with Pause,
  Stop and Detach, and the order blotters name the macro that took an
  order. Received orders are offered to the armed runs in their **Priority**
  -- first armed, first offered -- which **Move Up**/**Move Down** change; the
  same market macro can be armed once per session. `run` may name its
  session (`run on SESSION`) or leave it to Run…, so one client macro runs
  on several sessions at once; **Stop all** in the editor stops every live
  run of a macro, and editing one leaves its live runs on the version they
  started with. Seventeen
  bundled examples -- an auto-acknowledge, a cancel/replace desk, a dispute
  desk, a deliberately misbehaving counterparty; a single order's lifecycle,
  a replace chase, a seeded burst of twenty orders, a DK policy, a regression
  suite with verdicts, a minder for hand-sent orders; and a loopback venue and
  client that together play both sides -- open as copies from each editor's
  **From example…**, and the language reference is under **Help**. The client
  examples run over two loopback sessions, this server talking to itself:
  **Help › Macro Language › Macro Examples › Set up loopback sessions**
  creates and starts them, and **Run the loopback tour** also arms the venue
  and runs the client, so the tour is one click on a fresh install. Sent Orders and Received Orders carry their side's macro
  controls as four symbols -- **▶** play (macros ticked from a list, and paused runs to
  resume), **⏸** pause and **■** stop (runs ticked from a list: one, several
  or all), and **●** record, red while it records -- and the status bar says what the macros are doing:
  recording, playing, paused, or how the last run ended. **Record…**, there or in either editor, writes the
  first draft for you: work orders by hand -- accept, fill, answer a cancel
  on one side; send, replace, cancel, DK on the other -- and Stop recording
  opens the macro that would have done the same, ready to run and to
  loosen. It is triggered by events, not the clock: each action waits for
  what you heard before you took it, a request you always answered the same
  way becomes a `when` handler, and time is written only where nothing came
  between two of your actions (or everywhere, with **Keep my delays**). Macros are versioned
  like sessions, so every Save is kept: **History** in an editor lists the
  versions with the runs that used each, shows one against the macro as
  saved now, and **Restore** brings it back as an unsaved edit. A macro can do more than the blotter
  offers -- the buttons
  hide Fill on a rejected order, the engine does not refuse it -- which is
  the point of a test venue. Macros live in the server's memory: after a
  restart every run that was live is marked `interrupted` and nothing is
  played again -- a restart stops every macro, and never sends anything.
- **Message Replay** -- Load a FIX log -- a whole day's, both sides -- and
  replay it into a test session *as that session*: Load reads the file once
  and shows the CompID pairs, message types and time span it holds; Configure
  picks the session, the types (a checklist with counts), a time-of-day window,
  the speed and the longest gap to wait; Start asks which direction to play --
  the client's messages into an initiator, or the venue's into an acceptor.
  On the wire the session's BeginString, CompIDs, sequence numbers,
  SendingTime and TransactTime replace the log's, PossDup and its
  companions are dropped, and every other tag -- routing tags, repeating
  groups -- goes out as logged; admin messages are never sent. Five bundled
  example logs are offered in the Load dialog; Help > Replaying a Log has
  the details.
- **Saved Layouts** -- The Layout menu saves the window arrangement (frame
  positions, tabs, and each open table's filters, sort, and visible columns)
  on the server and restores the newest save at startup; earlier saves stay
  restorable from the Restore Layout submenu, and Reset to Default returns to
  the shipped arrangement. mkfix has no login, so the history is shared by
  everyone using the same server.
- **Help** -- Help → Keyboard Shortcuts lists the keys the tables and dialogs
  answer; Help → About mkfix shows the client's version beside the server's
  and the mkui and mkio versions, the GPL-2.0 no-warranty notice, and links to
  the repository, the issue tracker, the license and the FIX dictionary
  notice, with a Copy details button for bug reports.
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

**Upgrading from 0.48-0.50.** What those releases called *scenarios* are
*macros* from 0.51, and they start afresh: on its first start 0.51 drops the
saved scenarios, their runs and logs from an existing database (it says so),
and `mkfix restore` leaves them out of an older archive. Orders, trades and
messages are untouched. To keep a scenario, **Export** it from the editor
before upgrading, change its first line from `scenario NAME` to `macro NAME`,
and **Import** it into Client Macros or Market Macros. Saved window layouts
that name the old scenario panes lose those panes.

Runs on Linux, macOS and Windows with the standard CPython 3.11+
interpreter. On Windows, Ctrl+C stops the server the same way as
elsewhere: every session is logged out, then the process exits. The
server runs on mkio's selector loop there, so a browser's dropped
connections print no tracebacks and Ctrl+C reaches the loop at once;
mkio's `event_loop` config key overrides it. mkio 1.2.1 also closes its
read connection before the final WAL checkpoint, so the session-state
writes a stopping engine makes can no longer leave the exit waiting
out SQLite's 5 s busy timeout, which showed on Windows as a Ctrl+C
that took several seconds. The
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

`mkfix -h` lists the options with their defaults and names the two
subcommands below; `mkfix archive -h` and `mkfix restore -h` carry their own
options and examples.

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
`allocations`, `macro_runs`, `macro_orders`, `macro_log`, `sessions`, `dictionaries`,
`settings`, `ids`, `replay_jobs`, `templates`, `macros`, `layouts`, and `--group config` or `--all` reaches the config tables, which
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
3. **Send an order** with the New button on the Sent Orders blotter (Client
   menu; the receiving side's blotters are under Market) -- see
   it in the Messages pane and the blotter itself; click any message row to
   break it out field by field in the Detail pane. On the acceptor side it
   appears in Received Orders, where it can be accepted, rejected, filled,
   restated, or canceled unsolicited;
   fills land in Sent Trades, where they can be corrected or busted, and on
   the client side in Received Trades, where they can be DK'd.
4. **Replay a log** from To Do > Replay Control -- Load the bundled
   `two-sided-day` example (or a log of your own), Configure it onto a
   session, and Start it in either direction; the other end's blotters fill
   as if the day were happening again.

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

Two mkio keys govern the browser connections (mkio >= 1.3.0). Each page has
its own send queue, so one that stops reading -- a laptop asleep, a tab frozen
in the background -- no longer holds up the blotters of every other page, as
it did through mkfix 0.41.0, where its eventual disconnect could also end a
blotter's live updates until the server was restarted:

```toml
ws_heartbeat_s = 30      # ping interval; a page that stops answering is dropped (0 = off)
ws_send_buffer_mb = 16   # a page further behind than this is closed, and reconnects for a fresh snapshot
```

A blotter whose subscription the server ended shows `not updating -- retry` in
its toolbar instead of sitting still with old rows.

## Dependencies

- [mkio](https://github.com/markuskimius/mkio) >= 1.5.0, < 2 -- async microservice
  framework (aiohttp + aiosqlite); 1.5.0 brings expression language 2
  (`and`/`or`/`not`/`in`, durations, `COUNT`), which the client handshake pins
- [mkui](https://github.com/markuskimius/mkui) >= 1.11.0, < 2 -- Web Components UI
  framework; 1.10.0 evaluates the same language 2 in the browser, and 1.11.0
  adds the dialog's checklist field (the Pause and Stop run lists)

## Third-party code

The Macros editor is [Ace](https://ace.c9.io) (ace-builds 1.44.0, BSD-3-Clause),
vendored prebuilt under `mkfix/static/vendor/ace` with its LICENSE; nothing is
built or downloaded at run time. The standard FIX dictionaries are generated
from the QuickFIX specs (see `mkfix/fix/dictionary_data/NOTICE`).

## License

GPL-2.0
