# mkfix

FIX protocol testing engine on [mkio](../mkio) (async microservices) and [mkui](../mkui) (Web Components UI).

## Quick start

```bash
pip install -e .
mkfix              # port 8080 (-p), built-in config
mkfix -d mytest    # mytest.db (.db added)
mkfix -d :memory:  # in-memory
mkfix -i Q7        # instance code in generated IDs, kept per database
mkfix check F.macro  # offline check; mkfix run F.macro runs it (macro/CLAUDE.md)
```

## Project layout

```
mkfix/
  __init__.py          # Package entry (__version__, lazy serve())
  __main__.py          # CLI (argparse) + aiohttp app setup
  mkfix.toml           # Default config: tables, services, static routes
  fix/
    dictionary.py       # FixDictionary + custom registry/merge
    dictionary_data/    # generated dictionaries (FIX40–FIX50SP2) + NOTICE
    idgen.py            # IdGenerator — prefixed business IDs (ID scheme)
    message.py          # FixMessage, FixMessageFactory, parse_fix
    parser.py           # FixStreamParser — streaming TCP reader
    session.py          # FixSession — session state machine
    transport.py        # FixSocket, FixInitiator, FixListener, FixServer
    engine.py           # FixEngine — session lifecycle, WriteBatcher bridge, replay
    actions.py, events.py  # perform(), event bus, order lock — see fix/CLAUDE.md
    families.py         # IOIs, adverts, allocations — see fix/CLAUDE.md
    instrument.py       # options, futures: SecurityType and its tags per version — see fix/CLAUDE.md
    lists.py            # lists: NewOrderList, orders carrying ListID, ListStatus — see fix/CLAUDE.md
    replay.py           # Message Replay — see fix/CLAUDE.md
  macro/               # macros, the scripting language — see macro/CLAUDE.md
  archive.py           # mkfix archive / restore over mkio's row archiving
  upgrade.py           # pre-0.34 mirror columns; pre-0.51 scenarios dropped
  services/
    fix_command.py       # FixCommandService — UI commands to engine
  static/                # macro UI: static/CLAUDE.md
    index.html, app.json, mkfix.css
    fix-dictionary.js, fix-formatter.js
    panes/               # custom mkui pane types
tools/
  quickfix_to_json.py    # regenerate dictionary_data from QuickFIX XML specs
  overlays/              # curated per-version overlays merged by the converter
```

## Architecture

`create_app(cfg)` in `__main__.py` returns an `MkioApp`, which runs schema migration and service preflight before the event loop starts. The FIX TCP engine runs in the same asyncio event loop and writes FIX messages to SQLite via mkio's `WriteBatcher.submit(ops, params_list, data)` with pre-compiled `CompiledOp` objects. The UI is an mkui app with custom pane types; its commands flow through `FixCommandService`, a custom mkio `Service` subclass.

