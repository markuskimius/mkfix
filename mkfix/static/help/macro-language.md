# Macro Language

A macro is a macro that acts on orders as things happen to them: accept this, fill that a second later, refuse the second replace, dispute a fill that is through its limit. Each order gets its own copy of the macro, so the same few lines handle one order or a thousand.

There are two kinds, kept apart throughout — two menus, two editors, two sets of runs:

| | A **market macro** | A **client macro** |
|---|---|---|
| acts on | the orders you receive | the orders you send |
| with | `accept`, `reject`, `fill`, `unsol cxl`, `restate`, `correct`, `bust`, `renotify` | `new`, `replace`, `cancel`, `dk` |
| in blocks | `on order` | `run`, `on sent order` |
| is started with | **Arm…** — it waits for orders to match | **Run…** — it sends at once, on the session you choose |
| at once | once per session it is armed for | as many runs as you like |
| lives in | **Market** menu: Market Macros, Macro Runs, Macro Orders, Macro Log | **Client** menu: Client Macros, Macro Runs, Macro Orders, Macro Log |

A macro is one or the other: a block of the other kind is a problem the editor underlines. To play both sides of an order, write one of each — the loopback tour in [Macro Examples](macro-examples.md) does.

The rest of the language — waiting, `when`, events, control, expressions — is the same on both sides. The editor checks as you type, completes words with Ctrl+Space, explains a word when the mouse rests on it — an action's terms, the FIX code behind `buy` or `day`, the problem on an underlined line — and opens this reference at the word under the cursor with F1. The **Macro Orders** pane of each side shows every order's macro, the line it is on and what it is waiting for.

## A first macro

```macro
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

Every IBM or MSFT order that arrives is accepted after 200 ms and then filled in clips of up to 100 about once a second. If the counterparty asks to cancel meanwhile, the cancel is accepted and the macro stops.

## The shape of a macro

- Its name is the one it is saved under: the text does not carry it, so a copy renames freely. A name is letters, digits, spaces and `. _ - :`; **Export** writes it as `NAME.macro` with each colon as a dot, and **Import** names the macro after the file. A recording is offered as `Market 2026-09-22 14:30:15` (its side, then the local date and time you pressed Stop).
- `seed N` makes a run repeat exactly: the same `RANDOM()` numbers and the same timing jitter.
- `on error continue` turns a refused action into an `error` event instead of failing the order's macro.
- Then one or more blocks. Lines inside a block are indented, with spaces; deeper blocks indent further. `#` starts a comment.
- One statement per line. An expression cannot run onto the next line.

## Blocks

| Block | Side | The order is | The macro may |
|---|---|---|---|
| `on order where EXPR` | market | one you received | `accept`, `reject`, `fill`, `unsol cxl`, `restate`, `correct`, `bust`, `renotify` |
| `run` or `run on SESSION` | client | one the macro sends with `new` | `new`, `replace`, `cancel`, `dk` |
| `on sent order where EXPR` | client | one sent some other way — by hand, or by Message Replay | `replace`, `cancel`, `dk` |

`where` is optional. In it the order's columns are names by themselves — `symbol == 'IBM' and order_qty >= 1000` — and `order.symbol` works too.

## Market macros

The macro at the top of this page is one: `on order` blocks, armed with **Arm…**, which asks for an optional session (only its orders are taken), a speed and a seed.

- A received order is offered to the armed runs in their **Priority** — first armed, first offered — and to each macro's blocks from the top. The first block whose `where` is true takes it, and an order has one macro. **Move Up** and **Move Down** in **Market Macro Runs** change a run's place in line: put the narrow macro (`where symbol == 'IBM'`) ahead of the catch-all.
- Orders that were there before a macro was armed are left alone.
- Arm as many macros as you like, and the same one for several sessions. Only a second arming of the same macro for the same sessions is refused — it could never be given an order.
- A market run stays armed until you stop it, or the server restarts: a restart stops every macro, and none is played again by itself.

## Client macros

