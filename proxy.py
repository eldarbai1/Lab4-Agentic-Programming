#!/usr/bin/env python3
"""proxy.py: the naive agent-traffic proxy for Lab 4, plus a live dashboard.

A reverse proxy that prints every request and response and forwards them
unchanged. Standard library only; nothing to install.

    python3 proxy.py                       # listens on http://localhost:8041
    ANTHROPIC_BASE_URL=http://localhost:8041 claude

Everything Claude Code sends to the API now passes through this program and is
printed to the terminal. The proxy changes nothing: headers go through untouched
(a subscription login needs its bearer token and its `anthropic-beta` header),
and the reply is streamed back byte for byte as it arrives.

The same process also serves a browser dashboard (see SPEC.md) on
http://localhost:8042 that shows the traffic live, newest exchange first.

    python3 proxy.py --port 9000
    python3 proxy.py --dashboard-port 9001 --quiet
    python3 proxy.py --upstream https://chatgpt.com/backend-api/codex   # for Codex
"""

from __future__ import annotations

import argparse
import http.client
import itertools
import json
import queue
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

# Headers that describe one hop of the connection, not the message. They are
# never forwarded. `accept-encoding` is dropped too, so the API answers in plain
# text and what we print is readable.
SKIP = {
    "host", "connection", "keep-alive", "transfer-encoding", "te", "trailers",
    "upgrade", "proxy-authorization", "proxy-authenticate", "accept-encoding",
    "content-length",
}

UPSTREAM = "https://api.anthropic.com"
PROXY_VERSION = "lab4-build1"
QUIET = False
INDEX_HTML = Path(__file__).with_name("index.html")

# Headers whose values are secrets: cut before they reach the dashboard.
SECRET_HEADERS = {"authorization", "x-api-key", "cookie", "set-cookie", "proxy-authorization"}
MAX_RESPONSE_BODY = 8 * 1024 * 1024  # non-streaming bodies kept for the dashboard


def stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def say(*args: object, **kwargs: object) -> None:
    """Terminal output, silenced by --quiet."""
    if not QUIET:
        print(*args, **kwargs)  # type: ignore[arg-type]


def redact(headers: list[tuple[str, str]]) -> list[list[str]]:
    out = []
    for name, value in headers:
        if name.lower() in SECRET_HEADERS:
            value = value[:16] + "…"
        out.append([name, value])
    return out


def parse_body(raw: bytes) -> tuple[object, bool]:
    """(parsed JSON or raw string, was it JSON?). Empty body -> (None, True)."""
    if not raw:
        return None, True
    try:
        return json.loads(raw), True
    except ValueError:
        return raw.decode("utf-8", "replace"), False


# --------------------------------------------------------------------------- #
# Event bus
#
# The proxy thread only does `inbox.put(...)` (O(1), never blocks). A single
# worker thread turns those raw observations into structured events (JSON
# parsing, SSE parsing, redaction, serialisation) and appends them to the
# in-memory list. Each SSE client reads that list at its own pace with its own
# cursor, so a slow or closed browser tab can never stall the agent.
# --------------------------------------------------------------------------- #