`serve()` mirrors `MkioApp.run` rather than calling it: it probes the web port first (`_check_port`; mkio's `start()` opens the database before binding, and a bind failure there hangs the process) and prints the banner only once bound. The loop is mkio's `loop_factory` (uvloop; the selector loop on Windows).

Key integration points:
- `create_app` / `MkioApp` — server bootstrap; `app.db`/`app.writer`/`app.change_bus`/`app.services` expose internals to the engine
- `app.add_service("fix_cmd", FixCommandService)` — custom service registration (not in TOML)
- `app.on_startup` / `app.on_shutdown` — no-arg async hooks; startup fires after services start, shutdown before they stop
- `[static] "/" = "./static"` — mkio serves `index.html` at `/` and the directory's assets under `/static`; non-root routes (e.g. `/mkui`) serve at their own prefix

## FIX session protocol

The session state machine in `session.py` handles:
- Logon/Logout (both roles), Heartbeat/TestRequest, sequence tracking (`_rx_seq_num`/`_tx_seq_num`), ResendRequest on a gap, Logout on sequence too low, SequenceReset/GapFill
- PossDupFlag (tag 43) — skips app message processing for retransmits
- ResetSeqNumFlag (tag 141) — must be processed before advancing rx_seq_num
- Answering an inbound ResendRequest (`_answer_resend_request`) by replaying the recorded TX rows: `engine.sent_messages` returns the newest row per sequence number in the range; application messages go out again through `_retransmit` as `FixMessage.retransmit_copy` (43=Y, the original 52 moved to 122), while runs of admin types (`ADMIN_MSG_TYPES`) and unrecorded numbers collapse into one SequenceReset-GapFill whose MsgSeqNum is the first number of the run — what the counterparty is expecting — and NewSeqNo the number after it (`_tx_seq_num` when the run reaches the end). Neither path goes through `_send`, so no sequence number is spent; EndSeqNo 0 (or 999999 on 4.0/4.1) means through the last sent number, and a BeginSeqNo past it draws a session Reject. Sequence numbers recur across resets, so each outbound reset (`reset_sequence_numbers`, `reset_on_logon`, an inbound Logon with 141=Y) records the current `fix_messages` id as `seq_epoch` in `fix_session_state` (`_reset_seq_space`), and the replay query only looks past it.
- A local stop of an ACTIVE session terminates per the session protocol (`_initiate_logout`): a TestRequest first, waiting up to one heartbeat interval for the Heartbeat echoing its TestReqID, proof the peer processed everything sent before it — the spec only recommends this step, so `logout_test_request` (session column, default 1) can switch it off, and a silent peer delays but never blocks the Logout; then the heartbeat loop is cancelled (after our Logout nothing goes out unsolicited), Logout is sent, status becomes `LOGOUT_SENT`, and `stop()` waits for the read loop to end — the peer's confirming Logout returns it (not answered, but persisted: their Logout consumed a number), a ResendRequest arriving first is still honored — or for the peer to drop, for at most `logout_timeout` seconds (column, default 0 = twice `heartbeat_interval`, the spec's recommendation), then the socket closes. If the peer's EOF already ended the read loop before the stop, nothing is sent (`_connection_gone`). `FixEngine.stop` logs sessions out concurrently.

`FixServer` (`transport.py`) reads the first message from each TCP connection to route by CompID pair, pushing the Logon's bytes back into the parser buffer so the session processes it normally.

Messages are stored as wire bytes: `FixMessage.raw` is set by the stream parser on receive and by `FixSocket.write` on send, and `record_message` (the IOI, advert and allocation rows too) writes `to_wire_string()` (latin-1, SOH included) into `raw_message`; `to_pipe_string` is a rendering only, and storing it lost any `|` inside a value. `parse_fix` and the client's `splitFix` (fix-dictionary.js, behind its parsers and the Details pane) take SOH as the delimiter when present and fall back to `|` for text without any — typed input, replay log lines, and rows recorded before 0.18, which stay readable but lossy. The Messages pane shows the column through a `display` template replacing SOH with `|`, so cell and clipboard carry pipes while the stored value keeps SOH.

When a connection ends without a local stop (remote logout, drop), the reported status must match what the session still does: an acceptor returns to `LISTENING` (its listener stays registered), while an initiator detaches its finished transport and reports `DOWN`, since an attached transport would make `start()`'s re-entry guard silently no-op. The initiator's retry-limit `ERROR` path detaches the same way.

## FIX dictionaries

