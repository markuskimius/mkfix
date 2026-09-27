# User Guide

mkfix is a FIX counterparty you drive by hand or by script. It plays either end of a FIX session — the client that sends orders, the market that answers them — and both at once when it talks to itself. Everything it sends and receives is kept, shown live, and can be looked at field by field.

This page is the tour of the whole application. Two subjects have pages of their own: [Macro Language](macro-language.md), with its [Macro Examples](macro-examples.md), and [Replaying a Log](replaying-a-log.md).

## The first five minutes

The quickest way to see mkfix work is to have it talk to itself.

1. Start the server with `mkfix` and open the address it prints, `http://localhost:8080/` unless you chose another port.
2. Open [Macro Examples](macro-examples.md) — **Help › Macro Language**, then **Macro Examples** in the list of pages — and press **Set up loopback sessions**. Two sessions appear in the Sessions blotter and turn `ACTIVE`: `LOOP-CLI`, the client, and `LOOP-MKT`, the market it connects to.
3. On **Sent Orders** press **New**, choose the session `LOOP-CLI`, fill in a symbol, a quantity and a price, and press **Send Order**.
4. The order arrives on **Received Orders** with **New** in its Pending column. Select it and press **Accept**, then **Fill**.
5. Watch the other side: Sent Orders shows the order's status change, **Received Trades** shows the fill, and **Messages** shows every message in both directions. Click a message and **Detail** breaks it out tag by tag.

**Run the loopback tour**, beside the set-up button, does all of this with macros and adds IOIs, adverts and allocations.

