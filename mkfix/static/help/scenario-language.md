# Scenario Language

A scenario is a script that acts on orders as things happen to them: accept this, fill that a second later, refuse the second replace, dispute a fill that is through its limit. Each order gets its own copy of the script, so the same few lines handle one order or a thousand.

A script can play either side, or both: answer the orders you receive, send orders of its own and manage them, or mind the orders you send by hand.

Write scripts in the **Scenarios** pane (Trading menu). The editor checks as you type, completes words with Ctrl+Space, and explains the word under the cursor with F1. **Arm** a saved script to let it take orders — **Run**, when it sends its own; the **Scenario Runs** pane shows every order's script, the line it is on and what it is waiting for.

## A first script

```scenario
scenario slow-fill

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

Every IBM or MSFT order that arrives is accepted after 200 ms and then filled in clips of up to 100 about once a second. If the counterparty asks to cancel meanwhile, the cancel is accepted and the script stops.

## The shape of a script

- The first line names it: `scenario NAME`. The name is the one it is saved under.
- `seed N` makes a run repeat exactly: the same `RANDOM()` numbers and the same timing jitter.
- `on error continue` turns a refused action into an `error` event instead of failing the order's script.
- Then one or more blocks. Lines inside a block are indented, with spaces; deeper blocks indent further. `#` starts a comment.
- One statement per line. An expression cannot run onto the next line.

## Blocks

| Block | The order is | The script may |
|---|---|---|
| `on order where EXPR` | one you received | `accept`, `reject`, `fill`, `unsol cxl`, `restate`, `correct`, `bust`, `renotify` |
| `run on SESSION` | one the script sends with `new` | `new`, `replace`, `cancel`, `dk` |
| `on sent order where EXPR` | one sent some other way — by hand, or by Message Replay | `replace`, `cancel`, `dk` |

`where` is optional. In it the order's columns are names by themselves — `symbol == 'IBM' and order_qty >= 1000` — and `order.symbol` works too.

A received order is offered to the armed scenarios in the order they were armed, and to each script's blocks from the top. The first block whose `where` is true takes it, and an order has one script. Orders that were there before a scenario was armed are left alone. An order you send by hand is offered to the `on sent order` blocks the same way; one a `run on` script sent belongs to that script.

## Sending orders

```scenario
scenario burst

run on LOOP-CLI
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

- A `run on` block starts as soon as the script is run, so its session must be logged on: **Run…** refuses a session that is not active. `new` sends the order; from then on the script is that order's, and one script sends one order.
- Put `new` inside a `repeat` and every pass is a script of its own with an order of its own. `at 5/s` or `every 200ms` paces the passes, which do not wait for one another. Lines outside that `repeat` run once, before any order exists: they may set names, wait and log, but not act.
- `replace` and `cancel` name the order's current ClOrdID by themselves. A `replace` changes only the terms it gives; the rest keep the order's last accepted values, as the Replace dialog would.
- `order.pending_action` is the request still unanswered — never stack one request on another unless that is what you are testing.
- A run that only sends ends by itself when its last script does. One that also waits for orders stays armed until you stop it.

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

```scenario
scenario which-trade
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
| `expect EVENT within DURATION else fail 'WHY'` | Waits for an event and fails the order's script if it does not come in time |

Durations are numbers of seconds with a unit — `250ms`, `2s`, `1.5m`, `1h` — and are ordinary numbers, so `after 100ms * n` and `if elapsed > 30s` work.

An event that arrived before the `wait` started is not missed: a `wait` looks at everything since the last one.

## Reacting: when

```scenario
scenario desk
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
- A script with a live `when` at the top of its block stays alive after its last plain line has run, until `stop`, a verdict, or the end of the run. That is how a desk refuses a too-late cancel after the order has filled.

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
| `error` | both | an action of this script was refused; `event.text` says why |

A script never hears its own actions.

## Control

| Statement | Does |
|---|---|
| `if EXPR` … `else if EXPR` … `else` | Chooses |
| `while EXPR` | Repeats while true. A loop that goes round 1000 times without once waiting fails the script |
| `repeat N at 10/s with sym = ['IBM', 'MSFT']` | Runs the block N times; `at` or `every 250ms` paces the passes; `with` hands each pass the next value of a list; `n` counts from 0 |
| `let NAME = EXPR` | Names a value. Setting a name again changes it, and a script has one set of names, so it can count |
| `log EXPR` | Writes to the run's log |
| `stop` | Ends this order's script with no verdict |
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
| `event.prev` | the order as it stood before the event. A re-notified fill is recorded as a new trade; `event.prev.cum_qty < order.cum_qty` is how a script tells a fill that moved CumQty from one that restated it |
| `elapsed` | seconds since this order's script started |
| `since` | seconds since the last event this script waited for or reacted to |
| `n` | the pass of the innermost `repeat`, from 0 |

A misspelt column is an error when you save, not a surprise when you run: `order.leave_qty` is underlined.

Two functions exist only in scenarios:

- `TICK(price, size)` rounds a price to a tick without floating-point noise: `TICK(order.price + 0.05, 0.01)`.
- `RANDOM()` is a number from 0 up to 1 from the run's seeded generator.

## When things go wrong

- A failing expression, a refused action or a trade target that matches nothing fails the order's script and names the line. With `on error continue` the last two raise an `error` event instead.
- More than 1000 actions on one order fails its script. A desk that re-notifies every dispute, facing a client that disputes every report, would otherwise go round for ever — a re-notification is a new trade.
- A script can do more than the blotter offers: the buttons hide Fill on a rejected order, the engine does not refuse it. That is deliberate — a test venue misbehaves on purpose — so check your `if`s.
- Scripts live in the server's memory. After a restart the run is marked `interrupted` and its orders' scripts are gone. A scenario that only waits for orders is armed again as a new run; one that sends orders is not run again — a restart must not send anything.
- A script can fail in its first instant — `new` refused because the session had just dropped. The run then ends at once: the Scenarios pane says so in its status line, and **Scenario Log** and **Scenario Scripts** say why. **Stop** on a run that is already over changes nothing.

## Running

- **Arm…** (**Run…** for a script that sends) asks for an optional session (only its orders are taken by the `on` blocks), a speed (2 runs the script's waits twice as fast — mind that real answers do not get faster) and a seed.
- The sending examples run over two loopback sessions, `LOOP-CLI` facing `LOOP-MKT`: **Help › Scenario Examples › Set up loopback sessions** creates and starts them.
- **Scenario Runs** lists the runs with their orders' scripts. **Pause** parks every script before its next line; **Stop** ends the run; **Detach** gives one order back to you.
- Orders a script has taken carry its name and run in the **Scenario** column of the order blotters.
- An archive that would take orders, trades or run rows is refused while a scenario is armed.