Standard dictionaries (`STANDARD_VERSIONS`: FIX.4.0–FIX.5.0SP2) are generated JSON in `fix/dictionary_data/`; `python tools/quickfix_to_json.py` regenerates them from the QuickFIX XML specs (license in NOTICE), merging `tools/overlays/<VER>.json` last: the FIX42 overlay preserves the display names the engine writes into DB rows (`test_ui_config.py` asserts they survive). Documents carry fields with types (`UTCTIMESTAMP` gets timezone display in the Details pane), enums, messages, ordered header/trailer, `begin_string` (what `sendprep` stamps in tag 8 — `FIXT.1.1` for the 5.0 dictionaries, whose header/trailer/admin messages are merged from FIXT11), and `groups` (counter tag → `delim` + `members`). The factory emits version-correct wire codes (`wire_exec_codes` in message.py): through FIX 4.2, fills go out as 150=1/2 alongside ExecTransType(20) and corrects/busts ride 20=2/1; from 4.3, tag 20 is withheld and fills/corrects/busts go out as 150=F/G/H (recorded as sent via `_sent_exec_kind`; both dialects recognized inbound). Tags the session dictionary doesn't define are withheld — 150/151 on 4.0, 141 on 4.0, 434 before 4.2 — body TransactTime(60) is dropped on D/F/G/9 before 4.2, CxlType(125)=F is added on a 4.0 OrderCancelRequest where it is required, and a FIXT.1.1 Logon carries DefaultApplVerID(1137) mapped from the app version. An accepted replace answers with OrdStatus(39)=5 where the dictionary defines it; FIX 4.4 removed that value (5.0 restored it), so there the ER and blotter carry the working status (New/PartiallyFilled/Filled) with ExecType=5 alone marking the event. All of it keys off `defines`/`has_enum`, so a custom dictionary removing a tag stops it being emitted. A 5.0 session speaks 4.x mechanics.

Custom dictionaries live in the `fix_dictionaries` table: `doc` is JSON, a delta over `base_version` when set, else a full document. Merge semantics are `merge_dictionary` in dictionary.py, mirrored client-side by `mergeDictionary` in fix-dictionary.js (fields/messages/groups per tag with null removing, enums per tag per code union with null removing, header/trailer/begin_string wholesale). Rows are written only through `fix_cmd` (`save_dictionary`/`delete_dictionary`) so dictionary.py's registry stays in step; `FixDictionary(name)` checks the registry before the shipped files, making a custom name valid wherever a version string is accepted. The engine registers rows at startup (`_load_custom_dictionaries`, before sessions are built) and refuses to delete a dictionary a session's `dictionary` column names. Sessions bind by name: `fix_sessions.dictionary` (`''` = the standard one for `fix_version`). `start_session` reloads config first, and `reload_session` rebuilds a stopped session's `FixSession` (dictionary and factory are `__init__`-time wiring) while only swapping `config` on a running one, so a dictionary edit applies on next start. `dictionaries_list` (reqrep; `''` + standard names + custom rows) feeds the session dialogs' Dictionary dropdown. Client side, `loadDictionary(name)` fetches standard versions from the `/dictionaries` static route and custom names through `fix_cmd get_dictionary` (the server resolves deltas), caching per name; the Dictionaries pane calls `invalidateDictionary` on save.

## UI panes