To face a real counterparty instead, create a session of your own: see [Sessions](#sessions).

## Two sides

Every blotter belongs to one of two sides, and the menus are split the same way.

| | Client side | Market side |
|---|---|---|
| Menu | **Client** | **Market** |
| Orders | sends them: **Sent Orders** | receives them: **Received Orders** |
| Trades | receives them: **Received Trades** | sends them: **Sent Trades** |
| IOIs, adverts, allocations | receives them | sends them |
| Macros, under the **Macro** menu | **Client Macros**, **Client Macro Runs** | **Market Macros**, **Market Macro Runs** |

A session is not tied to a side. Whatever a session sends shows on the sent blotters and whatever it receives on the received ones, so one mkfix can be the client on one session and the market on another.

## The menus

| Menu | What it holds |
|---|---|
| **FIX** | Sessions, Messages, Detail, Replay Control |
| **Edit** | Undo and Redo of a session change, Copy, Select All |
| **Client** | the client side's blotters |
| **Market** | the market side's blotters |
| **Macro** | the macros of each side and the end-to-end ones, each with its runs |
| **Config** | Templates, Dictionaries |
| **Layout** | Save Layout, Restore Layout, Reset to Default |
| **Window** | tile, grid or cascade the windows, the list of open ones, and [sloppy focus](#sloppy-focus) |
| **Help** | this guide, the macro pages, the key lists, About |

A menu item brings its pane to the front, opening it if it was closed. Panes sit as tabs inside windows; drag a tab to move it and a window's edge to size it. The status bar at the bottom shows the connection on the left and, on the right, what the macros are doing and the version.

| Key | Does |
|---|---|
| Alt/Option+Shift+←/→ | moves the active tab left or right in its window; with [sloppy focus](#sloppy-focus) on, it moves the window |

## Sessions

**FIX › Sessions** lists every session with its status, sequence numbers and last error.

### Creating one

**New** asks for:

- **Session ID** — your name for it, shown in every blotter.
- **FIX Version** — FIX.4.0 through FIX.5.0SP2. A 5.0 session logs on as FIXT.1.1.
- **Dictionary** — blank for the standard one of that version, or a dictionary of your own; see [Dictionaries](#dictionaries).
- **SenderCompID** and **TargetCompID** — yours and the counterparty's.
- **Host** and **Port** — with a host the session is an *initiator* and connects to it; with the host left blank it is an *acceptor* and listens on the port. Several acceptors may share a port as long as their CompIDs differ.
- **Heartbeat** — the interval in seconds.
- **Reset on Logon** — whether each logon starts both sequence numbers at 1.
- **Client Tag(s)** — where this counterparty carries the client's name; see [The Client column](#the-client-column).
- **Timestamps** — the precision of outgoing timestamps. *Protocol standard* is seconds through FIX 4.1 and milliseconds from 4.2.
- **Logout Timeout** and **TestRequest before Logout** — how Stop ends the session; see below.

### Running one

| Status | Meaning |
|---|---|
| `DOWN` | stopped |
| `LISTENING` | an acceptor waiting for its counterparty to connect |
| `LOGON_SENT` | connected, waiting for the Logon to be answered |
| `ACTIVE` | logged on |
| `LOGOUT_SENT` | stopping, waiting for the counterparty's Logout |
| `ERROR` | gave up; the Error column says why |

**Start** and **Stop** run and end a session. **Edit**, **Delete**, **Reset Seq** and **Change Seq** are offered only while it is stopped.

- **Reset Seq** sets both sequence numbers to 1. Nothing sent before the reset is ever replayed to a later ResendRequest.
- **Change Seq** sets them to numbers you type, to provoke a gap or a sequence-too-low Logout.
- **Stop** logs out the way the specification asks: a TestRequest first, to know the counterparty has caught up, then the Logout, then a wait for theirs. The wait is bounded by the Logout Timeout, which at 0 is twice the heartbeat interval.

A session change can be undone: **Edit › Undo Session Change**. A deleted session cannot.

## Messages and Detail

**FIX › Messages** is every message sent (`TX`) and received (`RX`), newest at the bottom, streaming live. **Earlier** and **Later** page through the past and **Live** returns to the stream. Heartbeats are hidden when the pane opens; the Type column's filter brings them back.

Click a message and **FIX › Detail** shows it field by field, with tag names and enum values from the dictionary of the session that owns it. Header, body and trailer fold, repeating groups show as trees, and timestamps are shown in the time zone you pick. Drag a column divider to size it; double-click to fit.

Messages are stored exactly as they crossed the wire. The `|` you see between fields is only how the delimiter is drawn.

## Working with a table

Every blotter is the same kind of table.

- **Filter** — the ≡ in a column's header opens its filter: a checklist of the column's values, or bounds for a number or a time. Time columns offer Today, Last hour and Last 15 minutes. Active filters show as chips on the toolbar, where a chip's box switches it off without losing it.
- **Sort** — click a column's name, and again to reverse it. Shift-click a second column to sort by both.
- **Columns** — the picker at the right end of the header row lists every column the table has. Many are hidden to begin with.
- **Select** — click a row; Ctrl/Cmd-click and Shift-click select several. Most buttons act on every selected row.
- **Find** and **Copy** — see the keys below. Copied rows paste into a spreadsheet.

The order and trade blotters open on today's rows. Clear the *Today* chip to see older ones. Times are shown in your browser's time zone and stored in UTC.

| Key | Does |
|---|---|
| Ctrl/Cmd+C | copies the selected rows |
| Ctrl/Cmd+A | selects every row |
| Ctrl/Cmd+F | finds in the focused table |
| Ctrl/Cmd+G | next match; with Shift, the previous one |
| Escape | clears the selection, then closes find |

## Orders, from the client side

### Sent Orders

| Button | Sends | Offered when |
|---|---|---|
| **New** | NewOrderSingle | always |
| **Clone** | NewOrderSingle, the form filled from the selected order | one order is selected |
| **Replace** | OrderCancelReplaceRequest, the form filled from the terms last accepted | the order is working, or filled |
| **Cancel** | OrderCancelRequest | the order is working |
| **History** | nothing; see [History](#history) | one order is selected |
| **Macro…** | nothing; writes a macro, see [Macros](#macros) | any order is selected |

A Replace or Cancel waits on the row as **Pending**, with the request's ClOrdID and terms beside it, until the counterparty answers:

- an accepting ExecutionReport moves the order to the request's ClOrdID;
- an OrderCancelReject leaves the ClOrdID alone and says what was refused under **Rej Reason**.

Fills keep arriving while a request is pending.

### Received Trades

Each fill is a row. **DK** disputes one with a DontKnowTrade carrying a reason and an optional text. The row keeps the dispute in its DK columns until a correction or a bust answers it.

### The IDs on a row

- **ClOrdID** changes: it is the latest ID the counterparty accepted.
- **Order ID** never changes. mkfix mints it when the order is first sent or received, so it identifies the order across every replace.
- **Market Order ID** is the counterparty's own OrderID, as last reported.
- A trade's **Trade ID** never changes either; a correction or a bust gives the trade a new ExecID and keeps the old ones in its history.

Generated IDs say what they are: `RT` for a ClOrdID, `OR` for an Order ID, `EX` for an ExecID, `TR` for a Trade ID, `IO`, `AD` and `AL` for IOIs, adverts and allocations. Two characters follow, from your user name, so that two people testing against one counterparty can tell their orders apart.

## Orders, from the market side

### Received Orders

A new order, a cancel request and a replace request all arrive the same way: as **New**, **Cancel** or **Replace** in the row's **Pending** column, with the request's terms beside it.

| Button | Sends | Offered when |
|---|---|---|
| **Accept** | ExecutionReport New, Canceled or Replaced | something is pending |
| **Reject** | ExecutionReport Rejected, or OrderCancelReject for a request | something is pending |
| **Fill** | ExecutionReport with a fill | the order is working, or filled |
| **Unsol Cxl** | ExecutionReport Canceled that nobody asked for | the same |
| **Restate** | ExecutionReport Restated with new quantity and price | the same |
| **Clone** | the order, sent out again as your own | one order is selected |
| **Allocate** | AllocationInstruction for the order's fills | the session is active |
| **Macro…** | nothing; writes a macro, see [Macros](#macros) | any order is selected |

Things the buttons allow on purpose:

- An order can be filled while a request is pending, and filled again after it is full. An overfill is a test.
- An unsolicited cancel leaves a pending request where it is, so it can still be rejected as too late.

### Sent Trades

| Button | Sends |
|---|---|
| **Correct** | the trade again with a new quantity or price |
| **Bust** | the trade's cancellation |
| **Re-notify** | the trade's current report under a new ExecID, in answer to a DK |
| **Allocate** | AllocationInstruction for that one fill |

A busted trade is finished and shown muted. A DK from the counterparty marks the trade in its DK column.

mkfix words each message for the session's FIX version: a correction is ExecTransType 2 through FIX 4.2 and ExecType G from 4.3, and a tag the version does not define is left out.

## Dialogs

Every button that sends something opens a dialog, and they share these habits.

- A field's label names its tag, as in *Quantity (38)*, and a dropdown lists `code - name`.
- **Terms as tags**, at the foot, shows what you have entered as `tag=value` pairs before anything is sent.
- **Text (58)** starts empty in every dialog.
- **Extra Tags** takes anything the form has no field for; see the next section.
- **Template**, at the top, fills the form from terms saved earlier; **Save as template** keeps the terms you are sending. See [Templates](#templates).
- The **pin** in the title bar keeps the dialog open after it sends, with its terms, for a run of similar orders.
- A dialog never blocks the application. The title names the rows it was opened on, and it acts on those rows whatever you select afterwards.
- With several rows selected the dialog sends once for each.

| Key | Does |
|---|---|
| Enter | submits from a single-line field |
| Ctrl/Cmd+Enter | submits from anywhere in the dialog |
| Escape | cancels, unless the dialog is pinned |

Buttons that act on an existing order or trade are greyed while its session is not `ACTIVE`.

## Extra tags

Extra Tags is `tag=value` pairs separated by `|`, for example `528=A|382=2|375=BRK1|375=BRK2`.

| You write | What goes out |
|---|---|
| a tag the message does not carry | the tag, added: header tags in the header, trailer tags before the checksum, the rest after the body |
| a tag the message already carries | your value in its place, even for 34, 52, 9 or 10 |
| a tag with no value, `21=` | the message without that tag |
| the same tag several times | every one, in your order: a repeating group |

The last three are how you send a deliberately wrong message: a stale sequence number, a missing required field, a bad checksum.

On the market side, custom tags that arrive on an order or a request are kept on the row and offered back in the Accept, Reject and Fill dialogs, to confirm or edit before they are echoed.

## IOIs, adverts and allocations

The market side sends these and the client side receives them, the other way round from orders. Each has a Sent blotter under **Market** and a Received one under **Client**, as tabs beside the order blotters.

| | Sent blotter | Received blotter |
|---|---|---|
| **IOIs** | New, Clone, Replace, Cancel, Macro… | **Order**: a new order naming the IOI in tag 23; Macro… |
| **Adverts** | New, Clone, Replace, Cancel, Macro… | nothing to press |
| **Allocations** | New, Clone, Replace, Cancel, Macro… | **Accept**, **Reject**, Macro… |

- Nothing answers an IOI or an advert. A Replace or Cancel takes effect at once.
- An allocation is answered with an AllocationInstructionAck. Until then a replace or cancel waits in the Pending column, the way an order's request does.
- An allocation's orders, executions and accounts are typed one to a line: `ACC1 300` is an account and a quantity.
- **Allocate** on Received Orders starts an allocation from an order. Pick its fills from **Fills** and the executions, the total and the average price are entered for you.

## Templates

A template is a named set of terms for one kind of dialog: an order template holds symbol, side, quantity, type, price and the rest, a fill template a quantity and a price, a DK template a reason.

- Pick one from **Template** and it fills the form. A term the template leaves blank is left to the row: a fill's quantity becomes what the order has left.
- Type a name in **Save as template** and the terms you send are kept under it, replacing any template of that name and kind.
- Each dialog reopens on the template you last used in it.
- **Config › Templates** lists them all, with Edit, Clone and Delete.

Templates are kept on the server and shared by everyone who uses it.

## The Client column

Orders, trades and messages show the client they name, but counterparties carry that name in different tags. Each session therefore says where to look, in **Client Tag(s)**:

| Written as | Means |
|---|---|
| `109` | the tag ClientID |
| `448[452=3]` | the PartyID of the party whose PartyRole is 3 |
| `109,115` | tag 109 if it is there, otherwise 115 |
| blank | `448[452=3],109,115,1` |

The **Client** field of the New and Replace dialogs is written to the first of those places. An extra tag that names the client wins over it.

## Dictionaries

**Config › Dictionaries** shows the standard dictionaries, which cannot be changed, and any of your own.

- **New** and **Clone** make a dictionary either *linked* to a standard version, storing only what differs, or as a *full copy* that stands alone. **Flatten** turns a linked one into a full copy.
- The tabs edit **Fields**, **Enums**, **Messages** and **Groups**; **Wire** edits the header and trailer order and the BeginString.
- **Import** and **Export** read and write JSON. **Export Delta** writes only the differences.
- **Save** stores the dictionary. A session picks up the change the next time it starts.

A session's dictionary decides two things: how its messages are displayed, and which tags mkfix sends. Remove a tag from the dictionary and the session stops sending it.

## History

Sessions, orders, trades, IOIs, adverts and allocations keep every version of every row.

- **History**, on a blotter, opens the selected row's versions, with the differences between any two and which version last changed each column.
- **As of…** shows the whole blotter as it stood at a moment you choose.

Only a session can be put back to an earlier version. An order or a trade cannot, because the counterparty's copy of it would not move.

## Macros

A macro is a short script that does what you would do by hand: accept an order after 200 ms, fill it in clips, refuse the second replace. This one fills every IBM or MSFT order a hundred at a time:

```macro
on order where symbol in ['IBM', 'MSFT']
    after 200ms
    accept
    while order.leaves_qty > 0
        after 1s ± 250ms
        fill qty: MIN(100, order.leaves_qty), price: order.price
```

- Write one in **Client Macros** or **Market Macros**, under the **Macro** menu. The editor checks as you type and **Example…** opens a copy of a bundled one.
- An **end-to-end macro** plays both sides in one run: it sends the order and answers it, a test in one file with one verdict. It has an editor and a runs window of its own, and is run on two sessions, one a side.
- **▶** starts it: **Run…** for a macro that sends, **Arm…** for one that waits for something to arrive.
- **●** records what you do by hand and writes the macro that would have done it.
- **Macro…**, on a blotter, writes the macro from what has already happened: select the orders and it reads your side's part back from the messages kept.
- The macros of one run can work together: one says `signal`, the others hear it, and an `on signal` block sends something new for each, such as the sell that hedges a buy that has filled.
- Each side's **Macro Runs** window shows what is running, line by line, over its log.
- The same four symbols sit on each blotter's toolbar, and the status bar says what the macros are doing.

Restarting the server stops every macro, and none starts again by itself.

[Macro Language](macro-language.md) is the reference.

## Replaying a log

**FIX › Replay Control** plays a FIX log into one of your sessions as that session: **Load…** a file, **Configure…** what to play and how fast, **Start…** in one direction. The session's own CompIDs, sequence numbers and times replace the log's. See [Replaying a Log](replaying-a-log.md).

## Layouts

**Layout › Save Layout** keeps the arrangement of the windows, with each table's filters, sort and columns. The newest save is restored when the page opens, earlier ones are under **Restore Layout**, and **Reset to Default** returns to the arrangement mkfix ships with.

Layouts are kept on the server. mkfix has no login, so everyone on a server shares them.

## Sloppy focus

Sloppy focus is another way of choosing the window you work in, familiar from X11 desktops. **It is off until you turn it on, and nothing in this section applies until you do.**

- **To turn it on**, hold Shift as you open the **Window** menu: *Sloppy Focus* appears below the list of open windows. Click it.
- **To turn it off**, click *Sloppy Focus* again. While it is on it is always in the **Window** menu, ticked.
- Your browser remembers the choice. **Help › Keyboard Shortcuts** lists the keys below while it is on.

Normally a click focuses a window (the highlighted border: the window the keys and the **Edit** menu act on) and brings it to the front. With sloppy focus on, the focus follows the pointer into a window *without* bringing it forward. It stays with that window while the pointer crosses empty space, the menu bar or the status bar, so the **Edit** menu still acts on the window you were just over. A held mouse button, an open menu or a confirmation box keeps the focus where it is. A new window (a dialog, a torn-out tab) has the focus until the pointer enters another. A window gets its keyboard focus back where it was, unless you are typing in a field.

| Gesture | Does |
|---|---|
| Click a title bar | brings the window to the front. The title bar is the row of tabs along the top, the strip beside them included, or a dialog's title. Dragging by it moves the window without bringing it forward |
| Click in a window | focuses it, leaving it where it is |
| Alt/Option-click | brings the window to the front, when you let go |
| Shift+Alt/Option-click | sends it to the back |
| Alt/Option-drag | moves it from anywhere inside, snapping to other windows' edges, without bringing it forward |
| Alt/Option+P | brings the focused window to the front |
| Alt/Option+N | sends it to the back; the focus goes to the window now under the pointer |
| Alt/Option+H/J/K/L or arrows | moves a stand-in mouse pointer left, down, up or right |
| Alt/Option+Shift+H/J/K/L or arrows | moves the focused window, which keeps the focus |

### The stand-in pointer

A web page cannot move the mouse, so the keys move a stand-in. The mouse pointer disappears and an arrow with a small dot appears in its place. The stand-in focuses the windows it crosses, as the mouse would, but it does not click, and hover highlights stay where the mouse is. Move, click or scroll the mouse and the stand-in goes, the mouse pointer coming back where it was.

A tap moves the pointer or the window 5 pixels. Held, it glides, faster the longer you hold, and two keys together go diagonally. Pressing or letting go of Shift mid-glide switches between moving the pointer and moving the window. A window moved by keys carries the stand-in along and doesn't snap; a tiled window becomes an ordinary one.

### When the key or click is someone else's

- **In a text field**, the macro editor included, the arrows, Alt/Option+P and Alt/Option+N are the field's. On a Mac the letters are too, since Option+letter types a character. On Windows and Linux, Alt+H/J/K/L still work from a field.
- **Alt/Option+Shift+←/→** moves the window, not the tab, while sloppy focus is on. Drag a tab to reorder it.
- **Alt/Option-clicks** belong to the window while sloppy focus is on. Two Alt-clicks inside windows stop working: on a filter's group icon, which switches every filter off or on, and on a dialog section's heading, which folds every section. Switching to Ctrl+Alt-click (below) gives them back.
- **Some Linux desktops** (Xfce, KDE before Plasma 6) take Alt-click for themselves, so the page never sees it. There, hold Shift as you open the **Window** menu while sloppy focus is on and tick *Raise with Ctrl+Alt-click*. The clicks become the ones below, and a plain Alt-click is the content's again. The keys stay Alt. A Mac doesn't offer the choice, since Ctrl-click is a right-click there.

| Gesture | Does |
|---|---|
| Ctrl+Alt-click | brings the window to the front |
| Shift+Ctrl+Alt-click | sends it to the back |
| Ctrl+Alt-drag | moves it |

## The command line

```
mkfix                      start the server: port 8080, mkfix.db
mkfix -p 9090              another port
mkfix -d mytest            another database, mytest.db
mkfix -d :memory:          a database that is gone when the server stops
mkfix -i Q7                the two characters in generated IDs
```

| Command | Does |
|---|---|
| `mkfix check FILE…` | checks macro files without a server, a line for each problem |
| `mkfix run FILE` | saves a macro on a running server and starts it; `--wait` follows it to its verdict |
| `mkfix archive` | moves data from before today into CSV files and deletes it from the database |
| `mkfix restore DIR` | puts an archive back; the server must be stopped |

Each takes `-h` for its options and examples.

## When something looks wrong

| You see | Why, and what to do |
|---|---|
| A blotter's buttons are greyed | The row's session is not `ACTIVE`, or the row is in a state the button does not apply to. Start the session. |
| A session goes to `ERROR` | The Error column says why. An initiator gives up after its retries; Start tries again. |
| The status bar is red and says *Server version mismatch* | The page is older or newer than the server. Reload the page. |
| The status bar is grey and says *Disconnected* | The server has stopped or cannot be reached. The page reconnects by itself when it is back. |
| A blotter says *not updating — retry* | The server ended that table's subscription. Press retry. |
| The order you sent is `Rejected` with *Send failed* | The message never left: the session dropped as it was sent. |
| A macro's run says `interrupted` | The server was restarted while it ran. Start it again. |
| `mkfix` exits at once saying the port is taken | Another server is using it. Choose another with `-p`. |
| Yesterday's orders are gone | They are hidden, not gone: the blotter opens on today. Clear the *Today* chip. |
