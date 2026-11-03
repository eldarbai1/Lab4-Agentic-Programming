# SPEC: Live Agent-Traffic Dashboard (latest)

A browser dashboard that shows, live, every request Claude Code (or another agent) sends
through `proxy.py` and every response the API streams back. **The newest traffic is shown first.**

Status: **v1.2, matches Build 1** (branch `build-1`, commit `fa15c7b`, which includes the
v1.2 user-prompt change). All MVP items (§2) are implemented. No **[Stretch]** items are implemented yet.
See `SPEC_HISTORY.md` for what changed from the original spec and why.

Files: `proxy.py` (proxy, event bus and dashboard server) and `index.html` (the whole frontend).

---

## 1. Goals

- Show how an agent works: system instructions, tool definitions, growing conversation history,
  tool calls, and the tool results sent back in the following request.
- Stay readable when there are hundreds of messages and multi-kilobyte prompts.
- Update live while the agent runs, including token-by-token streaming of responses.

### Non-goals

- Changing, replaying or injecting traffic. The proxy stays a passive tap.
- Multiple users, remote access or authentication. Localhost only.
- Exact cost accounting.

---

## 2. MVP for the 70-minute lab

The lab build includes **only** the following. Everything else in this spec is marked
**[Stretch]** and is built only if time remains.

| # | MVP feature                       | Spec section | MVP scope                                                                                       |
|---|-----------------------------------|--------------|-------------------------------------------------------------------------------------------------|
| 1 | Live SSE updates                  | §3, §4       | `GET /events` pushes events to the browser. New traffic appears without a reload. Events are held in an in-memory buffer so a reconnecting or late-joining tab gets the whole session. |
| 2 | Request/response cards            | §6           | One card per exchange, **newest at the top**. Collapsed summary line, expanded Request and Response columns, responses streamed live. |
| 3 | System instructions               | §7           | Shown in full the first time. Later cards show an "unchanged" or "changed" badge you can click to open. |
| 4 | Tool definitions                  | §8           | A badge with the tool count, tool names as chips, and each tool's schema in the JSON viewer.   |
| 5 | Conversation/history              | §9           | Only the messages new since the previous request are shown, with a "show full history" toggle. |
| 6 | Tool call/result pairing          | §10          | `tool_use` and `tool_result` are matched by `tool_use_id`, linked in both directions, with pending/ok/error status. |
| 7 | Collapsible long content          | §12.2        | Long text is cut, with a "Show all (N lines)" button. What you open stays open during live updates. |
| 8 | Readable JSON                     | §12.3        | A collapsible, colour-coded tree with a copy button.                                            |
| 9 | Basic filtering                   | §13          | Plain-text search, a tool-name filter, an "errors only" filter, and a "messages only" endpoint toggle. |
| 10 | Error display                     | §14          | HTTP errors, stream errors, proxy errors and tool results with `is_error` are shown in red, with an error count in the header. |
| 11 | Basic token/timing stats          | §11          | Per card: input/output/cache tokens, time to first token and total time. Header: number of exchanges, errors and total tokens. |
| 12 | User prompt shown clearly *(added v1.2)* | §6.3     | The text the user typed, with Claude Code's `<system-reminder>` blocks removed, is shown on the collapsed card and in a highlighted box at the top of the Request column. |

**Stretch goals**, built only if time remains:
- persistence to disk and the session picker (§16)
- replay (§16)
- diffs (§7, §8, §9)
- advanced statistics (§11)
- keyboard shortcuts (§12.4)
- regex search and the additional filters (§13)
- performance optimizations (§12.4, §15)

---

## 3. Architecture

```
claude ──HTTP──▶ proxy.py :8041 ──HTTPS──▶ api.anthropic.com
                    │
                    ├─ in-memory event list (MVP)
                    ├─ appends events ─▶ sessions/<session-id>.jsonl      [Stretch]
                    │
                    └─ dashboard server :8042 ──▶ browser
                         GET /              → index.html (single static page)
                         GET /events        → SSE stream of live events
                         GET /sessions      → list of saved sessions        [Stretch]
                         GET /sessions/<id> → full JSONL of one session      [Stretch]
```