```macro
run
    let clients = ['ACME', 'GLOBEX']
    repeat 20 at 5/s with sym = ['IBM', 'MSFT']
        new symbol: sym, side: buy, qty: 100 * (n + 1), price: 25.00, client: clients[n % 2]
        expect ack within 2s else fail 'order ${n} was not acknowledged'
        wait filled or timeout 5s
        if order.leaves_qty > 0 and order.pending_action == ''
            cancel
            expect canceled or filled within 2s
        pass
```

- `run` leaves the session to **Run…**, which asks for it — so one macro runs on any session, and on several at once. `run on SESSION` names it in the macro: Run… then opens on that session, and the one you choose there wins.
- A `run` block starts as soon as the macro is run, so the session must be logged on: Run… refuses one that is not active. `new` sends the order; from then on the macro is that order's, and one macro sends one order.
- Put `new` inside a `repeat` and every pass is a macro of its own with an order of its own. `at 5/s` or `every 200ms` paces the passes, which do not wait for one another. Lines outside that `repeat` run once, before any order exists: they may set names, wait and log, but not act.
- `replace` and `cancel` name the order's current ClOrdID by themselves. A `replace` changes only the terms it gives; the rest keep the order's last accepted values, as the Replace dialog would.
- `order.pending_action` is the request still unanswered — never stack one request on another unless that is what you are testing.
- **Run it as often as you like, while it is running.** Every Run… is a run of its own, with its own orders, seed and verdict, listed in **Client Macro Runs**; **Stop all** in the editor stops every live run of the open macro, **Stop** in Client Macro Runs stops one. Editing a macro does not disturb its live runs: each keeps the version it started with.
- A run that only sends ends by itself when its last macro does.
- `on sent order` is the other client block: it waits, like a market macro, for orders sent some other way — by hand from Sent Orders, or by Message Replay — and minds them. An order a `run` macro sent belongs to that macro and is never offered. The session chosen at Run…, if any, is the only one it watches; such a run stays live until stopped, and has a Priority among the client runs that wait.

## Actions

An action is a blotter button. Its terms are the dialog's fields, written `name: value`, separated by commas. A value is an expression; quoted text may hold `${…}` placeholders.

| Action | Does | Terms |
|---|---|---|
| `accept` | Accepts whatever is pending: the new order, or a cancel or replace request | `text`, `extra` |
| `reject` | Rejects whatever is pending | `text`, `extra` |
| `fill` | Fills the order, in part or in full | `qty`, `price`, `text`, `extra` |
| `unsol cxl` | Cancels the order though nobody asked | `text`, `extra` |
| `restate` | Changes the order's terms unasked | `qty`, `price`, `reason`, `text`, `extra` |
| `correct` | Corrects a trade you sent | `qty`, `price`, `text`, `extra` |
| `bust` | Busts a trade you sent | `text`, `extra` |
| `renotify` | Sends a disputed trade's report again under a new ExecID | `text`, `extra` |
| `new` | Sends a new order | `symbol`, `side`, `qty`, `type`, `price`, `tif`, `expire`, `client`, `handl_inst`, `text`, `extra` |
| `replace` | Asks to replace the order; terms left out keep their last accepted value | `qty`, `type`, `price`, `tif`, `expire`, `client`, `handl_inst`, `text`, `extra` |
| `cancel` | Asks to cancel the order | `text`, `extra` |
| `dk` | Disputes a received trade (DontKnowTrade) | `reason`, `text`, `extra` |

- `extra` is the dialogs' Extra Tags: `extra: '9001=venue-A'` adds a tag, `extra: '60='` removes one, and naming a computed tag (`extra: '10=000'`) overrides it.
- `using 'NAME'` takes the terms from a saved template of that action; terms written on the line override the template's.
- Fixed choices may be a bare word, a quoted name, or the FIX code: `side: buy`, `reason: 'Price exceeds limit'`, `tif: '3'`. The words are listed by completion after the colon.

### Which trade

`correct`, `bust`, `renotify` and `dk` act on a trade, so they say which:

```macro
on order
    accept
    fill qty: 100, price: order.price
    bust last trade, text: 'erroneous'
    bust first trade
    bust trade where trade.last_price > 150
```

`trade where` takes the first of the order's trades the condition is true for, with `trade` the candidate. Busted trades are not offered. Under a `when` whose event carries a trade — `when dk`, `when fill` — the trade is already in hand and no target is needed.

## Waiting