Blotter/viewer panes are `mkio-table` configs in app.json. `columns` lists every column the pane's service returns (table columns plus `AS` aliases), so the picker can show any of them; `visible` is the default view, internals (`id`, `direction`, codes, raw bytes) listed but hidden. A new table column joins every pane over its table, hidden unless wanted (`TestEveryColumnReachable`). `raw-messages` sets `select: { state: "selected_message" }` so mkio-table mirrors the cursor's row into app state; `message-detail` subscribes to that path and renders the field breakdown. `fix_orders` stores raw FIX codes (`side_code`, `ord_type_code`) alongside display names so blotter buttons can send `fix_cmd` transactions with `${row.*}` interpolation. The client-side (order-blotter/trade-blotter: "Sent Orders"/"Received Trades") and market-side (market-order-blotter/market-trade-blotter: "Received Orders"/"Sent Trades") blotters share `orders_query`/`executions_query` and are split only by a `filter` on the `direction` column (`TX` = sent by this engine, `RX` = received). What the order and trade buttons and dialogs do — accept/reject, fill, unsolicited cancel, restate, correct/bust, DK, re-notify, New/Clone/Replace, expiry — is in `mkfix/fix/CLAUDE.md` (Order and trade actions). `session-blotter` is a plain `mkio-table` over `sessions_query`, whose `sql` LEFT JOINs `fix_session_state` (the state of record: status, sequence numbers, `error_text`, timestamps) onto `fix_sessions` with `watch_tables` naming both, so every state write re-runs it and publishes the changed session row. Its buttons gate on the row's status through `enable.when` expressions (`ALL(rows, r -> ...)` on multi-select buttons): Start, Edit, Delete, Reset Seq and Change Seq only while stopped, Stop only while running/transitional. `orders_query`/`executions_query` join the same table for a `session_status` column only, and `watch_columns` names it, so per-message seq-num writes never re-query; they set `key = ["id"]` because mkio otherwise builds `_mkio_row` from every watched table's key and the `id`-keyed history blocks would stop matching live rows. Every button acting on an existing order or trade gates on `r.session_status == 'ACTIVE'` (New is exempt); the engine enforces `_active_session` too. New/Edit/Change Seq open dialogs declared inline in app.json; a dialog's field `name`s become the transaction payload, so they must match the op's TOML `fields` — the session dialogs pair Version/Dictionary and Client Tags/Timestamps as half-width `row`s, Edit passes `session_id` and `enabled` as hidden `${row.*}` fields, and Change Seq asks for Tx/Rx sequence numbers and submits `fix_cmd` `reset_sequence`, while Reset Seq is a one-click transaction submitting the same op with literal `tx_seq_num`/`rx_seq_num` of 1 — a session reset, `seq_epoch` fenced. Everything conditional in app.json (`styles`, `rowStyle`, `enable.when`, query `filter`s) is mkio's expression language. FIX stamps aren't ISO-8601, so each timestamp column declares `{"type": "time", "parse": "%Y%m%d-%H:%M:%S.%f"}` (UTC; `trade_date` uses `%Y%m%d`) plus `format` (`%f` keeps a wire stamp's digits) and `zone: "local"`: shown, copied and range-picked in the browser's zone, stored UTC; `trade_date` and Replay's From/To (times of day) stay raw. `filters` sets the default views: Messages excludes `Heartbeat` by `msg_type_name`, the four order/trade blotters open on a rolling `preset: "today"`. mkui ignores unknown pane keys silently, so `test_ui_config.py` rejects legacy keys, compiles every `when`/`filter` with `mkio.expr`, and checks `types` entries and timestamp columns against `_fix_timestamp()` output.

The Details pane (`message-detail`) translates through the owning session's dictionary (a live `sessions_query` map of session → `dictionary` or `fix_version`, falling back to the message's BeginString (not `FIXT.1.1`, which names no app version) then FIX.4.2) and renders `parseMessageTree` output: collapsible groups and sections (persisted), drag-resizable columns (double-click fits), `UTCTIMESTAMP` values and the header stamp in a selectable timezone; prefs in localStorage under `mkfix.detail.*`. `dictionaries` is a custom pane (Fields/Enums/Messages/Groups/Wire tabs) over the `fix_cmd` dictionary ops: edits mutate the working doc client-side, Save persists and invalidates the client cache; New/Clone offer linked-delta vs full-copy, Flatten converts derived→standalone, Import/Export round-trip documents (`<name>.json`) or deltas (`<name>.delta.json`). `test_ui_config.py` guards the four copies of the standard-version list.

## IOIs, adverts, allocations, RFQs