- **One process, standard library only.** `proxy.py` gains a second `ThreadingHTTPServer` on
  `--dashboard-port` (default 8042) running in a background thread. Nothing has to be installed.
- **Proxy behaviour does not change.** Forwarding stays byte for byte. The dashboard only
  observes. If the dashboard fails, it must never break or slow the agent's traffic: event
  publishing is non-blocking, and a slow browser loses events rather than holding up the proxy.
- **Event bus.** The proxy thread does no dashboard work itself. It only puts raw
  observations (request bytes, response chunks, timings) on an unbounded queue, which is an
  O(1) operation that never blocks. A single worker thread turns them into structured events
  (§5): JSON parsing, SSE parsing, header redaction and serialisation. Each event is:
  1. serialised once and appended to the in-memory event list for the session,
  2. delivered to every connected SSE client. Each client has its own **cursor** into the
     shared list and reads at its own pace, so a slow tab falls behind instead of stalling
     the proxy, and it loses nothing. This replaces the per-client bounded queue of v1.0.
  3. **[Stretch]** appended to the session JSONL file.
- **Frontend.** One `index.html` with inline JS and CSS. No build step and no CDN, so it works
  offline. It uses `EventSource` for live data.
- **Binding.** The dashboard binds to `127.0.0.1` only.
- **Terminal output.** The current terminal printing stays, behind `--quiet` to turn it off.

---

## 4. Live updates (MVP)

- The browser subscribes to `GET /events`, an SSE stream. The SSE `id:` field is
  `<session_id>:<index>`, where index is the event's position in the in-memory event list.
  The JSON payload also carries `"id": <index>`.
- **Reconnecting.** `EventSource` reconnects automatically and sends `Last-Event-ID`. If the
  session part matches the running session, the server replays the events after that index.
  If it doesn't match, because the proxy was restarted, the server replays everything from
  index 0. When the browser sees a `session_start` with a new `session_id`, it clears its state.
  The browser also ignores any event whose index it has already processed. A connection
  indicator in the header shows live, reconnecting or offline.
- The server sends `retry: 2000` and a `: ping` comment every 15 s while idle.
- **Late join.** When the page opens in the middle of a session, the server first replays every
  event from the start, then continues with live events.
- Streaming text appears in its card as it arrives.

---

## 5. Event model

Every event has the fields `{ "type", "exchange_id", "ts" }`, where `ts` is an ISO 8601
timestamp in milliseconds. `exchange_id` is a counter that increases within a session.

| type              | When                              | Payload                                                                       |
|-------------------|-----------------------------------|-------------------------------------------------------------------------------|
| `session_start`   | Proxy starts                      | `session_id` (`YYYYMMDD-HHMMSS`), `upstream`, `proxy_version`                  |
| `request`         | Request body fully received       | `method`, `path`, `headers` (redacted), `body` (parsed JSON or raw string), `body_is_json` |
| `response_start`  | Upstream status received          | `status`, `reason`, `headers` (redacted), `ttfb_ms`                            |
| `stream_event`    | Each SSE event from upstream      | `event`, `data` (parsed JSON, or the raw string if it is not JSON)              |
| `response_body`   | Non-streaming body complete       | `body`, `body_is_json` (bodies kept up to 8 MB)                                |
| `response_end`    | Upstream finished                 | `total_ms`, `bytes`                                                            |
| `proxy_error`     | Upstream unreachable / connection reset / client gone | `stage`, `message`, and optionally `status` (502 for `upstream_connect`), `total_ms`, `bytes` |

- `session_start` has `exchange_id: 0`. Exchanges are numbered from 1.
- `proxy_error.stage` is one of `upstream_connect` (the agent gets the 502, as before),
  `upstream_request` (sending the request or reading the status line failed),
  `upstream_read` (the stream broke) and `client_write` (the agent disconnected). For the last
  three, the proxy re-raises the exception after reporting it, so the agent sees exactly what
  it saw before the dashboard existed.
- Upstream SSE is parsed on the worker thread with `\r\n` normalised, multi-line `data:`
  joined and `:` comments ignored. Raw response chunks are never published as events.

