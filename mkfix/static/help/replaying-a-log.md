# Replaying a Log

Replay Control (To Do menu) plays a FIX log — a day's worth, both sides — into one of your sessions, as that session. Load a file once, choose what to play, and Start asks which side you are.

## Load

**Load…** takes a path to a file on the machine running mkfix, or one of the bundled examples. The whole file is read once and what it holds goes on the job's row: the CompID pairs it carries (`PRODCLI → PRODVENUE · 11`, `PRODVENUE → PRODCLI · 17`), the message types with counts, the first and last timestamp. Four shapes of line are recognised, and a file may mix them:

```
20260921-14:00:00.000 : 8=FIX.4.4|9=140|35=D|49=PRODCLI|56=PRODVENUE|34=1002|...|10=228
2026-09-21 14:00:00.000 | 8=FIX.4.4|9=140|35=D|...
8=FIX.4.4|9=140|35=D|49=PRODCLI|...
35=D|11=REPLAY-001|55=IBM|54=1|38=100|40=2|44=150.25|
```

The delimiter may be the real SOH byte or a `|`. Lines that are not FIX are skipped, as are lines starting with `#`. Logons, Logouts, Heartbeats, TestRequests, ResendRequests, Rejects and SequenceResets are left out at Load: a replay never sends an admin message.

The last shape carries no header at all. That is fine — the session supplies one — and such a file has a single direction, so Start has nothing to ask.

## Configure

**Configure…** sets, per job:

- **Session to replay into** — the messages go out as this session.
- **Speed** — the gaps between the log's timestamps are divided by it; `0` sends everything at once.
- **Max gap** — a gap longer than this many seconds is cut to it, so a lunch break or the overnight does not hold the replay up. `0` keeps every gap whole.
- **From / To** — a window by the log's time of day, `HH:MM` or `HH:MM:SS`; blank means from the start or to the end.
- **Message types** — every type the file holds, ticked or not.

Pacing comes from the line's timestamp prefix, or SendingTime(52) where there is none, or TransactTime(60) where there is neither. A message with no timestamp at all goes out straight after the one before it.

## Start

**Start…** asks the **direction**: which of the file's CompID pairs to play, the messages from that sender to that target. The pair whose sender is the session's own SenderCompID is preselected when the file has one; a production log will not, so you choose. Play the client's side into an initiator and its orders arrive on the acceptor's Received Orders blotter; play the venue's side into an acceptor and its ExecutionReports drive the initiator's Sent Orders and Received Trades. The same job can do both, one Start at a time.

**Pause**, **Resume** and **Stop** act on a playing job; a finished job starts again from the top. **Delete** removes a job, stopping it first.

## What goes on the wire

Every message is sent *as the session*:

| Tag | On the wire |
|---|---|
| BeginString(8), BodyLength(9), CheckSum(10) | the session's, recomputed |
| MsgSeqNum(34) | the session's next number — a replay spends sequence numbers like any send |
| SenderCompID(49), TargetCompID(56) | the session's |
| SendingTime(52), TransactTime(60) | the time of sending |
| PossDupFlag(43), OrigSendingTime(122), LastMsgSeqNumProcessed(369) | dropped — they described the log's own sequence space |
| everything else | as logged, in the logged order |

"Everything else" includes the routing tags — SenderSubID(50), TargetSubID(57), OnBehalfOfCompID(115), OnBehalfOfSubID(116), DeliverToCompID(128), DeliverToSubID(129), the location IDs — and repeating groups, which go out with their entries in the logged order. The Messages pane shows each replayed message as the session sent it.

## The bundled examples

| Example | What it shows |
|---|---|
| `two-sided-day` | A day between a client and a venue: orders, a replace, cancels, a DK; acks, fills, a reject, a cancel reject, a re-notified fill, an end-of-day expiry; admin messages that are never sent; a lunch gap for **Max gap**; routing tags and a Parties group on one order. Start asks the direction. |
| `order-flow` | Six order messages with no header and a timestamp prefix: one direction, the session's header on every message. |
| `pipe-raw` | Three whole messages, pipe-delimited, no prefix: SendingTime paces them. |
| `soh-raw` | The same three with the real SOH byte. |
| `iso-timestamped` | The same three behind ISO-8601 timestamps. |

Load `two-sided-day` into a pair of loopback sessions (Help › Macro Examples has a button that makes them) and play it once each way to see both blotters fill.