class Bus:
    def __init__(self, upstream: str) -> None:
        self.session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.events: list[str] = []          # serialised JSON, index == event id
        self.cond = threading.Condition()
        self.inbox: queue.SimpleQueue = queue.SimpleQueue()
        self.streams: dict[int, dict] = {}   # per-exchange response state (worker only)
        self._ids = itertools.count(1)
        self._ids_lock = threading.Lock()
        threading.Thread(target=self._run, name="dashboard-bus", daemon=True).start()
        self.emit("session_start", 0, session_id=self.session_id, upstream=upstream,
                  proxy_version=PROXY_VERSION)

    def next_exchange(self) -> int:
        with self._ids_lock:
            return next(self._ids)

    def emit(self, type_: str, exchange_id: int, **payload: object) -> None:
        try:
            self.inbox.put((type_, exchange_id, iso_now(), payload))
        except Exception:  # noqa: BLE001 - the dashboard must never break the proxy
            pass

    # -- worker ------------------------------------------------------------ #

    def _run(self) -> None:
        while True:
            item = self.inbox.get()
            try:
                self._handle(*item)
            except Exception as e:  # noqa: BLE001
                print(f"[dashboard] event error: {e!r}", file=sys.stderr, flush=True)

    def _handle(self, type_: str, xid: int, ts: str, p: dict) -> None:
        if type_ == "request":
            body, ok = parse_body(p.pop("raw_body"))
            p["headers"] = redact(p["headers"])
            p["body"] = body
            p["body_is_json"] = ok
        elif type_ == "response_start":
            ctype = next((v for k, v in p["headers"] if k.lower() == "content-type"), "")
            self.streams[xid] = {"sse": "text/event-stream" in ctype, "buf": b"", "body": bytearray()}
            p["headers"] = redact(p["headers"])
        elif type_ == "chunk":
            st = self.streams.get(xid)
            if st is None:
                return
            if st["sse"]:
                st["buf"] = (st["buf"] + p["data"]).replace(b"\r\n", b"\n")
                while b"\n\n" in st["buf"]:
                    block, st["buf"] = st["buf"].split(b"\n\n", 1)
                    self._sse_block(xid, ts, block)
            elif len(st["body"]) < MAX_RESPONSE_BODY:
                st["body"] += p["data"]
            return  # raw chunks are not events themselves
        elif type_ in ("response_end", "proxy_error"):
            st = self.streams.pop(xid, None)
            if st is not None:
                if st["sse"]:
                    if st["buf"].strip():
                        self._sse_block(xid, ts, st["buf"])
                elif type_ == "response_end" and st["body"]:
                    body, ok = parse_body(bytes(st["body"]))
                    self._publish("response_body", xid, ts, {"body": body, "body_is_json": ok})
        self._publish(type_, xid, ts, p)

    def _sse_block(self, xid: int, ts: str, block: bytes) -> None:
        event, data_lines = None, []
        for line in block.decode("utf-8", "replace").split("\n"):
            if not line or line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                event = value
            elif field == "data":
                data_lines.append(value)
        if event is None and not data_lines:
            return
        text = "\n".join(data_lines)
        try:
            data: object = json.loads(text)
        except ValueError:
            data = text
        self._publish("stream_event", xid, ts, {"event": event, "data": data})

    def _publish(self, type_: str, xid: int, ts: str, payload: dict) -> None:
        idx = len(self.events)  # single writer (the worker), so this is stable
        ev = {"id": idx, "type": type_, "exchange_id": xid, "ts": ts, **payload}
        line = json.dumps(ev, ensure_ascii=False, default=str)
        with self.cond:
            self.events.append(line)
            self.cond.notify_all()


BUS: Bus | None = None


def emit(type_: str, exchange_id: int, **payload: object) -> None:
    if BUS is not None:
        BUS.emit(type_, exchange_id, **payload)


# --------------------------------------------------------------------------- #
# Dashboard HTTP server (127.0.0.1 only)
# --------------------------------------------------------------------------- #