- **Redaction.** `authorization`, `x-api-key`, `cookie`, `set-cookie` and
  `proxy-authorization` headers, in both requests and responses, are cut to their first
  16 characters plus `…`, as in `proxy.py` today. Redaction happens on the server, before the
  event is stored. Secrets must never reach the browser (or,
  later, the JSONL file) in full.
- **Reconstruction is done in the browser.** The client assembles each response message from
  its `stream_event`s: `message_start`, `content_block_start`, `content_block_delta`
  (`text_delta`, `input_json_delta`, `thinking_delta`), `content_block_stop`, `message_delta`
  and `message_stop`. The server stores only what it actually saw.
- **Endpoints other than messages.** A request counts as a messages request when its path,
  without the query string, is exactly `/v1/messages`, so Claude Code's
  `/v1/messages?beta=true` counts. Everything else, for example
  `/v1/messages/count_tokens` or `HEAD /api/hello`, is recorded, shown as a compact card, and
  hidden by default behind the "messages only" toggle.
- **Time to first token** is computed in the browser: the `ts` of the first
  `content_block_delta` minus the `ts` of the `request` event.

---

## 6. Main view: timeline of exchanges (MVP)

The page has a fixed header (stats, §11), a filter bar (§13), and a scrolling timeline of
**exchange cards ordered newest first**. The most recent exchange is always the top card.

- A new exchange is inserted **at the top** as soon as its `request` event arrives. It then
  fills in its response in place while streaming.
- Newest first applies **to cards only**. Inside a card, messages, content blocks and SSE events
  are listed in their natural chronological order, so they read top to bottom.
- Scrolling behaviour is described in §12.4.

### 6.1 Exchange card (collapsed)

One line per exchange:

```
#12  14:03:21  POST /v1/messages  claude-opus-5-5  200  1.8s (ttft 0.4s)
     in 48,210 (cache read 47,900)  out 312   ▸ +2 msgs  → tool_use: Read   ● stop: tool_use
     💬 read test.txt and tell me what it says
```

- The status is coloured: 2xx neutral, 4xx/5xx red, still streaming has a pulsing marker.
  The card's left edge is red when it has errors and blue while in flight. An error count
  badge (`⚠ 2 errors`) is shown.
- When the new messages contain a user prompt (§6.3), it is shown on its own line,
  prefixed with 💬 and cut to 200 characters. Hovering shows the full text.
- The summary shows the stop reason and the names of any tools called in the response.
- The newest messages card starts expanded. When a newer one arrives, the previous one is
  collapsed, unless the user toggled it or the user is scrolled down reading. A card's body is
  only built the first time it is expanded.

### 6.2 Exchange card (expanded)

The card has two columns, **Request** on the left and **Response** on the right. On narrow
screens they stack.

Each card has **Expand all** / **Collapse all** buttons, and each column has
**Rendered** / **Raw** tabs.

**Request side**, in this order:
0. **User prompt** *(v1.2)*: a highlighted box with the user's own text (§6.3), when present.
1. **System**: a badge (§7). The full text appears only when the system prompt is new or changed.
2. **Tools**: a badge (§8).
3. **History delta**: only the messages that are new since the previous `/v1/messages` request (§9).
4. A "Show full history (N messages)" toggle.
5. **Parameters**: `model`, `max_tokens`, `temperature`, `thinking`, `stream`, `metadata` and
   any other top-level keys, shown as a small key/value table.
6. A **Raw** tab with the full request JSON in the JSON viewer (§12.3) and the headers.

**Response side:**
1. A red error box, if there are errors (§14).
2. The assembled assistant message, with content blocks rendered by type (§12.1). While a
   block streams, its raw text (or partial tool-input JSON) grows live with a cursor. When
   `content_block_stop` arrives, it is replaced by the final rendering.
3. `usage`, `stop_reason`, timing (ttfb, ttft, total), bytes and the message id.
4. A **Raw** tab with the response headers, the ordered list of SSE events (index, offset
   from the request, event name and delta type, data in the JSON viewer), and the body for
   non-streaming responses. The list grows live.

### 6.3 User prompt *(added in v1.2)*

