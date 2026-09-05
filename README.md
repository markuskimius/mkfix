# mkfix

A FIX protocol testing engine for capital markets connectivity, built on
[mkio](https://github.com/markuskimius/mkio) and
[mkui](https://github.com/markuskimius/mkui).

## Features

- **Session Management** -- Configure and run FIX sessions as initiator or
  acceptor from a live Sessions blotter showing status and sequence numbers,
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
  message type shows); the Messages menu carries one-click views -- Hide
  Heartbeats, Hide Session Admin, Last 15 Minutes, and Show All, which also
  restores hidden heartbeats. The same filtering applies across every blotter,
  and the order and trade blotters open showing today's activity by default.
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
  replace's) -- and a fully filled order can still be replaced up to revive it. An accepted cancel or replace moves the order to
  the request's ClOrdID (per the FIX chain), while an immutable Order ID keeps
  the order recognizable across the chain; corrections and busts likewise
  replace the ExecID while an immutable Trade ID groups a fill with its
  corrections and busts.
- **Prefixed IDs** -- Every generated ID states its kind: `RT` ClOrdIDs
  (routed), `OR` Order IDs, `EX` ExecIDs, `TR` Trade IDs -- followed by a
  2-character instance code (the first two letters of the username, so
  concurrent mkfix users against the same counterparty mint distinguishable
  IDs) and an 8-digit counter that persists across restarts.
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
  Every message goes out version-correct: fills report ExecType F from FIX
  4.3, tags a version does not define are withheld, FIX 4.0 cancels carry
  CxlType, and FIXT.1.1 Logons carry DefaultApplVerID.
- **Color-Coded Blotters** -- Conditional cell and row styling throughout:
  buy/sell sides, order statuses, exec types, TX/RX direction, and pending
  requests are colored; heartbeat chatter is dimmed in the Messages viewer.
  Order and trade action buttons disable while the owning FIX session is
  down, driven by the session's live status mirrored onto each row.
- **Extra Tags on Anything** -- Every send action (New, Replace, Cancel,
  Accept, Reject, Fill, Correct, Bust) takes an optional Extra Tags field in
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

Or from source:

```bash
git clone https://github.com/markuskimius/mkfix.git
cd mkfix
pip install -e .
```

## Usage

```bash
mkfix                        # start with defaults (port 8080, mkfix.db)
mkfix -p 9090                # override port
mkfix -d mytest              # use mytest.db
mkfix -d :memory:            # in-memory database
mkfix myconfig.toml          # custom config file
```

On startup mkfix prints where to find it, along with the config and database in
use and the enabled FIX sessions:

```
mkfix <version>
  Web UI:    http://localhost:8080/
  Listening: 0.0.0.0:8080 (all interfaces)
  Config:    /path/to/mkfix.toml
  Database:  /path/to/mkfix.db
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
   fills land in Sent Trades, where they can be corrected or busted.
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

- [mkio](https://github.com/markuskimius/mkio) >= 0.2.0 -- async microservice
  framework (aiohttp + aiosqlite)
- [mkui](https://github.com/markuskimius/mkui) >= 0.2.8 -- Web Components UI
  framework

## License

GPL-2.0