| Statement | Does |
|---|---|
| `after DURATION` | Waits. `after 1s ± 250ms` adds random jitter either way; with a `seed` it repeats exactly |
| `wait EVENT or EVENT where EXPR or timeout DURATION` | Waits for one of the events; carries on either way. After a timeout `event.kind` is `'timeout'` |
| `expect EVENT within DURATION else fail 'WHY'` | Waits for an event and fails the order's macro if it does not come in time |

Durations are numbers of seconds with a unit — `250ms`, `2s`, `1.5m`, `1h` — and are ordinary numbers, so `after 100ms * n` and `if elapsed > 30s` work.

An event that arrived before the `wait` started is not missed: a `wait` looks at everything since the last one.

## Reacting: when

```macro
on order
    accept
    when replace and order.pending_qty < order.cum_qty
        reject text: 'below executed quantity'
    when replace
        accept
    while order.leaves_qty > 0
        after 2s
        fill qty: MIN(100, order.leaves_qty), price: order.price
```

- A `when` is live from its line to the end of the block it is written in, and runs beside the lines below it — a `when` that waits does not hold up a fill loop.
- For one event the first `when` that matches wins, so write the special cases first.
- A macro with a live `when` at the top of its block stays alive after its last plain line has run, until `stop`, a verdict, or the end of the run. That is how a desk refuses a too-late cancel after the order has filled.

## Events

| Event | Side | Happens when |
|---|---|---|
| `cancel` | received | the counterparty asks to cancel the order |
| `replace` | received | the counterparty asks to replace the order |
| `dk` | received | the counterparty disputes a trade you sent |
| `ack` | sent | the order is accepted |
| `pending` | sent | a request was received but not yet decided |
| `fill` | sent | a fill, partial or complete |
| `filled` | sent | the fill that completed the order |
| `replaced` | sent | a replace request was accepted |
| `canceled` | sent | the order was canceled, asked for or not |
| `rejected` | sent | the order was rejected |
| `cancel rejected` | sent | a cancel or replace request was refused; see `event.response_to` and `event.reason` |
| `restated` | sent | the counterparty changed the order's terms unasked |
| `expired` | sent | the order expired |
| `done for day` | sent | the order is done for the day |
| `corrected` | sent | a trade you received was corrected |
| `busted` | sent | a trade you received was busted |
| `er` | sent | any ExecutionReport, named or not: test `event.tag['150']` |
| `message` | both | any application message about the order |
| `manual` | both | someone acted on the order by hand; `event.op` names the action |
| `session down` | both | the order's session lost its connection |
| `session up` | both | the order's session is connected again |
| `error` | both | an action of this macro was refused; `event.text` says why |

A macro never hears its own actions.

## Control

| Statement | Does |
|---|---|
| `if EXPR` … `else if EXPR` … `else` | Chooses |
| `while EXPR` | Repeats while true. A loop that goes round 1000 times without once waiting fails the macro |
| `repeat N at 10/s with sym = ['IBM', 'MSFT']` | Runs the block N times; `at` or `every 250ms` paces the passes; `with` hands each pass the next value of a list; `n` counts from 0 |
| `let NAME = EXPR` | Names a value. Setting a name again changes it, and a macro has one set of names, so it can count |
| `log EXPR` | Writes to the run's log |
| `stop` | Ends this order's macro with no verdict |
| `pass 'WHY'` | Ends it as passed |
| `fail 'WHY'` | Ends it as failed |

## What an expression can see

Expressions are mkio's expression language: `and or not`, `in`, `== != < <= > >=`, arithmetic, `[...]` lists, and functions such as `MIN`, `ROUND`, `COUNT(list, x -> …)`, `ANY`, `IF`. Completion lists them all.

| Name | Is |
|---|---|
| `order` | the order's row, as the blotter shows it: `order.leaves_qty`, `order.pending_action`, `order.pending_qty`, `order.entered_qty` … |
| `trade` | the trade in hand: the event's, or the one a trade target chose |
| `trades` | every trade of the order, oldest first |
| `history` | every recorded version of the order's row, oldest first |
| `event` | what just happened: `event.kind`, `event.request`, `event.tag['150']`, `event.text`, `event.op` … |
| `event.prev` | the order as it stood before the event. A re-notified fill is recorded as a new trade; `event.prev.cum_qty < order.cum_qty` is how a macro tells a fill that moved CumQty from one that restated it |
| `elapsed` | seconds since this order's macro started |
| `since` | seconds since the last event this macro waited for or reacted to |
| `n` | the pass of the innermost `repeat`, from 0 |