Claude Code wraps the user's typed text in the same message as injected context blocks
(`<system-reminder>…</system-reminder>`), so the question is buried under the context.

- **Extraction.** For each `user` message, take its `text` blocks (or the string content),
  remove every `<system-reminder>…</system-reminder>` section, trim, and keep what is left.
  `tool_result` blocks never count as a prompt.
- **Which one.** The card's prompt is the **last** user message among the new messages
  (§9) that has prompt text. A request whose new messages are only tool results has no
  prompt.
- **Where it is shown.** In three places:
  - the collapsed summary line (§6.1);
  - a highlighted box at the top of the Request column, in a larger proportional font, with
    a "Show all" cut for long prompts and the label `(message [i], system-reminders removed)`;
  - the message preview in the history, as `💬 <text>`.
- **Reminder blocks.** A text block that contains nothing but system-reminders is shown as a
  one-line collapsed `system-reminder (N chars)` row, so it no longer hides the real text.

---

## 7. System instructions (MVP)

- The client computes a short hash of the normalised `system` field, which may be a string or
  an array of blocks, and gives each distinct value a version label: S1, S2, …
  Normalisation: a string becomes one text block, and `cache_control` keys are ignored.
- Versions are compared with the previous **messages** request. When the value differs
  from the previous one but was seen before, for example because side requests alternate
  with the main conversation, the badge reads
  `System S1 (changed from S2; first seen in #3)` and stays collapsed.
- The first time a version appears, it is shown expanded, but collapsed beyond the length
  limit (§12.2).
- After that, the card shows the badge `System S1 (unchanged, 12.4k chars)`. Clicking the badge
  opens the full text.
- When the system prompt changes, the badge reads `System S2 (changed from S1)` and shows the
  new text in full.
- `cache_control` markers on system blocks are shown as small 🔖 tags.
- **[Stretch]** A line diff of S2 against S1.
- **[Stretch]** A **Library** side panel listing every system version and every tool-set
  version, with the exchange numbers that used each one.

## 8. Tool definitions (MVP)

- Tool sets get version labels the same way: T1, T2, … The hash covers the full `tools` array.
- The badge shows `Tools T1 (14 tools)`. Expanding it lists the tool names as chips. Clicking a
  chip shows that tool's description and its `input_schema` in the JSON viewer.
- When the tool set changes, the badge reads `Tools T2 (changed from T1)`.
- **[Stretch]** A list of tools added, removed and modified between versions.
- **[Stretch]** A call count on each tool chip.

## 9. Conversation history (MVP)

- **Structural comparison.** Each message is hashed after normalisation:
  - `cache_control` keys are removed, because Claude Code moves the marker every turn;
  - `content: "x"` is treated as `content: [{type: "text", text: "x"}]`, because Claude Code
    re-encodes an older message this way once its cache marker has moved on.

  Without these two rules, every real Claude Code turn looked like rewritten history.
- **Base request.** Among the last 80 messages requests, the base is the one whose
  **entire** message list is a prefix of request N, taking the longest. This replaces "the
  previous request" from v1.0, because Claude Code's side requests, such as title
  generation or quota checks, interleave with the main conversation and would otherwise
  break every delta.
- **Delta rule.** Show only the messages after the base's prefix, normally the previous
  assistant turn plus the new user turn or tool results. The header reads
  `History delta: +N new messages since #K (T total)`. Messages keep their real indexes.
- **No base.**
  - If an earlier request shares a longer *partial* prefix than any full-prefix base, the card
    shows **"History rewritten — closest earlier request #K shares only k of its n
    messages"**, followed by the full history.
  - If nothing is shared at all, it shows **"History rewritten / new conversation"**, followed
    by the full history.
  - The very first messages request of a session shows its whole history without a notice.
- The full history view lists all messages with an index (`[0] user`, `[1] assistant`, …), and
  each message can be collapsed.
- **[Stretch]** A diff summary for rewritten history, for example "kept 3, removed 40, added 2".
- **[Stretch]** **Conversation tracking.** Requests are grouped into a conversation when their
  histories share a prefix. A side request, such as one using a small model to generate a
  title, appears as a separate thread lane with its own colour, so the deltas of the main
  conversation are not polluted.

