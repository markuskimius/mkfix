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
    idgen.py                # IdGenerator — prefixed business IDs (see ID scheme)
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
    panes/                   # Custom mkui pane types (session-manager, message-detail, replay-control)
```

## Architecture

mkfix builds its server with mkio's programmatic API: `create_app(cfg)` in `__main__.py` returns an `MkioApp`, which runs schema migration and service preflight before the event loop starts. The FIX TCP engine runs in the same asyncio event loop. FIX messages are written to SQLite via mkio's `WriteBatcher.submit()` with pre-compiled `CompiledOp` objects. The UI is an mkui app with custom pane types. UI commands flow through `FixCommandService`, a custom mkio `Service` subclass.

Key integration points:
- `create_app` / `MkioApp` — server bootstrap; `app.db` / `app.writer` / `app.change_bus` / `app.services` expose internals to the engine
- `app.add_service("fix_cmd", FixCommandService)` — custom service registration (not in TOML)
- `app.on_startup` / `app.on_shutdown` — no-arg async hooks; startup fires after services start, shutdown before they stop
- `WriteBatcher.submit(ops, params_list, data)` — batched writes from FIX engine
- `ChangeBus` — drives live UI updates when tables change
- `[static] "/" = "./static"` — mkio serves `index.html` at `/` and the directory's assets under `/static`; non-root routes (e.g. `/mkui`) serve at their own prefix

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

## UI panes

Blotter/viewer panes are declarative `mkio-table` configs in app.json. `raw-messages` sets `select: { state: "selected_message" }` so mkio-table mirrors the cursor's row into app state; `message-detail` subscribes to that path and renders the field breakdown. `fix_orders` stores raw FIX codes (`side_code`, `ord_type_code`) alongside display names so blotter buttons can send `fix_cmd` transactions with `${row.*}` interpolation. The client-side (order-blotter/trade-blotter, titled "Sent Orders"/"Received Trades") and market-side (market-order-blotter/market-trade-blotter, titled "Received Orders"/"Sent Trades") blotters share `orders_query`/`executions_query` and are split only by a `filter` on the `direction` column (`TX` = sent by this engine, `RX` = received); the titles state the direction directly. Received Orders drives everything through one Accept/Reject pair (`fix_cmd` ops `accept_request`/`reject_request`) plus Fill (`fill_order`): a new order arrives as `pending_action` "New", and inbound OrderCancelRequest (35=F) / OrderCancelReplaceRequest (35=G) park as "Cancel"/"Replace" with the request's ClOrdID and terms in the `pending_*` columns — `status` is untouched, so fills stay possible while a request is pending, and both buttons gate on `pending_action` alone. The dispatchers route to `accept_order`/`accept_cancel`/`accept_replace` and `reject_order`/`reject_cancel` (all also exposed as `fix_cmd` ops for scripting); accepting a cancel/replace sends an ER answering the request's ClOrdID (tag 11, with the superseded ID in tag 41, per the FIX chain) and moves the order row to that ClOrdID via the `rename_order` compiled op — on both sides a blotter row's ClOrdID always shows the chain's latest accepted ID, and the immutable `order_id` is the stable identity across the chain. Rejecting a request sends OrderCancelReject (35=9) with CxlRejResponseTo(434) and leaves the ClOrdID alone, and requests naming an unknown order are auto-rejected. The order upsert's SQL guards `order_qty`/`price` with `excluded`-based iif so an ER lacking tag 38/44 can't zero them. Sent Trades drives `correct_trade`/`bust_trade`, which use FIX 4.2 ExecTransType(20) Correct/Cancel with ExecRefID(19) — the exec blotter is append-only, so corrections and busts add rows (`exec_type` "Correct"/"Cancel") rather than mutating fills. New orders are entered through the Sent Orders "New" button, which opens an mkui dialog; its session dropdown is fed by `sessions_list`, a reqrep service, because dialog `optionsFrom` uses request-reply and query services only answer subscriptions. `session-manager` stays a custom pane by necessity: it merges `sessions_query` and `session_state_query`, and an mkio query service with JOIN sql drops change events from non-primary watch tables (`query.py` `_listen_changes`), so a joined view cannot live-update status. Its New/Edit buttons open an mkui dialog via `openDialog` (imported from `/mkui/src/widgets/mkui-dialog.js`) rather than an inline form; the dialog's field `name`s become the `session_mgmt` transaction payload, so they must match the op's TOML `fields` — Edit passes `session_id` and `enabled` as hidden fields because the update op requires them but the user must not change them. The tall session form is why the mkui floor is 0.1.54: earlier dialogs clipped a body taller than the default frame instead of growing.

## ID scheme

All generated business IDs come from `fix/idgen.py`: `<2-char type code><2-char instance code><8-digit counter>`, e.g. `RTMA00000001`. Type codes: `RT` ClOrdIDs mkfix sends, `OR` Order IDs (received orders' also go out as OrderID tag 37), `EX` ExecIDs, `TR` Trade IDs. The instance code is the first two characters of the username, uppercased and padded with trailing X's, so concurrent mkfix users facing the same counterparty mint distinguishable IDs. Per-prefix counters start at 1, increment forever, and persist in `fix_id_state` (written through the WriteBatcher so they serialize with engine writes — no reissued IDs across restarts).

Identity rules the engine enforces:
- `fix_orders.order_id` is write-once: the upsert SQL guards it with an `iif` (like `order_qty`/`price`) so an inbound ER's OrderID(37) can't replace the locally minted OR id on a sent order. Sent orders get `order_id` at `send_new_order`; received orders at `_handle_new_order` (arrival, not accept).
- A ClOrdID chain advances on accept: `accept_cancel`/`accept_replace` answer the request's ClOrdID in tag 11 (superseded ID in 41) and rename the row; `_handle_execution_report` mirrors this on the client side by renaming 41→11 before upserting. The `rename_order` op no-ops if a row already holds the target ClOrdID (duplicate ER), keeping the `(cl_ord_id, session_id)` unique index safe.
- `fix_executions.trade_id` is the immutable trade identity: minted at fill, copied to corrections/busts (market side from the referenced execution row; client side by looking up ExecRefID(19) among recorded executions). Each correction/bust still gets a fresh ExecID — the exec blotter stays append-only.

## mkio stream subscriptions

`ref` is optional since mkio 0.1.55 — omitting it starts from the beginning of the buffer. Stream panes may also page backward with `before: true` + `maxcount`.

`raw-messages` sets `live: true` (mkui 0.1.52+) so it opens streaming instead of parked on today's first page. The start page still loads first, so `start: "today"` keeps its meaning; without that handoff the pane would replay the whole message buffer on open. Live stream panes follow the tail as of mkui 0.1.54 (jump to newest on going live, stay pinned while at the bottom, stop following once the user scrolls up) — before that, new messages rendered below the fold and looked like they were never sent.

`tests/test_ui_config.py` guards app.json against the failure mode this config invites: dangling pane references, `index.html` imports of deleted modules, unknown service names, and a `mkio.expect.version` left behind by a release. It also statically checks the pane-module JS: imports must resolve (mkui paths against the installed `mkui.static_dir`), and services, transaction ops, and `fix_cmd` commands named in JS must exist server-side. These fail silently in the browser, so they are checked statically.

## mkio transaction defaults

Transaction service ops require explicit `defaults` in TOML config for any field that the client may omit. Fields listed in `fields` without a corresponding `defaults` entry are treated as required.

## Versioning

`mkfix/__init__.py` `__version__` is the single source of truth: pyproject.toml reads it via hatch dynamic version, and `_load_config` injects it as the server's reported version (mkfix.toml must not carry a `version` key). The one deliberate copy is in `static/app.json` (`mkio.expect.version` and the statusbar text) — it is the client build's baked stamp, so a stale cached client fails the handshake against an upgraded server. On a release bump, update `__version__` and the two app.json spots; `tests/test_ui_config.py` fails if they drift.

## Security notes

The Message Replay feature loads production FIX logs into test sessions, so files and hosts whose names carry `prod`/`production` may legitimately appear here — treat them with care: replayed production data stays on this machine and must never be committed, pushed, or sent to external services.

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