The market sends these: Sent IOIs/Adverts/Allocations under Market (`market-ioi-blotter`…), Received ones under Client (`ioi-blotter`…), tabs beside the order blotters, each pair one query (`iois_query`… , the orders_query join) split by `direction`, with history and the side's macro deck. Sent: New, Clone, Replace, Cancel, History, Macro… (a macro from a row's messages, on the order blotters too); Received IOIs: Order (the New Order form with `23=<IOIID>` in Extra Tags); Received Adverts: History only; Received Allocations: Accept/Reject over the request slot, Received Orders' way, whose Allocate opens New Allocation from the received order. Template scopes `ioi`, `advert`, `allocation`, `alloc_accept`, `alloc_reject`; cancels share `cancel`. Macros speak them (macro/CLAUDE.md). Rows and engine: fix/CLAUDE.md; guards: `TestFamilyBlotters`, `test_families.py`. RFQs (`fix_rfqs`, `rfqs_query` split by `origin`+`direction`): Sent RFQs/Received Quotes (Client), Received RFQs/Sent Quotes (Market); RFQ requests (`fix_rfq_requests`) sent by Market (`TestRfq*Blotters`, `test_rfqs.py`). Lists (`fix_lists`, `lists_query` split by `direction`): Sent Lists (Client) and Received Lists (Market), each linked to its side's order blotter by `list_id` (`link.broadcast`/`listen`, chips off), so a selected list narrows it to its orders (`TestListBlotters`, `test_lists.py`; fix/CLAUDE.md).

## Templates