## 10. Tool calls paired with results (MVP)

- The client keeps an index `tool_use_id → { call: (exchange, block), result: (exchange, block) }`.
- A `tool_use` block shows the tool name, its input in the JSON viewer, and a link:
  **"→ result in #13"**. With newest first, #13 is the card **above**. A `tool_result` block
  shows **"← called in #12 (Read)"**.
- Clicking a link scrolls to the partner block, expanding its card if needed, and briefly
  flashes it. Hovering one side highlights its partner.
- Status of each pair:
  - **pending**: a call with no result yet, shown with a spinner
  - **ok**: a result arrived
  - **error**: the result has `is_error: true`, shown in red
  - **orphan**: a result with no matching call in the session, shown with a warning
- **Where each side is registered.**
  - A call is registered from the response's `content_block_start` (so it shows as pending
    while it streams). If a call is only seen in request history, for example because the
    proxy started mid-conversation, it is registered from the first request that contains it.
  - A result is registered at the first request that contains it, which is normally in that
    card's history delta.
  - Later copies of the same blocks in history show the same links.
- **Errors.** `pending` is not an error, because every call is pending until the next
  request. `orphan` and `error` count as errors in the header and on the card.
- Several tool calls in one response are listed together, each with its own link.
- The result content is shown in full (text or image placeholder) and collapsed beyond the limit.
- **[Stretch]** A **Tool calls** panel listing every pair in a table: id, tool, a summary of the
  input, the status, how long the tool took (time from the call's response ending to the next
  request arriving), and the exchange numbers.

## 11. Statistics

### MVP (basic token/timing stats)

- **Per card:**
  - input, output, cache read and cache write tokens, taken from `usage`
  - time to first token
  - total time
- **Header:**
  - number of exchanges, with the number still in flight
  - number of errors
  - total input, output and cache tokens for the session
  - connection status (§4)

### [Stretch] Advanced statistics

- Median and p95 for time to first token and for total time
- Cache hit ratio, defined as cache_read / (input + cache_read)
- Context size per request, shown as a sparkline to make growth and compaction visible
- Tool calls by tool name, shown as a bar list, plus the tool error count
- Models used, with the number of exchanges for each
- Session duration and time since the last event
- Statistics for the filtered subset next to the totals for the whole session

## 12. Readability

### 12.1 Content blocks (MVP)

