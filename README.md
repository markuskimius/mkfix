# mkfix

A FIX protocol testing engine for capital markets connectivity, built on
[mkio](https://github.com/markuskimius/mkio) and
[mkui](https://github.com/markuskimius/mkui).

## Features

- **Session Management** -- Configure and run FIX sessions as initiator or
  acceptor from a live Sessions blotter showing status and sequence numbers,
  created and edited through modal dialogs. Buttons follow the session's state:
  Start, Edit, Delete, and Reset Seq only while a session is down, Stop only
  while it runs; the Reset Seq dialog opens prefilled with the current
  sequence numbers. Multiple sessions on the same port with different CompIDs.
- **Message Viewer** -- Live virtualized table of FIX messages in tag=value
  format, streaming live by default, with time-based paging, per-column filters,
  sorting, and clipboard copy.
- **Message Detail** -- Field-by-field breakdown of the message selected in the
  Messages viewer, with field names and enum translations.
- **Sent Orders & Received Trades Blotters** -- Live order state machine driven by
  ExecutionReports; fill and partial fill records. Send NewOrderSingle from
  the blotter's New dialog; Replace (Cancel/Replace) and Cancel working orders
  directly from the blotter -- a fully filled order can still be replaced up
  to revive it. An accepted cancel or replace moves the order to
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
  from the Sent Trades blotter (FIX 4.2 ExecTransType Correct/Cancel with
  ExecRefID).
- **Message Replay** -- Load production FIX logs and replay them into a test
  session with speed control, message filtering, and pause/resume.
- **IOI & Allocation Viewers** -- Indications of Interest and Allocation message
  tracking.
- **Session Protocol** -- Logon, Logout, Heartbeat, TestRequest, ResendRequest,
  SequenceReset, GapFill, PossDupFlag handling, and heartbeat timeout detection.

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

Then open http://localhost:8080 in your browser.

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

- [mkio](https://github.com/markuskimius/mkio) >= 0.1.65 -- async microservice
  framework (aiohttp + aiosqlite)
- [mkui](https://github.com/markuskimius/mkui) >= 0.1.54 -- Web Components UI
  framework

## License

GPL-2.0
