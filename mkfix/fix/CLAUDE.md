# mkfix/fix — actions, events and the order lock

What scripted macros stand on. None of it changes what a user sees.

## One way in: `FixEngine.perform(op, data, source)`

`actions.py` holds `ACTIONS`: every order, trade, IOI, advert and allocation action by its `fix_cmd` name, each a small coroutine turning a loosely typed payload (a dialog submits strings) into the engine call and naming what it returns (`cl_ord_id`, `order_id`, `exec_id`). `FixCommandService._dispatch` hands any command in `ACTIONS` to `perform` — `save_as` templates are saved first, a UI concern — and keeps only the session, replay and dictionary commands as branches of its own; `test_ui_config._fix_cmd_commands` is the union the app.json guards check against. A script calls the same `perform` with `source="macro"`, so it can do exactly what a button can, behind the same refusals (`_active_session`, `_require_live_trade`, the Restate checks). `ORDER_KEY`/`TRADE_KEY` name the payload key carrying the action's subject; `test_events.TestPerform` holds the table to `TEMPLATE_TERMS`.

## Events: `events.py`

`engine.events` is an `EventBus`: synchronous fan-out to listeners that must not block (they run on a session's read loop or inside an action); one that raises is logged and skipped. **Nothing listens until something subscribes**, and with no listener the engine skips the row reads an event costs (`events.active`).

An `EngineEvent` has `kinds` (most specific first; `kind` is the first), `session_id`, `source` (`wire` / `manual` / `macro`), `order` and `prev` (the order row after and before — what lets a listener tell a fill that moved CumQty from a re-notified one, which the engine deliberately records as a new trade and infers nothing about), `trade`, `msg`, `request` (the ClOrdID a request or its answer names) and `detail`. `order_key` is `fix_orders.id`, the identity that survives every rename.

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
| `perform` | `action` (`detail.op`, `detail.result`, `detail.data` — the payload as given, minus `_` keys — and `detail.trade_before`, the trade row as the action found it: what the macro recorder writes scripts from), `send_new_order` included. The subject is found before the action (an accepted replace renames it) and re-read after by row id. A refused action announces nothing |

## The order lock

`_market_action` decorates the eleven market-side actions (accept/reject order, fill, unsolicited cancel, restate, accept cancel/replace, reject cancel, correct, bust, re-notify). The decorated body loads its order, builds the answer and submits its writes, returning `(message, result)`; the wrapper holds `_order_lock(session_id)` across that, **then sends with the lock released**. `_handle_cancel_request` takes the same lock around its load and write.

Why: these actions write the whole of `ORDER_UPDATE_COLS` back from the snapshot they loaded, so a cancel/replace request parked between the load and the write lost its `pending_*`. A hand on a button rarely hit it; a script answering at wire speed would. The send stays outside because it awaits and the counterparty's answer may be handled inside it — by a handler that needs the lock (`test_the_lock_is_not_held_across_the_send`). One lock per session, not per order: an accepted replace renames the key an order would be locked by, and the sections are short. The client side needs none — its writes are single statements since 0.45 (the request slot ops).

## IOIs, adverts and allocations: `families.py`

Three more message families on the order model, each a chain of IDs: an IOI (35=6) is New/Replace/Cancel by IOITransType(28) with IOIRefID(26) naming the one superseded, an Advertisement (35=7) the same by AdvTransType(5)/AdvRefID(3), an AllocationInstruction (35=J; *Allocation* through 4.2) by AllocTransType(71)/RefAllocID(72). Only the allocation is answered — an AllocationInstructionAck (35=P) carrying AllocStatus(87) and, refused, AllocRejCode(88) — so only it has a request slot; nothing answers an IOI or an advert (the response to an IOI is an order carrying its ID in tag 23, which `fix_orders.ioi_id` records on both sides and `_link_ioi_order` writes back onto the IOI row's `order_cl_ord_id`; 23 is a consumed order tag). `families.py` is the pure part: `ioi_columns`/`advert_columns`/`allocation_columns`/`ack_columns` map a message to its row's term columns; `group_instances` reads a repeating group off the ordered wire pairs (the dictionary's member list says where an instance ends, `members` which tags the row keeps) and `group_pairs` writes one; an allocation's three groups (`ALLOC_GROUPS`: orders 73 → 11/37, executions 124 → 17/32/31, accounts 78 → 79/80/366) are kept as lines — `parse_lines`/`format_lines`, one instance per line or `;`, members by comma or space, the form the dialogs' textareas take and the cells show. The factory's `ioi`/`advertisement`/`allocation_instruction`/`allocation_ack` emit version-correct wire (626 from 4.3, 366 from 4.2, the IOI's 60 from 4.3, 25/130/199 where defined; groups ride on `extra` after the body, before the user's extras).

`fix_iois`, `fix_adverts`, `fix_allocations` (`IOI_COLS`/`ADVERT_COLS`/`ALLOC_COLS`): one row per chain, versioned, the ID column the chain's latest ID and `*_ref_id` the one before, split by `direction` into a Sent and a Received blotter. Generic ops `insert_<table>`/`update_<table>` (by row id, everything but session/direction/`timestamp`), `_find_family_row` (newest row holding an ID, per direction), `_sent_family_row` (columns from the message as sent — `_as_sent`, extras applied), `_received_family_row`. Inbound: `_handle_ioi`/`_handle_advertisement` insert a New, rewrite the row a Replace names (its terms), mark the one a Cancel names Canceled (terms kept); a reference matching nothing starts a row. `_handle_allocation`: a New parks as `PendingNew`/`pending_action` New; a Replace/Cancel (71=1/2) parks in the slot of the row 72 names — `pending_alloc_id` the request's 70, `pending_terms` its columns as JSON (a Cancel's only its identity and message), `pending_extra_tags` — under the session's order lock; one naming no row is answered at once with 87=1/88=7 "Unknown allocation". `_handle_allocation_ack` answers a sent row by the chain's current ID (the New, or an ack again) or by `pending_alloc_id`: accepted (87 in `ALLOC_ACCEPTING`: 0/3/4) the slot's terms and ID move onto the row (`_promoted_allocation`), Canceled for a Cancel; refused the row keeps them; both record `alloc_status`/`alloc_rej_*`/`text` (theirs) and `ALLOC_STATUS_OF` gives `status`. Actions (`SUBJECT_KEY`/`CREATES` naming their rows for `perform`'s event; `UNSCRIPTED` in actions.py holds only the RFQ ops since 0.64 gave these verbs): `send_ioi`/`replace_ioi`/`cancel_ioi` and the advert three write the row first, a Replace renaming it at once (nothing answers) and a Cancel keeping its terms, with the previous row put back if the send fails (`_send_family_message`; a new row that fails to go out is `Failed`); `send_allocation` (status `Sent`), `replace_allocation`/`cancel_allocation` park in the slot until the Ack (a failed send clears it); `accept_allocation`(`alloc_status` 0/3/4)/`reject_allocation`(1/2/5, `alloc_rej_code`) are `_market_action`s over the received row's slot, answering the request's ID, an accepted Replace/Cancel renaming the chain as the ER path does for orders, `sent_text` ours. IDs `IO`/`AD`/`AL`. Allocate (Received Orders) reads the order's live fills through the `order_fills` reqrep — a `_fills` pick whose `fill` copies executions-as-lines, total and average onto the form, remembered so the next open fills at once; Sent Trades' Allocate allocates the one trade. Events: `ioi`/`ioi replaced`/`ioi canceled`, `advert …`, `allocation`/`allocation replace`/`allocation cancel`, `allocation accepted`/`received`/`incomplete`/`rejected` then `allocation acked` (every Ack, as every ER is `er`), `sent ioi`/`sent advert`/`sent allocation` (with `source` and the macro's `tag`, as `sent order`), each with `table`/`row` and no `order`; the macro runner routes them by `(kind, row id)` with the family's name dropped from the kind. `_backfill_families` (fix_settings `families_backfill`) re-derives the pre-0.63 viewer rows from `raw_message` and folds a Replace/Cancel into the row it names. `tests/test_families.py` covers all of it; `TestFamilyBlotters` the panes.

## RFQs and quotes: `families.py`, engine.py

`fix_rfqs` (`RFQ_COLS`) holds one row per negotiation. There are two kinds, told apart by `origin`:

- **`rfq`**: a QuoteRequest (35=R) and the quotes that answer it.
- **`quote`**: unsolicited quotes on one instrument.

The row's quote columns hold the quote standing now. A requote is a new version of the row, so the row's history is the negotiation. `direction` is who opened the row. A side owns two kinds of rows (`quote_side`): the client's are sent RFQs and received quotes, the market's the other two. `_find_family_row` takes `client`/`market` in place of a direction to select them, and `SUBJECT_KEY` names the side the same way.

**Pure part** (families.py): `rfq_columns`, `quote_columns`, `response_columns`, and the `CONSUMED_*_TAGS` sets.

**Factory:**
- `quote_request` puts the instrument in the body through FIX 4.1, and in one NoRelatedSym(146) instance from 4.2. That instance also carries 303/537/54/38/15/60 where they are defined.
- `quote` sends 537 from 4.3, 54/38 from 4.4, and 60/15 from 4.2.
- `quote_cancel` sends 298=1 with the symbol in 295.
- `quote_request_reject` carries 658 and the 146 group.
- `quote_response` carries 693/117/694; a Hit adds 11/54/38/40/44.
- The engine refuses a message the session's dictionary lacks (`_require_message`): Z before 4.2, AG before 4.3, AJ before 4.4.

**Inbound:**

| Message | Handler | Effect |
|---|---|---|
| R | `_handle_quote_request` | opens an Open row |
| S | `_handle_quote` | quotes or requotes the row its 131 names, else the live unsolicited chain on its symbol (`_live_quote_chain`: status Quoted/Countered), else opens a `quote` row. A quote on a finished row (`FINAL_RFQ`) keeps that row's status |
| Z | `_handle_quote_cancel` | 298=4 cancels every live quote; otherwise the quote its 117 or 131 names, else the symbols in its 295 group |
| AG | `_handle_quote_request_reject` | rejects the RFQ |
| AI | `_handle_quote_status_report` | records 297; a status in `QUOTE_STATUS_ENDS` ends the quote |
| AJ | `_handle_quote_response` | see below |

On a QuoteResponse (AJ):
- **Counter** parks in the slot (`pending_*`), status Countered.
- **Hit with an 11** becomes a received order through `_handle_new_order` (`_order_from_response` fills side, size and price from the quote, 40=D).
- **Anything else** sets the status by `status_of_response`.

Every handler but R's holds the order lock.

**Orders naming a quote.** Tag 117 is a consumed order tag, recorded as `fix_orders.quote_id`. `_link_quote_order` marks the quote Hit and stores `order_cl_ord_id`, on both sides (sent orders by `send_new_order`).

**Actions** (`CREATES`/`SUBJECT_KEY` as for the other families):

| Side | Action | What it does |
|---|---|---|
| Client | `send_rfq` | writes the row, then announces `sent rfq`, before the send |
| Client | `hit_quote` | AJ Hit; writes the sent order (`_sent_order_row`, shared with `send_new_order`) and announces `sent order`; a failed send puts the quote back and rejects the order |
| Client | `counter_quote` | parks our counter in the slot |
| Client | `pass_quote` | |
| Market | `quote_rfq`, `requote` | `_market_action`s; clear the slot, since a requote answers a counter |
| Market | `reject_rfq`, `cancel_quote` | `_market_action`s |
| Market | `send_quote` | replaces the live chain on its symbol or opens a row |

**Validity and expiry.** `valid_for` (seconds) or `valid_until` becomes 62. `expire_due_quotes` marks Expired every Quoted/Countered row whose 62 has passed (source `engine`, nothing on the wire). `_expire_quotes` is the timer: started by `start`, stopped by `stop`, woken by `_wake_expiry`, sleeping at most a minute.

**Events** are `<origin> <what>`: `rfq`, `rfq quoted`, `rfq requoted`, `rfq countered`, `rfq hit`, `rfq passed`, `rfq rejected`, `rfq canceled`, `rfq expired`, `rfq status`, `rfq response`, the same with `quote`, plus `sent rfq` and `sent quote`.

**RFQ requests** (35=AH, FIX 4.3+) are kept on `fix_rfq_requests` (`RFQ_REQUEST_COLS`), one row per RFQReqID(644):
- The market side sends them (`send_rfq_request`: instruments by `parse_symbols`, 263 = 0 snapshot or 1 subscribe; `unsubscribe_rfq_request`: the same 644 and instruments with 263=2), and the client side receives them (`_handle_rfq_request`: an unsubscribe closes the row its 644 names).
- The factory's `rfq_request` sends one 146 instance per instrument (303/537 in each) and then 263. `rfq_request_columns` keeps the instruments as `symbols` (`; `-joined).
- Nothing answers a request. A QuoteRequest carrying 644 (consumed, `fix_rfqs.rfq_req_id`) is counted on the request's row by `_link_rfq_request` — the row we sent for a received RFQ, the row we received for a sent one — in `quote_requests` and `last_quote_req_id`.
- Events: `rfq request`, `rfq request unsubscribed`, `sent rfq request`. IDs `RR`; template scopes `rfq_request` and `unsubscribe`.

**IDs** are `RQ`/`QT`/`QR`.

**Templates:** the scopes are `rfq`, `quote`, `new_quote`, `quote_reject`, `hit`, `counter` and `pass`; `cancel_quote` shares `cancel`.

**Not scripted yet:** the ops are in `UNSCRIPTED` until the macro verbs ship.

**Tests:** `tests/test_rfqs.py`; `TestRfqBlotters` covers the panes.

## Message Replay: `replay.py`

A job (`fix_replay_jobs`, `REPLAY_JOB_COLS`/`REPLAY_CONFIG_COLS` in engine.py) is a log file read once by `load_replay` (in a thread — a day's log; a path, or `example:<name>` naming a file in `replay_examples/`, resolved by `resolve_path`, which pins the name inside the folder): `parse_log_file` takes the four line shapes (prefix + message, ISO prefix, raw SOH, raw pipe), skips `#` lines and drops `ADMIN_MSG_TYPES`, and `summarize` puts the CompID pairs (first-appearance order), types with counts and dictionary names (by the first BeginString), and time span on the row as JSON `summary` with readable `pairs`/`first_time`/`last_time`. The two reqreps `replay_directions`/`replay_types` read it with `json_each` for the Start and Configure dialogs; `msg_filter` starts as every type, ticked. `configure_replay` validates (session known, numbers, window as `HH:MM[:SS]`, types in the file) and recomputes `default_direction` (`_default_direction`: the only pair, else the one pair whose sender is the session's SenderCompID — never the target's, which would play the counterparty out of our session). **The direction is asked at every Start** (`start_replay(job_id, direction)`; blank falls back to the default, and a two-way file with no default is refused) — the venue's side into an acceptor is a supported use. `select_messages` narrows by direction key (`SENDER→TARGET`, `direction_key`; blank for header-less lines, which form one blank pair), types and the log's time of day (unstamped messages pass), and `begin_replay` records the choice and count.

On the wire a replayed message is the session's: `prepare_for_send` drops `SESSION_TAGS` (8/9/10/34/49/56/52 — sendprep stamps them) and `DROPPED_TAGS` (43/122/369, the log's sequence space), restamps `RESTAMPED_BODY_TAGS` (60) with the send time at the session's precision, keeps every other header/trailer tag as a field (routing tags 50/57/115/116/128/129/142–145 ride in the dictionary's header order) and carries the body as ordered pairs on `extra`, so repeating groups survive (`fields` is a dict). The pre-0.54 bug was `sendprep` preferring a header tag already in `fields`, so a production log went out with its own CompIDs and MsgSeqNum. Pacing: the prefix, else 52, else 60; `ReplayTask.delay_before` divides the gap by `speed` (0 = none) and caps it at `max_gap` (0 = no cap); a pause landing during the sleep is honoured before the send. Progress writes go through the writer (`update_replay`), so the pane is live; `_replay_tasks` is stopped by `FixEngine.stop`, and `check_archive` refuses a playing job. The pane is a declarative `mkio-table` over `replay_jobs` (which LEFT JOINs `fix_session_state` for `session_status`, orders_query's way) with Load…/Configure…/Start… dialogs and one-click Pause/Resume/Stop/Delete, every op a `fix_cmd` command; `TestReplayControl` pins the Load dialog's example list to the files. `tests/test_replay_e2e.py` plays `two-sided-day` both ways over a linked stub pair.
