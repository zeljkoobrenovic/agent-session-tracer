#!/usr/bin/env python3
"""Live trace page: a local server that follows the latest Claude Code session of a folder and serves the trace page,
which refreshes it every few seconds while the session goes on.

Usage:
  serve.py                                  # the folder you are in: its latest session, on http://localhost:8765
  serve.py --cwd /path/you/ran/claude/in    # another folder
  serve.py --session f8fc5201               # pin one session (id prefix) instead of following the latest
  ... [--port 8765] [--interval 3] [--root ..] [--source-root ..] [--title ..] [--no-browser]

The page is template.html with no examples embedded; /trace.json is the trace of the transcript, re-extracted only
when the transcript file changes (ETag, so an unchanged poll costs nothing); /trace.json?session=<id prefix> is any
session of the folder, and /sessions.json lists them for the page's session switcher. /sessions is the sessions table
of the folder (sessions_template.html, fed by /stats.json), each row opening its session in the trace page.
Python 3.8+, no dependencies, local only.
"""
import argparse
import datetime
import hashlib
import json
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import turn_trace

HERE = os.path.dirname(os.path.abspath(__file__))


class Tracer:
    """Traces of the folder's transcripts, each re-extracted only when its file changes."""

    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.docs = {}  # path -> ((path, mtime, size), trace document)
        self.traces = {}  # path -> ((path, mtime, size), payload, etag)
        self.summaries = {}  # path -> ((mtime, size), summary) for /sessions.json
        self.stats = {}  # path -> ((path, mtime, size), row) for /stats.json

    def pick(self, session=None):
        """The transcript to serve: the given session (id prefix), else the one pinned with --session, else the latest."""
        files = turn_trace.transcripts_for(self.args.cwd)
        want = session or self.args.session
        if want:
            files = [f for f in files if os.path.basename(f).startswith(want)]
        return files[-1] if files else None

    def trace(self, session=None):
        path = self.pick(session)
        if path is None:
            what = f"session {session or self.args.session}*" if (session or self.args.session) else "a session"
            doc = {"error": f"no transcript of {what} started in {self.args.cwd} yet; start claude there, or pass --cwd"}
            return json.dumps(doc).encode("utf-8"), '"none"'
        with self.lock:
            key, doc = self.doc(path)
            entry = self.traces.get(path)
            if entry is None or entry[0] != key:
                etag = '"' + hashlib.md5(repr(key).encode()).hexdigest()[:16] + '"'
                entry = self.traces[path] = (key, json.dumps(doc).encode("utf-8"), etag)
            return entry[1], entry[2]

    def doc(self, path):
        """The trace document of a transcript, re-extracted when the file changed (call with the lock held)."""
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
        entry = self.docs.get(path)
        if entry is None or entry[0] != key:
            sid = os.path.basename(path)[:8]
            title = self.args.title or f"{os.path.basename(self.args.cwd.rstrip('/'))} \u00B7 session {sid}"
            doc = turn_trace.trace_for(path, root=self.args.root, source_root=self.args.source_root, title=title)
            doc["live"] = {"transcript": path, "updated": datetime.datetime.now().isoformat(timespec="seconds")}
            print(f"{time.strftime('%H:%M:%S')}  {sid}  {len(doc['turns'])} turns, {len(doc['files'])} files", flush=True)
            entry = self.docs[path] = (key, doc)
        return entry

    def rows(self):
        """The sessions table of the folder (/stats.json): one row of totals per transcript, newest first, each
        recomputed only when its file changed; a row links to the live trace page of its session."""
        out = []
        with self.lock:
            for f in reversed(turn_trace.transcripts_for(self.args.cwd)):
                key, doc = self.doc(f)
                entry = self.stats.get(f)
                if entry is None or entry[0] != key:
                    row = turn_trace.session_stats(doc, f)
                    row["page"] = "./?session=" + row["id"]
                    entry = self.stats[f] = (key, row)
                out.append(entry[1])
        return json.dumps({"updated": datetime.datetime.now().isoformat(timespec="seconds"), "sessions": out}).encode("utf-8")

    def sessions(self):
        """The folder's sessions, newest first, each summarized once per change of its file."""
        out = []
        with self.lock:
            for f in reversed(turn_trace.transcripts_for(self.args.cwd)):
                st = os.stat(f)
                key = (st.st_mtime_ns, st.st_size)
                entry = self.summaries.get(f)
                if entry is None or entry[0] != key:
                    summary = turn_trace.session_summary(f)
                    summary.pop("path", None)
                    entry = self.summaries[f] = (key, summary)
                out.append(entry[1])
        return json.dumps({"following": self.args.session, "sessions": out}).encode("utf-8")


