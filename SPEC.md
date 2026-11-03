# SPEC: Live Agent-Traffic Dashboard

A browser dashboard that shows, live, every request Claude Code (or another agent) sends
through `proxy.py` and every response the API streams back. **The newest traffic is shown first.**

Status: specification only. Nothing here is implemented yet.

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
- **Event bus.** The proxy produces structured events (§5). Each event is:
  1. appended to the in-memory event list for the session,
  2. sent to every connected SSE client through a bounded per-client queue, and
  3. **[Stretch]** appended to the session JSONL file.
- **Frontend.** One `index.html` with inline JS and CSS. No build step and no CDN, so it works
  offline. It uses `EventSource` for live data.
- **Binding.** The dashboard binds to `127.0.0.1` only.
- **Terminal output.** The current terminal printing stays, behind `--quiet` to turn it off.

---

## 4. Live updates (MVP)

- The browser subscribes to `GET /events`, an SSE stream. Each event carries an `id`, which is
  its index in the in-memory event list.
- **Reconnecting.** `EventSource` reconnects automatically and sends `Last-Event-ID`. The server
  then replays the events missed since that id. A connection indicator in the header shows
  live, reconnecting or offline.
- **Late join.** When the page opens in the middle of a session, the server first replays every
  event from the start, then continues with live events.
- Streaming text appears in its card as it arrives.

---

## 5. Event model

Every event has the fields `{ "type", "exchange_id", "ts" }`, where `ts` is an ISO 8601
timestamp in milliseconds. `exchange_id` is a counter that increases within a session.

| type              | When                              | Payload                                                                       |
|-------------------|-----------------------------------|-------------------------------------------------------------------------------|
| `session_start`   | Proxy starts                      | `session_id`, `upstream`, `proxy_version`                                      |
| `request`         | Request body fully received       | `method`, `path`, `headers` (redacted), `body` (parsed JSON or raw string)     |
| `response_start`  | Upstream status received          | `status`, `reason`, `headers`, `ttfb_ms`                                       |
| `stream_event`    | Each SSE event from upstream      | `event`, `data` (parsed JSON)                                                  |
| `response_body`   | Non-streaming body complete       | `body`                                                                         |
| `response_end`    | Upstream finished                 | `total_ms`, `bytes`                                                            |
| `proxy_error`     | Upstream unreachable / connection reset / client gone | `stage`, `message`                                         |

- **Redaction.** `authorization`, `x-api-key` and cookie headers are cut to their first
  16 characters plus `…`, as in `proxy.py` today. Secrets must never reach the browser (or,
  later, the JSONL file) in full.
- **Reconstruction is done in the browser.** The client assembles each response message from
  its `stream_event`s: `message_start`, `content_block_start`, `content_block_delta`
  (`text_delta`, `input_json_delta`, `thinking_delta`), `content_block_stop`, `message_delta`
  and `message_stop`. The server stores only what it actually saw.
- **Endpoints other than messages.** Requests to paths other than `/v1/messages` (for example
  `count_tokens` or HEAD probes) are recorded, shown as compact cards, and hidden by default
  behind the "messages only" toggle.

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
```

- The status is coloured: 2xx neutral, 4xx/5xx red, still streaming has a pulsing marker.
- The summary shows the stop reason and the names of any tools called in the response.
- The newest card starts expanded. Older cards are collapsed unless the user expanded them.

### 6.2 Exchange card (expanded)

The card has two columns, **Request** on the left and **Response** on the right. On narrow
screens they stack.

**Request side**, in this order:
1. **System**: a badge (§7). The full text appears only when the system prompt is new or changed.
2. **Tools**: a badge (§8).
3. **History delta**: only the messages that are new since the previous `/v1/messages` request (§9).
4. A "Show full history (N messages)" toggle.
5. **Parameters**: `model`, `max_tokens`, `temperature`, `thinking`, `stream`, `metadata` and
   any other top-level keys, shown as a small key/value table.
6. A **Raw** tab with the full request JSON in the JSON viewer (§12.3) and the headers.

**Response side:**
1. The assembled assistant message, with content blocks rendered by type (§12.1).
2. `usage` and `stop_reason`.
3. A **Raw** tab with the ordered list of SSE events (event name, timestamp offset, data in the
   JSON viewer) and the response headers.

---

## 7. System instructions (MVP)

- The client computes a short hash of the normalised `system` field, which may be a string or
  an array of blocks, and gives each distinct value a version label: S1, S2, …
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

- **Delta rule.** For request N, find the longest prefix of `messages` that matches the previous
  `/v1/messages` request, comparing messages structurally. Show only the messages after that
  prefix, normally the previous assistant turn plus the new user turn or tool results.
- If the prefix does not match, for example because history was compacted or rewritten, or
  because a new conversation started, the card shows a **"History rewritten"** notice and
  shows the full history.
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
| unknown types          | Shown in the JSON viewer, labelled with the type                     |

Roles have distinct colours on the left edge of each message: user, assistant and system.

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

- **Search box:** plain, case-insensitive substring search over every text in requests and
  responses, including tool inputs and results. Only matching cards are shown, and the matches
  inside them are highlighted.
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
| Orphan tool result or pending tool call | pairing index (§10)                         |
| Body that is not valid JSON             | shown raw with a "not JSON" warning         |

For an API error, the card shows the error `type` and `message` prominently, plus the
`retry-after` header and the request id when present.

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

## 19. Open questions (deferred)

- Show estimated cost per exchange? This needs a pricing table that is maintained by hand.
- Also support the Codex / OpenAI Responses format (`--upstream …/codex`)? The event model is
  generic, but the content rendering here is specific to Anthropic.
- **[Stretch]** Show a side-by-side diff between any two requests, not just consecutive ones?