Blotter action templates: `fix_templates` (`scope` in `TEMPLATE_SCOPES`, `name`, the `TEMPLATE_TERM_COLS` as text, blank meaning "ask the row"; archive group `config`, alias `templates`; a name is unique per scope via `_ensure_indexes`, newest duplicate kept). Each order/trade dialog opens on a `_template` select (`optionsFrom` `templates_list` with `params.scope`, mkui's `fill` mapping each term column onto the same-named field, so a pick fills the form) and closes on an optional `save_as` field; options are keyed by name and the select's `remember` (`mkfix.template.<op>`; `save_as` when typed, else the pick) reopens it on the last template. A pinned dialog keeps its terms (`pin: "keep"`); `save_as` alone resets (`pin: "reset"`) to save once. `_` keeps the pick unsubmitted; `save_as` reaches `fix_cmd`, whose `TEMPLATE_TERMS` maps the twelve ops to scope and term keys (only order templates keep a session, Replace's via rowData) and calls `FixEngine.save_template` (a scope+name upsert) first, so a failed save stops the send. The `templates` pane (Config menu; `mkio-table` over `templates_query`) carries Edit (one dialog over the term superset gated by `row.scope`), Clone (Edit's form under `<name> copy`) and Delete, through `templates`.

## Saved layouts

The Layout menu is mkui's saved-layouts feature, opt-in per app: a `layouts` block (constructs mkui's `LayoutManager`, registers the `layout.*` actions; `key: "mkfix"` so the store key doesn't ride on the app title) *and* the menubar entries (`layout.save`, a `{"layouts": true}` Restore submenu, `layout.reset`). With `mkio.url` set the store is the server: mkfix.toml declares the `mkui_layouts` table, transaction service (`save`/`delete`) and `mkui_layouts_list`/`mkui_layouts_get` reqreps (no login, so every save lands under owner `''`). `test_ui_config.py` keeps menu, block and services together.

The Help menu is mkui message boxes: Shortcuts is `dialog.open` on `dialogs.shortcuts`, About is `dialog.about` from the `app` block; `TestHelpMenu` checks both.

## Record history

`fix_sessions`, `fix_orders`, `fix_executions` and the IOI, advert and allocation tables are `versioned = true`: every change to a row lands in `<table>__history`, and the live row's `_mkio_version` is the cursor into that chain. mkio's writer owns the counter and records a version only when a versioned column changed, hand-written ops included. `fix_session_state` is unversioned, so heartbeats and session transitions record nothing. `upgrade.py` drops the pre-0.34 mirror columns at startup and from archives on restore. A session's history is its config edits, an order's its FIX lifecycle (one chain keyed by the immutable `id`, renames included), a trade's its fill, corrections and bust (keyed by `id`, one `trade_id` throughout).

mkio writes no service for a history table, so mkfix.toml's "Record History" section declares, per table, a `<x>_versions` reqrep (one record's chain), `<x>_state` reqrep (live version and top, which arms Redo), `<x>_history` query over the history table (mkui's History pane feed, narrowed server-side by key, so the key is `filterable`), and as-of reqreps — split by direction (`sent_orders_as_of` etc.) because a pane's `filter` narrows only its live subscription. Each record blotter (the five, and the six IOI/advert/allocation ones) carries a `history` block naming those services and a `History` button (`unit: "row"`, action type `action`, `args.pane` naming the blotter it sits on — the `history` block alone brings Undo/Redo/As of…, not the timeline), the Edit menu (FIX, Edit, Client, Market, Macro, Config…: `TestMenubar`) leads with `edit.undo`/`edit.redo` labelled "Undo/Redo Session Change" since only sessions are undoable (no menu opens a history: `table.history` would follow whichever blotter's selection is current), and Sessions labels `_mkio_version` "Ver" (the FIX version column is "Version"). `TestRecordHistory` guards all of it.

Undo and redo are offered on Sessions only (`session_mgmt` `undo`/`redo` ops, `confirm` on): an undo rewinds only the local row — the counterparty's view of an order or trade does not move, and the next ExecutionReport would upsert over the restored version — so orders and trades stay read-only. A Delete is real in mkio and takes the chain with it, so it cannot be undone. `FixEngine.handle_undo_redo`, registered with `app.on_undo_redo` in `__main__.py` before start (the engine is built in the startup hook, so the callback dispatches through the closure), follows a cursor move: an edit calls `reload_session` (as Edit does); an undone Add stops and drops the session and deletes its `fix_session_state` row via the `delete_state` op, since that table is not versioned; a redone Add inserts a DOWN state row and rebuilds. The undo button gates only on version state, so a running session's config undo applies on its next start, and on a baselined session at version 1 it deletes the row.

## Archiving

`mkfix archive`/`mkfix restore` wrap mkio's row archiving. Tables come from mkfix.toml's `archive = {...}` keys: the running-data tables are group `data`, cut by their FIX-stamped column (`timestamp`; `created_at` for orders, `format = "%Y%m%d-%H:%M:%S.000"`); the config/state tables are group `config` (`fix_sessions` whole with `fix_session_state` as companion, the rest whole, `mkui_layouts` by `saved`), archived only by name. `test_archive.py` requires every table archivable or a companion. Defaults: cutoff = local midnight today (`default_cutoff`), the data group, `--out ./archive`, short names in `ALIASES`; `-d/-p/--host` match the server and `server_answers` probes `/api/services` for `fix_cmd` to pick the mode: online through the server's `_mkio` archive request (`--url`/`--offline` force it), else on the file with the server assumed down. Previews, then prompts unless `-y`. Online runs go through `app.on_archive` → `FixEngine.check_archive` (refuses a session not `DOWN`/`ERROR`, a dictionary bound to a session not leaving in the same run, and `fix_id_state` while the engine holds counters) and `after_archive` (drops session objects, `unregister_custom`). Restore is offline only and refuses while a server answers. A ResendRequest into archived messages becomes a GapFill; a trade whose order is gone can't be corrected, busted or re-notified.

## Client column

The tag naming an order's client differs by counterparty (ClientID(109) through 4.2, PartyID(448)/PartyRole(452)=3 from 4.3, OnBehalfOfCompID(115), Account(1), custom), so `fix_sessions.client_tags` names it per session: comma-separated specs — a tag, or a group member qualified by a sibling (`448[452=3]`) — first present wins, blank = `DEFAULT_CLIENT_TAGS` (`448[452=3],109,115,1`). `parse_client_tags` (message.py) is strict; the engine's `_client_specs_of` falls back to the default on a malformed value, since a plain transaction writes the column. `client_of` reads a message through the specs and fills `client` on every `fix_messages` row, on received orders at arrival and on sent orders as actually sent (`_client_as_sent` sendpreps a copy). The New/Replace Client field (an order template term; Cancel passes the row's as rowData) and every market-side answer (with the order's client) go through `_stamp_client` → `FixMessageFactory.stamp_client`: the first spec the dictionary defines, else the first as written, a group spec as a one-instance group with 447=D where defined; skipped when the extra tags name a client, so extras win and an echoed Parties group isn't sent twice. Trades take their ER's client, else their order's; a Replace rewrites it (`ENTERED_COLS`). `_backfill_client` seeds old rows. Every blotter and Messages show and filter `client`.

## Extra tags on outgoing messages

Every send action (the order, request, fill, trade and DK ops) takes an optional `extra_tags` string — pipe- or SOH-delimited `tag=value` pairs, parsed by `parse_extra_tags` in `message.py` into an ordered list allowing duplicate tags (a repeating group on the wire). The pairs ride on `FixMessage.extra` and are applied by `sendprep`, which composes an ordered pair list (`_pairs`; `fields` stays the dict view). Placement rules: an empty value (`21=`) deletes the tag, even a computed one like 52; a tag appearing once in extras that the message would emit anyway is overridden in place (34/49/52 too, and 9/10 — the escape hatch for deliberately corrupt messages); everything else appends in given order — header tags at the end of the header block, trailer tags before 10, the rest after the body. Tags are validated only as "numeric tag, has an `=`". Every blotter action button is a dialog carrying the optional `extra_tags` field (`rowData` + `submitPerRow`, so multi-select submits per row); no dialog is `modal` (tested): each acts on the rows captured at the click. Parsed messages (`parse_fix`, `FixStreamParser`) keep their ordered wire pairs on `_pairs`, duplicates included, so re-serialization is byte-faithful. The market side echoes inbound custom tags: `extra_pairs_of` (a D/F/G's tags outside header/trailer and `CONSUMED_ORDER_TAGS`, the ones the engine maps to columns) is formatted by `format_extra_tags` onto the received order's `extra_tags` (and `pending_extra_tags` for whatever is pending — the D's own tags for a New, the F/G request's for a Cancel/Replace). The Accept/Reject dialogs prefill Extra Tags from `pending_extra_tags` and Fill from `extra_tags`, so inbound tags ride back on the answering ER after review; accepting a replace promotes the request's tags to the order's, and acting on a request (or a fill consuming the pending New) clears `pending_extra_tags`. `_backfill_rx_extra_tags` seeds old received orders.

## HandlInst, Text and trade tags

New/Replace carry `handl_inst` (21; default `1`, withheld when blank or undefined), every order/trade dialog a never-prefilled `text` (58); an extra `58=`/`21=` still wins, and rows record what was sent (`_handling_as_sent`, `_text_as_sent`). `handl_inst`/`handl_inst_code`/`sent_text` sit in `ENTERED_COLS`: `sent_text` is the 58 we last sent on the order (requests via `_record_entered`, blank included; answers via `_write_order`), set by `coalesce(?, …)` so an inbound ER (`keep_sent_text`) leaves it; `text` ("Rcvd Text") is the counterparty's last 58, F/G requests included. `fix_executions.extra_tags` (`EXEC_UPDATE_COLS`): a sent trade's extras as typed, a received ER's custom tags (`CONSUMED_EXEC_TAGS`); it prefills Correct/Bust/Re-notify.

## Write-before-send ordering

Every engine action submits its database writes *before* `session.send_message`. The send awaits, so the read loop can process the counterparty's answer in between; sending first was a live race: an ER reaching `upsert_order` before `send_new_order`'s row existed lost its status to the action's own upsert, an inbound cancel request's `pending_action` was overwritten by the post-send `_write_order`, and an accepting ER renamed the chain before `_record_entered` found the order. The WriteBatcher queue is FIFO, so writing first is deterministic (`_write_order`'s docstring holds the rule). A failed send lands after the writes: `send_new_order` marks its row `Rejected` with `Send failed: …`. `TestAnswerBeforeOwnWrite`/`TestRequestDuringMarketSend` guard this.

## ID scheme

All generated business IDs come from `fix/idgen.py`: `<2-char type code><2-char instance code><8-digit counter>`, e.g. `RTMA00000001`. Type codes: `RT` ClOrdIDs mkfix sends, `OR` Order IDs (received orders' also go out as OrderID tag 37), `EX` ExecIDs, `TR` Trade IDs, `IO`/`AD`/`AL` IOI, advert and allocation IDs, `RQ`/`QT`/`QR`/`RR` QuoteReqID/QuoteID/QuoteRespID/RFQReqID, `LI` ListIDs. The instance code is the username's first two characters, uppercased and X-padded; `-i/--instance-code` overrides it, saved in `fix_settings` (see idgen.py). Counters live per type prefix in `fix_id_state`, start at 1 and persist through the WriteBatcher, serialized with engine writes — no reissued IDs across restarts.

The identity rules the engine enforces (write-once `order_id`, ClOrdID chains advancing on accept, the request slot, `trade_id`, trades resolving their order by `order_id`) are in `mkfix/fix/CLAUDE.md` (Identity rules).

## mkio stream subscriptions

`raw-messages` sets `live: true` (streaming), so `start: "today"` holds. Stream panes page backward with `before: true` + `maxcount`.

`tests/test_ui_config.py` guards app.json against silent failures (dangling panes, imports, services, ops, a stale version stamp).

## mkio transaction defaults

Transaction ops need TOML `defaults` for any field the client may omit; otherwise it is required.

## Versioning

`mkfix/__init__.py` `__version__` is the single source of truth: pyproject.toml reads it via hatch dynamic version, and `_load_config` injects it as the server's version (mkfix.toml carries no `version` key). The one deliberate copy is in `static/app.json` (`mkio.expect.version`, the statusbar text, the About box's `app.version`), the client's baked stamp, so a stale client fails the handshake. A release bump updates `__version__` and the three app.json spots; `test_ui_config.py` fails on drift. The framework floors are pinned in `pyproject.toml`'s `dependencies`, the README's Dependencies list, and `mkio.expect.mkio` in app.json, which must equal the pyproject mkio floor's major.minor: the server compares it by caret semver, so a 1.x minor stays compatible and a 2.x server paints the statusbar red until floor, cap (`<2`) and pin move. `expect.expr` pins `mkio.expr`'s `LANGUAGE_VERSION` exactly (2 since mkio 1.5/mkui 1.10: `and or not in`, durations, `COUNT`) and moves only when the language does.

## Security notes

Message Replay loads production FIX logs into test sessions, so files and hosts named `prod`/`production` may appear. Replayed production data stays on this machine: never commit, push or send it anywhere external.

## Running tests

`pytest`

## Conventions

- Python 3.11+, type hints throughout; Linux, macOS, Windows. Text I/O names an `encoding` (`test_platform.py`); `.gitattributes` pins LF.
- Comment only non-obvious whys
- FIX tags are always string keys (`"35"`, not `35`)
- Timestamps use FIX form: `YYYYMMDD-HH:MM:SS.mmm` UTC. Outgoing wire timestamps (52/60) follow the session's `timestamp_precision` (`''` = protocol standard: seconds through FIX 4.1, milliseconds from 4.2; explicit second through picosecond override, sub-nanosecond digits zero-padded). The factory owns the resolved value (`FixMessageFactory.timestamp_precision`; `standard_precision` in message.py); DB timestamps stay milliseconds.
- Static JS: plain ES modules, unbuilt
- Custom panes register with `window.Mkui.registerPaneType()`