class Dashboard(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self.serve_index()
        elif path == "/events":
            self.serve_events()
        else:
            self.send_error(404)

    def serve_index(self) -> None:
        try:
            data = INDEX_HTML.read_bytes()
        except OSError as e:
            self.send_error(500, f"cannot read {INDEX_HTML}: {e}")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def serve_events(self) -> None:
        bus = BUS
        assert bus is not None
        # Last-Event-ID is "<session_id>:<index>". A different session (proxy
        # restarted) means the client must get everything from the start.
        cursor = 0
        last = self.headers.get("Last-Event-ID", "")
        sid, _, num = last.rpartition(":")
        if sid == bus.session_id and num.isdigit():
            cursor = int(num) + 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while True:
                with bus.cond:
                    if cursor >= len(bus.events):
                        bus.cond.wait(timeout=15)
                    batch = bus.events[cursor:cursor + 500]
                if not batch:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                out = []
                for line in batch:
                    out.append(f"id: {bus.session_id}:{cursor}\ndata: {line}\n\n")
                    cursor += 1
                self.wfile.write("".join(out).encode("utf-8"))
                self.wfile.flush()
        except (OSError, ValueError):
            return  # browser went away


# --------------------------------------------------------------------------- #
# The proxy (behaviour unchanged; it only reports what it sees to the bus)
# --------------------------------------------------------------------------- #


class Tap(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # we print our own lines

    def do_POST(self) -> None:
        self.relay()

    def do_GET(self) -> None:
        self.relay()

    def do_HEAD(self) -> None:
        self.relay()

    def relay(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        t0 = time.monotonic()
        xid = BUS.next_exchange() if BUS else 0
        emit("request", xid, method=self.command, path=self.path,
             headers=list(self.headers.items()), raw_body=body)

        # 1. Print what the agent sent.
        say(f"\n{'=' * 78}\n[{stamp()}] >>> {self.command} {self.path}", flush=True)
        if not QUIET:
            for name, value in self.headers.items():
                if name.lower() in ("authorization", "x-api-key"):
                    value = value[:16] + "..."
                print(f"    {name}: {value}")
            if body:
                try:
                    print(json.dumps(json.loads(body), indent=2, ensure_ascii=False))
                except ValueError:
                    print(body.decode("utf-8", "replace"))

        # 2. Forward it, headers intact.
        target = urlsplit(UPSTREAM)
        conn_cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        # Connect with a short timeout (a lost SYN must not stall the agent for
        # minutes); the long timeout is for reading a slow stream.
        upstream = conn_cls(target.netloc, timeout=10)
        try:
            upstream.connect()
        except OSError as e:
            say(f"\n[{stamp()}] !!! cannot reach {UPSTREAM}: {e}", flush=True)
            emit("proxy_error", xid, stage="upstream_connect", message=f"cannot reach {UPSTREAM}: {e}",
                 status=502, total_ms=round((time.monotonic() - t0) * 1000))
            self.send_error(502, f"proxy: cannot reach {UPSTREAM}: {e}")
            self.close_connection = True
            return
        upstream.sock.settimeout(600)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in SKIP}
        try:
            upstream.request(self.command, target.path.rstrip("/") + self.path, body=body or None, headers=headers)
            reply = upstream.getresponse()
        except Exception as e:
            emit("proxy_error", xid, stage="upstream_request", message=f"{type(e).__name__}: {e}")
            raise
        emit("response_start", xid, status=reply.status, reason=reply.reason,
             headers=reply.getheaders(), ttfb_ms=round((time.monotonic() - t0) * 1000))

        # 3. Stream the reply back as it arrives, printing it on the way.
        say(f"\n[{stamp()}] <<< {reply.status} {reply.reason}", flush=True)
        stage = "client_write"
        nbytes = 0
        try:
            self.send_response(reply.status, reply.reason)
            for name, value in reply.getheaders():
                if name.lower() not in SKIP and name.lower() != "content-encoding":
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command == "HEAD":
                self.close_connection = True
                emit("response_end", xid, total_ms=round((time.monotonic() - t0) * 1000), bytes=0)
                return
            while True:
                stage = "upstream_read"
                chunk = reply.read1(4096)  # read1: pass on each chunk as it lands, don't wait for 4096 bytes
                stage = "client_write"
                if not chunk:
                    break
                emit("chunk", xid, data=chunk)
                nbytes += len(chunk)
                self.wfile.write(chunk)
                self.wfile.flush()
                if not QUIET:
                    sys.stdout.write(chunk.decode("utf-8", "replace"))
                    sys.stdout.flush()
        except Exception as e:
            emit("proxy_error", xid, stage=stage, message=f"{type(e).__name__}: {e}",
                 total_ms=round((time.monotonic() - t0) * 1000), bytes=nbytes)
            raise
        self.close_connection = True
        emit("response_end", xid, total_ms=round((time.monotonic() - t0) * 1000), bytes=nbytes)
        say(f"\n[{stamp()}] --- done", flush=True)


def main() -> None:
    global UPSTREAM, QUIET, BUS
    parser = argparse.ArgumentParser(description="Print and forward an agent's API traffic.")
    parser.add_argument("--port", type=int, default=8041)
    parser.add_argument("--upstream", default=UPSTREAM, help="where to forward (default: the Anthropic API)")
    parser.add_argument("--dashboard-port", type=int, default=8042, help="browser dashboard port (default 8042)")
    parser.add_argument("--quiet", action="store_true", help="do not print traffic to the terminal")
    args = parser.parse_args()
    UPSTREAM = args.upstream.rstrip("/")
    QUIET = args.quiet

    BUS = Bus(UPSTREAM)
    dash = ThreadingHTTPServer(("127.0.0.1", args.dashboard_port), Dashboard)
    dash.daemon_threads = True
    threading.Thread(target=dash.serve_forever, name="dashboard", daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Tap)
    server.daemon_threads = True
    print(f"proxy listening on http://localhost:{args.port}  ->  {UPSTREAM}")
    print(f"run the agent with:  ANTHROPIC_BASE_URL=http://localhost:{args.port} claude")
    print(f"dashboard: http://localhost:{args.dashboard_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
