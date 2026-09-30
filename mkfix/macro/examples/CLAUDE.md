# mkfix/macro/examples — the bundled examples and the loopback tour

The language is `../CLAUDE.md`. Everything reads this directory by `*.macro`, and the wheel excludes `**/CLAUDE.md`, so this file is seen by neither.

## The examples

Shipped in the wheel, read-only, opened as copies. Each opens with `# Title`, a bare `#`, then `Shows` / `Needs` / `Watch` / `Outcome` lines the Help page will be built from. They are the acceptance set: `tests/test_macro.py` requires each to check clean and the set to use every market-side verb, event, statement, trade target, context name and both macro functions, so a new word fails until an example shows it. Forty-one examples (three of them lists', two multileg, below; three of them instruments': `futures-roll` naming declared futures, `derivatives-desk` narrowing by the instrument, the end-to-end `option-quotes` streaming two series of one symbol; four of them the families': `ioi-desk`, `allocation-desk` sending from the market side, `ioi-taker`, `allocation-check` answering on the client side; three working together: `hedge-pairs`, `one-at-a-time`, `fill-and-allocate`; two end-to-end: `end-to-end-order`, `told-to-reject`); `tests/test_macro_sending.py::test_the_whole_language_is_covered` holds the set to every verb, block kind, statement shape and context name. Every sending example runs on `LOOP-CLI` (`store.LOOPBACK`), which `setup_loopback` creates.

RFQs (0.73): `rfq-desk`, `rfq-taker`, `quote-stream`, `quote-taker`, `rfq-subscriber`, `rfq-responder`, `end-to-end-rfq`. Their rules (`when`) come before the quote, since an answer can arrive inside the send.

Lists (0.78): `list-trader` (one NewOrderList, an order's own block, `add order`, the requests), `drip-basket` (order by order, paced by `repeat`), `list-desk` (the market side). Not in the tour: the venue's `on order` would take the lists' orders too. The coverage tests walk the orders' own blocks (`nodes.member_macros`) as well as the block's.

Multileg (0.79): `calendar-spread` (a declared strategy, `replace` with `leg` lines, `event.leg`, `legs`) and `spread-desk` (`report_legs`, `fill leg:`). Not in the tour either.

## The loopback pair and the tour

`setup_loopback(port=9880)` creates `LOOP-MKT` (acceptor) and `LOOP-CLI` (initiator on 127.0.0.1) on FIX 4.4 (`LOOPBACK_VERSION`: RFQ Hit/Counter/Pass need it) if missing and starts them, acceptor first; an existing pair keeps its port, and one still on the pre-0.74 default FIX 4.2 is moved to 4.4 while stopped (`upgraded`). The tour (`TOUR_ARMED`/`TOUR_RUN`) plays the RFQ examples too; a second `new quote` on a symbol takes the row, and `Instance.bind` detaches the macro that held it. Found with it, running the tour over real TCP at 20×: the engine's automatic reject of a request naming no order now carries CxlRejReason(102)=1, so the requester keeps its own status instead of taking the reject's 39=8.

**The tour.** `TOUR` = `loopback-venue` (market) + `loopback-client` (client); `run_tour` (`fix_cmd run_loopback_tour`, the Examples page's button) sets up the sessions, waits for `LOOP-CLI` to log on, saves the two examples unless macros of those names exist, arms the venue on `LOOP-MKT` unless already armed, and runs the client on `LOOP-CLI`. Examples name no session (`test_macro_ui` enforces it); a client example's `Needs:` says `LOOP-CLI`.

The loopback tour (`run_tour`) plays the families since 0.65: `TOUR_ARMED` (the venue on `LOOP-MKT`; `ioi-taker`, `allocation-check` on the client side, any session) are armed unless already armed, then `TOUR_RUN` (`loopback-client` on `LOOP-CLI`; `ioi-desk`, `allocation-desk` on `LOOP-MKT`) are run; the result keeps `venue_run`/`client_run` and adds `runs` by name. Since 0.75.1 it plays the instrument examples too: `derivatives-desk` is armed first on `LOOP-MKT` and `futures-roll` run on `LOOP-CLI`. The venue's `on order` takes every order on its session, so `TOUR_AHEAD` keeps the desk offered orders before it: `_put_ahead` moves the desk up the line past a venue an earlier tour left armed, and renumbers. The client run's `on sent order` block minds the orders `ioi-taker` sends.
