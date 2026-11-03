# SPEC history: Live Agent-Traffic Dashboard

How the specification changed from the pre-lab design to what Build 1 actually implements.
The current full specification is **`SPEC_LAST.md`**. The original is kept unchanged as
**`SPEC.md`**.

| Version | Date       | Source                                   | Summary |
|---------|------------|------------------------------------------|---------|
| v1.0    | 2026-11-03 | Pre-lab design, commit `cba1a15` (`main`) | Original spec: MVP plus stretch goals. Nothing implemented. |
| v1.1    | 2026-11-03 | Build 1 implementation                   | Technical adjustments found while building and testing against real Claude Code traffic. |
| v1.2    | 2026-11-03 | User feedback on Build 1                 | The user's question must be clearly visible. Added user-prompt extraction and display. |

Build 1 code: branch `build-1`, commit `fa15c7b` on `eldarbai1/Lab4-Agentic-Programming`,
covering v1.1 and v1.2.

---

## v1.0: original spec (pre-lab)

- Live dashboard served by `proxy.py` on `:8042`, with SSE `/events` and an in-memory buffer.
- MVP: 11 features (SSE, newest-first cards, system/tool versions, history delta, tool pairing,
  collapsing, JSON viewer, filters, errors, basic stats).
- Stretch: persistence and sessions, replay, diffs, advanced stats, keyboard shortcuts,
  regex search, performance work.

See `SPEC.md`.

---

## v1.1: changes made during Build 1

Each entry gives what v1.0 said, what changed, and why. Most of these were forced by how
real Claude Code traffic looks. They were found by running `claude -p` through the proxy.

### 1. History delta: base request (§9)
- **v1.0:** compare with the *previous* `/v1/messages` request. If the prefix doesn't match,
  show "History rewritten".
- **v1.1:** compare with whichever of the last 80 messages requests is a *full* prefix of
  this request, taking the longest. "History rewritten" appears only when an earlier request
  shares a longer *partial* prefix. A separate "new conversation" notice appears when nothing
  is shared.
- **Why:** Claude Code interleaves side requests (title generation, quota checks) with the
  main conversation. With v1.0's rule, every turn after a side request would show
  "rewritten".

### 2. History delta: normalisation (§9)
- **v1.0:** compare messages "structurally".
- **v1.1:** before hashing, remove `cache_control` keys and treat `content: "x"` the same as
  `[{type:"text", text:"x"}]`.
- **Why:** this came from real traffic. In request #2 a message was
  `[{type:"text", text, cache_control}]`. In request #3 the same message was sent as a plain
  string, because its cache marker had moved on. Without normalisation, the second request
  of every conversation showed "History rewritten (shares 1 of 2)".

### 3. Event delivery to browsers (§3)
- **v1.0:** each SSE client gets a bounded per-client queue, and a slow client loses events.
- **v1.1:** the proxy thread only enqueues raw observations (O(1)). A single worker thread
  builds and stores events. Each SSE client reads the shared list with its own cursor.
- **Why:** this meets the same goal (a slow tab never stalls the agent) more simply, and a
  slow tab now falls behind instead of losing events. It also takes JSON and SSE parsing off
  the agent's path entirely.

### 4. SSE event ids (§4)
- **v1.0:** the `id` is the event's index.
- **v1.1:** the SSE `id:` is `<session_id>:<index>`. A `Last-Event-ID` from another session
  replays from 0, and the browser clears its state on a new `session_id`.
- **Why:** with bare indexes, a tab that reconnects to a *restarted* proxy would ask for
  "after index 500" and silently miss the new session.

### 5. Event payload additions (§5)
- `request` and `response_body` gained `body_is_json`, so the "not JSON" warning can be shown
  for a body that was a string.
- `proxy_error` gained an optional `status` (502), `total_ms` and `bytes`. Its stages are
  `upstream_connect`, `upstream_request`, `upstream_read` and `client_write`.
