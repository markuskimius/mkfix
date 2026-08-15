# mkfix

A FIX protocol testing engine for capital markets connectivity, built on
[mkio](https://github.com/markuskimius/mkio) and
[mkui](https://github.com/markuskimius/mkui).

## Features

- **Session Management** -- Configure and run FIX sessions as initiator or
  acceptor. Multiple sessions on the same port with different CompIDs.
- **Raw Message Viewer** -- Live virtualized table of FIX messages in tag=value
  format, streaming live by default, with time-based paging, per-column filters,
  sorting, and clipboard copy.
- **Message Detail** -- Field-by-field breakdown of the message selected in the
  Raw Message viewer, with field names and enum translations.
- **Order Pad** -- Send NewOrderSingle, Cancel, and Cancel/Replace requests.
- **Order & Trade Blotters** -- Live order state machine driven by
  ExecutionReports; fill and partial fill records. Cancel and Cancel/Replace
  working orders directly from the blotter.
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

1. **Create two sessions** in the Session Manager -- one initiator pointing at
   the other as acceptor on the same port.
2. **Start both sessions** -- Logon and Heartbeat messages will stream in the Raw
   Messages pane.
3. **Send an order** from the Order Pad -- see it in Raw Messages and the Order
   Blotter; click any message row to break it out field by field in Message
   Detail.
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
- [mkui](https://github.com/markuskimius/mkui) >= 0.1.48 -- Web Components UI
  framework

## License

GPL-2.0