A misspelt column is an error when you save, not a surprise when you run: `order.leave_qty` is underlined.

Two functions exist only in macros:

- `TICK(price, size)` rounds a price to a tick without floating-point noise: `TICK(order.price + 0.05, 0.01)`.
- `RANDOM()` is a number from 0 up to 1 from the run's seeded generator.

## When things go wrong

- A failing expression, a refused action or a trade target that matches nothing fails the order's macro and names the line. With `on error continue` the last two raise an `error` event instead.
- More than 1000 actions on one order fails its macro. A desk that re-notifies every dispute, facing a client that disputes every report, would otherwise go round for ever — a re-notification is a new trade.
- A macro can do more than the blotter offers: the buttons hide Fill on a rejected order, the engine does not refuse it. That is deliberate — a test venue misbehaves on purpose — so check your `if`s.
- Macros live in the server's memory, so a restart stops them all. Every run that was live is marked `interrupted`, its orders' macros are gone, and nothing is played again — not a macro that sends orders, and not one that only waits for them: ▶ is dark after a restart, the status bar says `■ slow-fill interrupted` for a minute, and what should be playing is yours to play again.
- A macro can fail in its first instant — `new` refused because the session had just dropped. The run then ends at once: the editor says so in its status line, and that side's **Log** and **Macros** panes say why. **Stop** on a run that is already over changes nothing.

## From the order blotters

Sent Orders and Received Orders carry the macro controls of their side — client on Sent Orders, market on Received Orders — so a macro can be played, paused, stopped and recorded without leaving the blotter. They are four symbols at the end of the toolbar, lit like a tape deck's — ▶ while a macro of that side is playing, ⏸ while one is paused, ● red while it records — and each says what it does, and how many runs it is about, when the mouse rests on it.

- **▶** (play) lists, as tick boxes, the runs you have paused — to resume — and then the side's macros that check clean. Tick one or several; nothing is ticked when it opens. Ticking a macro brings up the session, speed and seed, which are for every macro ticked, and the session is asked for only when one of them "asks for a session" (its `run` names none). What you tick is played whole or not at all: if one of them cannot start — its session is down, it is armed there already — none does, and the dialog says which. A single macro plays exactly as Run… or Arm… in the editor would.
- **⏸** (pause) and **■** (stop) show the side's live runs as a list of tick boxes — macro, session, state and orders, scrolling when there are many. It opens with every run ticked: untick the ones to leave alone, or clear them all with the first row, "Every playing run" / "Every live run", and tick the few you mean. A paused run parks each order's macro before its next line; a stopped run is over and its orders are yours again.
- **●** (record) starts a recording of that side (see below). While it records the dot is red and pulses — its tooltip and the status bar count what you have done — and pressing it asks for a name, saves the macro and opens it in the editor. If the name is taken the recording goes on, and if nothing was recorded nothing is saved.
- Playing or pausing one run of several, moving a run up or down, and detaching an order are in **Macro Runs** and **Macro Orders**.

The **status bar** says what the macros are doing, at the right, each item a link to the pane it is about: `● REC client · 3 actions` while a recording runs, `▶ market slow-fill · 3 orders` while macros play (`▶ client 2 runs · 14 orders` for several), `⏸ market slow-fill paused`, and for a minute after a run ends `■ replace-chase passed · 3 of 3` or `■ slow-fill stopped`. A failed run — `■ suite failed · 1 of 3 failed` — stays until something else happens on its side, ten minutes at most.

## Recording

You do not have to start from an empty page: **Record…** — in an editor, or on Sent Orders and Received Orders — watches you work orders by hand and writes the macro that would have done the same.

