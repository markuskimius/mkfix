# mkfix/fix — actions, events and the order lock

What scripted scenarios stand on. None of it changes what a user sees.

## One way in: `FixEngine.perform(op, data, source)`

`actions.py` holds `ACTIONS`: every order and trade action by its `fix_cmd` name, each a small coroutine turning a loosely typed payload (a dialog submits strings) into the engine call and naming what it returns (`cl_ord_id`, `order_id`, `exec_id`). `FixCommandService._dispatch` hands any command in `ACTIONS` to `perform` — `save_as` templates are saved first, a UI concern — and keeps only the session, replay and dictionary commands as branches of its own; `test_ui_config._fix_cmd_commands` is the union the app.json guards check against. A script calls the same `perform` with `source="scenario"`, so it can do exactly what a button can, behind the same refusals (`_active_session`, `_require_live_trade`, the Restate checks). `ORDER_KEY`/`TRADE_KEY` name the payload key carrying the action's subject; `test_events.TestPerform` holds the table to `TEMPLATE_TERMS`.

## Events: `events.py`

`engine.events` is an `EventBus`: synchronous fan-out to listeners that must not block (they run on a session's read loop or inside an action); one that raises is logged and skipped. **Nothing listens until something subscribes**, and with no listener the engine skips the row reads an event costs (`events.active`).

An `EngineEvent` has `kinds` (most specific first; `kind` is the first), `session_id`, `source` (`wire` / `manual` / `scenario`), `order` and `prev` (the order row after and before — what lets a listener tell a fill that moved CumQty from a re-notified one, which the engine deliberately records as a new trade and infers nothing about), `trade`, `msg`, `request` (the ClOrdID a request or its answer names) and `detail`. `order_key` is `fix_orders.id`, the identity that survives every rename.

Every event is emitted **after its writes have committed** (`writer.submit` returns on commit), so a listener that queries finds what the event describes.

| Emitted by | kinds |
|---|---|
| `_handle_new_order` | `order`, `message` |
| `_handle_cancel_request` | `cancel` or `replace`, `message`; a request naming no order is `message` alone (`detail.unknown_order`) and is still auto-rejected |
| `_handle_execution_report` | `report_kinds(msg)` — `ack` `rejected` `canceled` `replaced` `pending` `expired` `done for day` `restated` `fill` (+ `filled` when 39=2) `corrected` `busted`, then `er`; from 150, from 39 where a dialect has no 150, and from 20 for a 4.2 bust/correct. A report that *creates* its order row (Message Replay sends around `send_new_order`) announces `sent order` first |
| `_handle_cancel_reject` | `cancel rejected`, `message`; `detail.response_to` (`cancel`/`replace`), `detail.reason` (102's name) |
| `_handle_dont_know_trade` | `dk`, `message` — only when it names a trade we sent |
| `update_session_state` | `session up` / `session down`, when a session's ACTIVE-ness flips; no order (`detail.status`) |
| `send_new_order` | `sent order` — after the row is written and **before the send**, so whoever takes the order owns it before an acknowledgement can arrive; `source` and `detail.tag` say who sent it |
| `perform` | `action` (`detail.op`, `detail.result`), `send_new_order` included. The subject is found before the action (an accepted replace renames it) and re-read after by row id. A refused action announces nothing |

## The order lock

`_market_action` decorates the eleven market-side actions (accept/reject order, fill, unsolicited cancel, restate, accept cancel/replace, reject cancel, correct, bust, re-notify). The decorated body loads its order, builds the answer and submits its writes, returning `(message, result)`; the wrapper holds `_order_lock(session_id)` across that, **then sends with the lock released**. `_handle_cancel_request` takes the same lock around its load and write.

Why: these actions write the whole of `ORDER_UPDATE_COLS` back from the snapshot they loaded, so a cancel/replace request parked between the load and the write lost its `pending_*`. A hand on a button rarely hit it; a script answering at wire speed would. The send stays outside because it awaits and the counterparty's answer may be handled inside it — by a handler that needs the lock (`test_the_lock_is_not_held_across_the_send`). One lock per session, not per order: an accepted replace renames the key an order would be locked by, and the sections are short. The client side needs none — its writes are single statements since 0.45 (the request slot ops).