| Block                  | Rendering                                                          |
|------------------------|--------------------------------------------------------------------|
| `text`                 | Plain text with line breaks kept                                    |
| `thinking`             | Muted italic panel, collapsed by default, shows its length          |
| `redacted_thinking`    | Placeholder "redacted thinking (N bytes)"                           |
| `tool_use`             | Tool chip, JSON input, pairing link (§10)                           |
| `tool_result`          | Bordered panel, pairing link, red border when `is_error`            |
| `image` / `document`   | Placeholder with media type and size; the data is never rendered inline |
| unknown types          | Shown in the JSON viewer, labelled with the type (for example Claude Code's `tool_addition`) |
| reminder-only `text`   | *(v1.2)* Collapsed one-line `system-reminder (N chars)` row (§6.3)   |

Roles have distinct colours on the left edge of each message: user, assistant and system.
Claude Code does send `role: "system"` messages inside `messages`, and they get the system colour.
`cache_control` on any block is shown as a 🔖 tag.

### 12.2 Collapsing long content (MVP)

- Any text over **20 lines or 2,000 characters** is cut, with a fade and the button
  **"Show all (843 lines)"**.
- System prompts, full history and raw SSE events are collapsed by default.
- **Expand all** and **Collapse all** controls are available for each card.
- Expand and collapse state survives live updates. New events must not reset what you have opened.
- **[Stretch]** Thresholds can be changed in a settings menu, and there are Expand all /
  Collapse all controls for the whole page.

### 12.3 JSON viewer (MVP)

- A collapsible tree with syntax colouring, showing item counts on collapsed nodes, for
  example `{…} 7 keys` or `[…] 42 items`.
- Long strings inside JSON are cut and can be expanded.
- A **Copy** button copies the JSON of any node.
- A toggle switches between the tree and the pretty-printed raw text.
- **[Stretch]** Strings that contain JSON can be opened as nested JSON.
- **[Stretch]** A **Copy path** button, giving paths such as `messages[3].content[0].text`.

### 12.4 Scrolling and volume

**MVP: scrolling with newest first**
- **When you are at the top of the page**, which is the default, new cards appear at the top
  and stay in view. The page follows the live traffic with no scrolling needed.
- **When you have scrolled down** to read older cards, inserting new cards above must **not**
  move what you are reading. The page keeps the scroll position fixed on the current content,
  using `overflow-anchor` or by adjusting the scroll offset by the height of the new content.
  A **"N new ↑"** pill appears. Clicking it, or scrolling back to the top, jumps to the newest
  card and clears the count.
- A card that keeps streaming while you read it grows in place without moving the cards around it.

**[Stretch] Keyboard shortcuts**
- `j`/`k` move to the next or previous card
- `Enter` expands or collapses a card
- `/` focuses search
- `e` jumps to the next error
- `g` jumps to the newest card

**[Stretch] Performance optimizations**
- Cards are rendered lazily: the body of a card is only built in the page when the card is
  expanded or scrolled into view.
- Streaming updates are grouped into one redraw per animation frame.
- A **Pause** button stops redrawing while events keep buffering. Resuming applies them all.

## 13. Filtering and search

### MVP (basic filtering)

- **Search box:** plain, case-insensitive substring search, debounced by 150 ms. Only
  matching cards are shown, and the matches inside them are highlighted with the CSS Custom
  Highlight API (up to 3,000 ranges, in rendered content only).
- **What is searched** for each card:
  - method, path and model;
  - the card's **new** messages (§9), including tool inputs and results;
  - the request parameters;
  - the response content and the error messages.

  The system prompt and tool definitions are searched only on the card where that version
  first appears (§7, §8). Otherwise a word like "Read" would match every card.
- **Filters**, combined with AND:
  - Tool name: exchanges that call or receive a result for that tool
  - Errors only
  - Endpoint: messages only (default) / all
- Filters apply to live cards as they arrive. A new card that doesn't match is not shown, but
  it still counts in the header totals.

### [Stretch]

- `/regex/` search syntax.
- Collapsed sections containing a match open automatically, with a match counter and
  next/previous buttons.
- Extra filters: model, content type (thinking / tool_use / tool_result / text), in flight,
  conversation thread.
- Filters and search are saved in the URL hash, so a view can be bookmarked or reloaded.

## 14. Errors (MVP)

All of the following are shown with a red marker on the card and counted in red in the header:

| Error                                   | Source                                     |
|-----------------------------------------|--------------------------------------------|
| HTTP 4xx/5xx (including 429, 529)      | `response_start.status`, error body parsed  |
| `error` event inside the SSE stream     | `stream_event` with `event: error`          |
| Stream ended without `message_stop`     | detected at `response_end`                  |
| Upstream unreachable (proxy's 502)      | `proxy_error`                               |
| Client disconnected mid-stream          | `proxy_error` with `stage: client_write`    |
| Tool result with `is_error: true`       | request body                                |
| Orphan tool result                      | pairing index (§10)                         |
| Body that is not valid JSON             | shown raw with a "not JSON" warning         |

A pending tool call is shown with a spinner but is **not** counted as an error (§10).

For an API error, the card shows the error `type` and `message` prominently, plus the
`retry-after` header and the request id when present. Retry-after and request id are shown
only for HTTP or stream errors, not for tool errors. The header error count is the total
number of error items across all exchanges.

## 15. [Stretch] Live-update robustness

- A bounded in-memory buffer, for example the last N events, with older events served from the
  JSONL file once persistence exists.

## 16. [Stretch] Persistence, sessions and replay

- Each run of the proxy creates `sessions/<YYYYMMDD-HHMMSS>.jsonl`, with one event per line,
  appended and flushed per event.
- A session picker in the header lists saved sessions with start time, number of exchanges
  and size. Opening an old session shows it read-only, with no live stream.
- **Replay** mode plays a saved session back at 1× / 4× / instant speed, using the original
  timing. Cards are inserted newest first, exactly as they were live.
- **Export** downloads the current session JSONL.
- Flags: `--sessions-dir` (default `./sessions`) and `--no-save`.

## 17. CLI changes to `proxy.py`

```
python3 proxy.py [--port 8041] [--upstream URL] [--dashboard-port 8042] [--quiet]
                 [--sessions-dir ./sessions] [--no-save]        # [Stretch]
```

On startup the proxy prints: `dashboard: http://localhost:8042`.

Implemented in Build 1: `--port`, `--upstream`, `--dashboard-port` and `--quiet`.
`index.html` is read from disk on every page load, so frontend changes need only a browser
reload, not a proxy restart.

### Running it

```
python3 proxy.py                                   # proxy :8041, dashboard :8042
ANTHROPIC_BASE_URL=http://localhost:8041 claude    # in a second terminal
# open http://localhost:8042
```

---

## 18. Acceptance criteria (MVP)

1. Running `ANTHROPIC_BASE_URL=http://localhost:8041 claude` and asking it to "read test.txt"
   produces, in the dashboard, without refreshing the page:
   - exchange #N with a response containing `tool_use: Read`, streamed live,
   - exchange #N+1 **above it**, whose history delta contains the `tool_result` with the same
     `tool_use_id`, and the two are linked both ways,
   - the system prompt and tools shown in full once and as "unchanged" badges afterwards.
2. The newest exchange is always the top card. While you have scrolled down reading an older
   card, new traffic does not move what you are reading, and a "new ↑" pill appears.
3. Closing and reopening the browser tab mid-session (while the proxy keeps running) restores
   every exchange, and live updates continue.
4. Breaking upstream access (bad `--upstream`) shows a red `proxy_error` card, and the agent
   gets the same 502 response as it does today.
5. Agent traffic through the proxy is byte-for-byte identical whether or not the dashboard is
   open. Having the dashboard open causes **no noticeable delay** for the agent, and a slow or
   closed browser tab never stalls the agent.
6. The dashboard stays **responsive in a typical lab session**, meaning dozens of exchanges
   with large system prompts and long histories:
   - expanding and collapsing cards feels immediate,
   - search and filters update without a visible freeze,
   - live streaming text does not make the page stutter or lose your scroll position.
7. No full auth token appears anywhere in the browser (or, later, in the JSONL files).
8. *(v1.2)* After typing a question in Claude Code, the question itself, without the
   system-reminder text, is readable on the newest card without expanding anything.

### Verification status (Build 1)

| # | Result | How it was checked |
|---|--------|--------------------|
| 1 | Pass | Real `claude -p` sessions: a Read call in #N and its result in #N+1 are linked both ways. A later session reading a missing file produced a red `is_error` pair. |
| 2 | Pass | Headless Chrome, scrolled down: two new cards streamed in above, and the reading position moved less than 1 px. The pill showed "2 new ↑" and clicking it jumped to the top. |
| 3 | Pass | Reload mid-session restores all cards (8 MB, 786 events, in about 0.5 s). `Last-Event-ID` resume and stale-session replay were checked with curl. |
| 4 | Pass | `--upstream http://127.0.0.1:1`: the client got the same 502 line as before, and the dashboard got a `proxy_error` with stage `upstream_connect`. |
| 5 | Pass | The response through the proxy has the same md5 as the response direct from upstream. No added delay was measurable over 60 requests. |
| 6 | Pass | 60 exchanges with a 60 KB system prompt and 120-message history: expanding 10 cards took 24 ms, search 7 ms, opening the full history 7 ms. |
| 7 | Pass | The page DOM was searched for the token prefix: only the 16-character redacted form appears. |
| 8 | Pass | Against captured real traffic: `💬 read test.txt and tell me the secret word` appears on the card and in the prompt box. |

## 19. Open questions (deferred)

- Show estimated cost per exchange? This needs a pricing table that is maintained by hand.
- Also support the Codex / OpenAI Responses format (`--upstream …/codex`)? The event model is
  generic, but the content rendering here is specific to Anthropic.
- **[Stretch]** Show a side-by-side diff between any two requests, not just consecutive ones?
