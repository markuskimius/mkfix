# mkfix

FIX protocol testing engine built on [mkio](https://github.com/markuskimius/mkio) (async microservice framework) and [mkui](https://github.com/markuskimius/mkui) (Web Components UI framework).

## Quick start

```bash
pip install -e .
mkfix                    # starts on port 8080 with built-in config
mkfix -p 9090            # override port
mkfix -d mytest          # use mytest.db (auto-adds .db extension)
mkfix -d :memory:        # in-memory database, no persistence
```

## Project layout

```
mkfix/
  __init__.py              # Package entry (__version__, lazy serve())
  __main__.py              # CLI (argparse) + aiohttp app setup
  mkfix.toml               # Default config: tables, services, static routes
  fix/
    dictionary.py           # FixDictionary — loads JSON tag/enum/message data
    dictionary_data/FIX42.json
    message.py              # FixMessage, FixMessageFactory, parse_fix()
    parser.py               # FixStreamParser — streaming TCP FIX message reader
    session.py              # FixSession — FIX session state machine
    transport.py            # FixSocket, FixInitiator, FixListener, FixServer
    engine.py               # FixEngine — session lifecycle, WriteBatcher bridge, replay
    replay.py               # Log file parser + ReplayTask
  services/
    fix_command.py           # FixCommandService — bridges UI commands to engine
  static/
    index.html, app.json, mkfix.css
    fix-dictionary.js, fix-formatter.js
    panes/                   # Custom mkui pane types (session-manager, raw-messages, etc.)
    widgets/form.js          # input/select/checkbox widgets for mkui
```

## Architecture

mkfix wraps mkio's aiohttp server. The FIX TCP engine runs in the same asyncio event loop. FIX messages are written to SQLite via mkio's `WriteBatcher.submit()` with pre-compiled `CompiledOp` objects. The UI is an mkui app with custom pane types. UI commands flow through `FixCommandService`, a custom mkio `Service` subclass.

Key integration points:
- `WriteBatcher.submit(ops, params_list, data)` — batched writes from FIX engine
- `ChangeBus` — drives live UI updates when tables change
- Custom service protocol via dotted-path string in TOML (`"mkfix.services.fix_command.FixCommandService"`)

## FIX session protocol

The session state machine in `session.py` handles:
- Logon/Logout handshake (initiator and acceptor)
- Heartbeat and TestRequest exchange
- Sequence number tracking (`_rx_seq_num`, `_tx_seq_num`)
- ResendRequest on sequence gap, Logout on sequence too low
- SequenceReset/GapFill processing
- PossDupFlag (tag 43) — skips app message processing for retransmits
- ResetSeqNumFlag (tag 141) — must be processed before advancing rx_seq_num

The `FixServer` in `transport.py` reads the first message from each TCP connection to route by CompID pair. It re-serializes the Logon back into the parser buffer so the session can process it normally.

## mkio stream subscriptions

Stream services require a non-empty `ref` string as a cursor. Use `ref: "0"` for initial subscription — empty string is falsy in Python and gets rejected.

## mkio transaction defaults

Transaction service ops require explicit `defaults` in TOML config for any field that the client may omit. Fields listed in `fields` without a corresponding `defaults` entry are treated as required.

## Running tests

```bash
pytest
```

## Conventions

- Python 3.11+, type hints throughout
- No comments unless the why is non-obvious
- FIX tags are always string keys (e.g., `"35"`, not `35`)
- Timestamps use FIX format: `YYYYMMDD-HH:MM:SS.mmm` UTC
- Config is TOML (mkfix.toml), UI layout is JSON (app.json)
- Static JS uses vanilla ES modules, no build step
- Custom pane types registered via `window.Mkui.registerPaneType()`
- Custom widgets registered via `window.Mkui.registerWidget()`