- In **Market Macros**, Record… follows the orders that arrive while it is on and what you do to them from Received Orders and Sent Trades — Accept, Reject, Fill, Unsol Cxl, Restate, Correct, Bust, Re-notify. In **Client Macros** it follows the orders you send from Sent Orders and your Replace, Cancel and DK. It asks for one session or every session; the button counts your actions while it records. What you do on the trade blotters counts too: a DK from Received Trades belongs to a client recording, a Correct, Bust or Re-notify from Sent Trades to a market one — as long as the trade is of an order the recording follows.
- **Stop recording** asks for a name and opens the macro. Each order is a block, and what happened decides when each thing is done, not the clock:
  - An action you took after the counterparty did something is triggered by it. Everything heard since your last action is waited for, in the order it came, and the action runs the moment the last of it arrives: `expect ack within 5s`, `expect fill within 6s`, `replace qty: 200`. A market order's arrival is such an event, so your first action on it has no delay.
  - A client macro `expect`s, within a generous bound — five seconds, or three times what it took — so a venue that never answers fails the run instead of hanging it. A market macro `wait`s, as long as the client takes. A client recording ends each order with `pass`.
  - Only where nothing came between two of your actions is there nothing but time to go by: `after 2s`, then the action — accept, then a fill two seconds later.
  - On the market side, a request you answered the same way every time is written as the rule it was — `when cancel` / `accept`, beside the main flow — and an accepted cancel with nothing done after it ends with `stop`, so a cancel that comes sooner next time is not followed by the fills still to come. A request answered two ways — the first replace accepted, the second refused — is no rule, and stays a `wait` in the order it happened.
  - **Keep my delays**, a tick box where you stop, also writes the time you took to answer: `after 500ms` between the wait and the action. Leave it off for a macro that reacts at once; tick it for a venue with a person's pauses.
- Market orders worked the same way share one block (`where symbol in [...]`); a symbol worked two ways is told apart by quantity, the narrower block first. A client recording is one `run` block an order, each after the pause you left, and names no session — Run… asks.
- Trades are named as a macro names them: `last trade`, `first trade`, or `trade where` with the terms the trade had.
- The result is a first draft, literal about what happened. It checks clean and runs as it stands; then loosen it — a quantity into `order.leaves_qty / 2`, a run of `expect fill` lines into `wait filled`, the `where` into the orders you mean. It expects exactly the events it heard, in their order: a venue that answers differently — three fills where there were two — fails or stalls it, which is what a regression test should do and not what a general-purpose macro should.
- Not recorded: orders already under way when recording began, orders a macro owns, and an arrival you never touched. The recording lives in the server, so it goes on if you close the editor, and ends with nothing written if the server stops.

## Running

- **Arm…** (Market Macros) and **Run…** (Client Macros) ask for a session, a speed (2 runs the macro's waits twice as fast — mind that real answers do not get faster) and a seed (blank: the macro's `seed`, or a random one, shown in Macro Runs so a run can be repeated).
- Any number of runs may be live at once, on either side. The server stops taking orders into macros at 20,000 live macros, and a single run at 10,000 orders; the run's log says so.
- **Macro Runs** lists a side's runs. **Pause** parks every macro of a run before its next line; **Stop** ends it; **Move Up**/**Move Down** change its Priority. **Macro Orders** lists the orders' macros — **Detach** gives one order back to you — and selecting a row moves the editor to its line. **Log** holds what the macros `log`, and why one failed.
- **History** in an editor lists every Save of the open macro, newest first, with the runs that used each version. Click a version to look at it in the editor, read-only, against the macro as it is saved now: lines only in that version are marked red, and a green ▸ in the margin shows where the saved macro has lines that version lacks. **Restore** puts the version back in the editor as an unsaved edit — Save keeps it as a new version, so nothing is lost by restoring — and **Back to the macro** returns to your text, unsaved edits included. Deleting a macro deletes its history.
- A macro cannot be deleted while it has a live run. Names are shared by the two sides: a client and a market macro cannot have the same one.
- The client examples run over two loopback sessions, `LOOP-CLI` facing `LOOP-MKT`: **Help › Macro Examples › Set up loopback sessions** creates and starts them, and **Run the loopback tour** also arms the venue and runs the client.
- Orders a macro has taken carry its name and run in the **Macro** column of the order blotters.
- An archive that would take orders, trades or run rows is refused while a macro is armed or running.