def page(args):
    live = {"interval": args.interval, "cwd": args.cwd, "session": args.session}
    nav = '<p class="nav"><a href="sessions">\u2190 all sessions of this folder</a></p>'
    return turn_trace.fill_template("template.html", live=live, nav=nav, note="Live from the transcripts in ~/.claude/projects.").encode("utf-8")


def sessions_page(args):
    live = {"interval": 30, "cwd": args.cwd}
    return turn_trace.fill_template("sessions_template.html", live=live,
                                    note="Live from the transcripts in ~/.claude/projects; a row opens the session in the live trace page.").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    tracer = None
    args = None

    def send(self, status, ctype, body, etag=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        path, query = url.path, parse_qs(url.query)
        if path in ("/", "/index.html"):
            self.send(200, "text/html; charset=utf-8", page(self.args))
        elif path in ("/sessions", "/sessions.html"):
            self.send(200, "text/html; charset=utf-8", sessions_page(self.args))
        elif path == "/stats.json":
            self.send(200, "application/json; charset=utf-8", self.tracer.rows())
        elif path == "/sessions.json":
            self.send(200, "application/json; charset=utf-8", self.tracer.sessions())
        elif path == "/trace.json":
            session = (query.get("session") or [""])[0]
            if session and not all(c.isalnum() or c == "-" for c in session):
                self.send(400, "text/plain", b"bad session id")
                return
            body, etag = self.tracer.trace(session or None)
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
            else:
                self.send(200, "application/json; charset=utf-8", body, etag)
        elif path == "/claude.png":
            try:
                with open(os.path.join(HERE, "docs", "claude.png"), "rb") as f:
                    self.send(200, "image/png", f.read())
            except OSError:
                self.send(404, "text/plain", b"not found")
        else:
            self.send(404, "text/plain", b"not found")

    def log_message(self, fmt, *a):  # only failures; the tracer prints when the trace changes
        if a and str(a[1:2] and a[1]).startswith(("4", "5")):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % a))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cwd", default=os.getcwd(), help="folder the session was started in (default: the current folder)")
    ap.add_argument("--session", help="follow this session (id or prefix) instead of the latest one; see turn_trace.py --list")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--interval", type=float, default=3, help="seconds between refreshes of the page (default 3)")
    ap.add_argument("--root", help="path prefix to strip from file paths (default: the session's cwd)")
    ap.add_argument("--source-root", help="where the files are now, for their total line counts (default: --root)")
    ap.add_argument("--title")
    ap.add_argument("--no-browser", action="store_true", help="do not open the page in a browser")
    args = ap.parse_args()
    args.cwd = os.path.abspath(args.cwd)
    Handler.tracer, Handler.args = Tracer(args), args
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        sys.exit(f"cannot listen on port {args.port}: {e} (pass --port)")
    url = f"http://localhost:{args.port}/"
    found = turn_trace.transcripts_for(args.cwd)
    print(f"following {'session ' + args.session + '*' if args.session else 'the latest session'} of {args.cwd}"
          f" ({len(found)} transcript{'s' if len(found) != 1 else ''} so far){'' if found else ' - waiting for one'}")
    print(f"{url}  (refresh every {args.interval:g} s; Ctrl-C to stop)", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