- Redaction was extended to `set-cookie` and `proxy-authorization`, and is applied to
  response headers too.

### 6. "Messages only" definition (§5, §13)
- **v1.0:** paths other than `/v1/messages` are hidden.
- **v1.1:** compare the path *without the query string*.
- **Why:** Claude Code calls `/v1/messages?beta=true`, which would otherwise be hidden.

### 7. Pending tool calls are not errors (§10, §14)
- **v1.0:** the errors table lists "orphan tool result **or pending tool call**".
- **v1.1:** pending calls show a spinner but are not counted. Orphans and `is_error`
  results are counted.
- **Why:** every tool call is pending until the next request arrives, so counting them
  would make the error badge flicker on every turn.

### 8. Search scope (§13)
- **v1.0:** search "every text in requests and responses".
- **v1.1:** search the card's new messages, its parameters, the response and the errors.
  The system prompt and tools are searched only on the card where that version first
  appears.
- **Why:** every request repeats the full system prompt, tools and history. Searching all of
  it makes every card match common words, such as a tool name.

### 9. System/tool versions when switching back (§7, §8)
- **v1.0:** only "unchanged" or "changed from S1" is defined.
- **v1.1:** added a third case: a version seen before but different from the previous
  request reads `System S1 (changed from S2; first seen in #3)` and stays collapsed.
- **Why:** side requests use a different system prompt, so the main prompt keeps switching
  back.

### 10. Smaller additions
- The newest card is auto-expanded, but the previous one is auto-collapsed only if the user
  is at the top and hasn't toggled it, so a card the user is reading never collapses under
  them.
- Card bodies are built only on first expand. This is a cheap part of the §12.4 stretch
  goal, and it keeps late-join replay fast.
- Rendered/Raw tabs on both columns. Streaming blocks show their raw text live, then switch
  to the final rendering at `content_block_stop`.
- Real-traffic content handled: `role: "system"` messages inside `messages`, and unknown
  block types such as `tool_addition`.
- The `retry-after` and request id lines appear only for HTTP or stream errors.
- `index.html` is read from disk on every page load, so frontend changes need only a browser
  reload.

---

## v1.2: user prompt shown clearly (user feedback)

- **Feedback:** "make it so that I'll see the user question clearly, cause I don't see it."
- **Cause:**
  - Claude Code puts the typed question in the same user message as several
    `<system-reminder>` text blocks, with the question last.
  - The message preview showed the first block, which is reminder text.
  - Each block was cut at 20 lines, so the question was hidden under several screens of
    context.
- **Change** (new §6.3, MVP item 12):
  - The prompt is extracted from the `text` blocks of user messages, with
    `<system-reminder>…</system-reminder>` sections removed. Tool results never count.
  - The card's prompt is the last one among its new messages.
  - It is shown on its own `💬` line on the collapsed card, in a highlighted
    "User prompt" box at the top of the Request column, and as the history message preview.
  - Text blocks that contain only reminders collapse to a one-line
    `system-reminder (N chars)` row.
- **Acceptance criterion 8** was added for this.

---

## Not done yet (carried forward)

All **[Stretch]** items in `SPEC_LAST.md` are still open:
- persistence to `sessions/*.jsonl`, the session picker and export
- replay
- system, tool and history diffs, plus the Library panel
- conversation thread lanes
- the Tool calls panel
- advanced statistics
- keyboard shortcuts
- regex search, auto-opening matches, extra filters and URL-hash state
- the Pause button and frame-batched redraws beyond what exists
- a bounded event buffer

Open questions from §19 (cost estimates, OpenAI Responses format, arbitrary request diffs)
are unchanged.

**Known limits of Build 1:**
- The in-memory event list is unbounded for the life of the proxy process.
- Search highlighting covers only content that is already rendered (expanded cards).
- Pasted images in a prompt appear as placeholders in the message, not in the prompt box.
