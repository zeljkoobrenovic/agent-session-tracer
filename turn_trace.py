#!/usr/bin/env python3
"""Turn-by-turn trace of an agent session: which files it read, searched and edited in each API call, and how many
tokens each call re-sent from the cache, wrote to it and produced.

The input is Claude Code's own session transcript, which it writes for every session (interactive or `claude -p`)
under ~/.claude/projects/<cwd slug>/<session id>.jsonl. Nothing else needs to be logged: every assistant message
carries the call's token usage (input, cache read, cache write, output) and every tool call is followed by its result.

Usage:
  turn_trace.py --cwd <folder> --list             # the sessions started in that folder (id, time, calls, prompt)
  turn_trace.py --cwd <folder> [--session <id>]   # the latest (or that) session started in that folder
  turn_trace.py --transcript <file.jsonl>         # any transcript
  ... [--root <repo root to shorten paths>] [--source-root <where the files are now>] [--out trace.json] [--title ...]

Output: one JSON document (see TRACE_FORMAT below) that the trace page (docs/index.html, built from template.html) renders:
drop the file on the page. serve.py serves the same trace live, refreshed as the session goes on.
"""
import argparse
import datetime
import glob
import json
import os
import re
import shlex
import sys
from collections import defaultdict

import codex_trace

PROJECTS = os.path.expanduser("~/.claude/projects")

TRACE_FORMAT = """
{
  "title": "...", "model": "...", "meta": {"task": ..., "variant": ..., "turns": N, ...},
  "shared_prefix": 10874,   # cache read by the first call: the static system prompt and tool definitions, shared by every session
  "turns": [ {"n": 1, "input": 2, "cache_read": 10874, "cache_write": 8434, "output": 105,
              "text": "what the agent said (first line)",
              "prompt": "the user's message this turn starts answering (first line); absent on the other turns",
              "at": "2026-10-09T19:20:08.629Z",      # when the call's response started arriving
              "seconds": {"model": 3.6, "tools": 0.8},  # from the prompt or last tool result to the end of the response; the tools' run time after it
              "result_chars": 3425,            # characters of tool results this turn received
              "new": {"prev_output": 147, "tool_results": 1489, "setup": 0, "other": 0},   # where the cache_write tokens
                                               # came from: the previous turn's output, its tool results (chars / 2.3, capped),
                                               # the session setup (first turn only: dynamic system prompt sections, listings,
                                               # CLAUDE.md, task prompt), the rest (later turns: reminders, injected instructions)
              "tools": [ {"tool": "Bash", "kind": "search|read|edit|build|vcs|shell|text", "detail": "the command or path",
                          "lines": 30,                      # lines of tool output the agent received
                          "files": [ {"path": "a/b.java", "kind": "read|edit|hit", "lines": 30} ] } ] } ],
  "files": [ {"path": "a/b.java", "lines": 1234, "first": 3, "read": 110, "edits": 2, "hits": 4} ]
}
"""

SOURCE_EXT = r"(?:java|xml|md|json|txt|properties|html|js|py|yml|yaml|sh|css|kt|gradle)"
PATH_RE = re.compile(r"(?<![\w.-])((?:\.{0,2}/)?(?:[\w@+.-]+/)*[\w@+.-]+\." + SOURCE_EXT + r")\b")
GREP_HIT_RE = re.compile(r"^(?:\d+:)?((?:[\w@+.-]+/)*[\w@+.-]+\." + SOURCE_EXT + r")(?::\d+)?[:-]")
GREP_LIST_RE = re.compile(r"^((?:[\w@+.-]+/)*[\w@+.-]+\." + SOURCE_EXT + r")$")
HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")
REDIRECT_RE = re.compile(r"(?<![<>\d&])>{1,2}\s*(['\"]?)([^\s'\"|;&<>]+)\1")
SCRIPT_CMDS = {"python", "python3", "node", "perl", "ruby", "sh", "bash", "zsh"}
WRITE_MARK_RE = re.compile(r"""open\([^)]*['"][wa]['"]|\.write\(|\.write_text\(|writeFileSync|writeFile\(|File\.write|> ?["']?[\w./$-]+\.""")
READ_CMDS = {"cat", "head", "tail", "nl", "less", "more", "bat"}
SEARCH_CMDS = {"grep", "rg", "find", "ls", "wc", "fgrep", "egrep", "ag", "tree"}
BUILD_CMDS = {"mvn", "java", "javac", "./mvnw", "make"}
CHARS_PER_TOKEN = 2.3  # measured on the experiment transcripts: (cache_write - previous output) / tool result chars, median 2.3 (code, paths, grep output)

# List prices in $ per million tokens, platform.claude.com/docs/en/about-claude/pricing as of 2026-10-09:
# [input, 5-minute cache write, 1-hour cache write, cache read, output]. Claude Code writes its cache with the 1-hour TTL.
# The pages get this table embedded (${prices}), so it is the one place to update.
PRICES_DATE = "2026-10-09"
PRICES = {
    "claude-fable-5-1": [10, 12.5, 20, 0.25, 50], "claude-mythos-5-1": [10, 12.5, 20, 0.25, 50],
    "claude-fable-5": [10, 12.5, 20, 1, 50], "claude-mythos-5": [10, 12.5, 20, 1, 50],
    "claude-opus-5-5": [4, 5, 8, 0.2, 20], "claude-opus-5": [5, 6.25, 10, 0.5, 25],
    "claude-opus-4-8": [5, 6.25, 10, 0.5, 25], "claude-opus-4-7": [5, 6.25, 10, 0.5, 25], "claude-opus-4-6": [5, 6.25, 10, 0.5, 25],
    "claude-opus-4-5": [5, 6.25, 10, 0.5, 25],
    "claude-opus-4-1": [15, 18.75, 30, 1.5, 75], "claude-opus-4": [15, 18.75, 30, 1.5, 75],
    "claude-sonnet-5-5": [2, 2.5, 4, 0.1, 10], "claude-sonnet-5": [2, 2.5, 4, 0.2, 10],
    "claude-sonnet-4-6": [3, 3.75, 6, 0.3, 15], "claude-sonnet-4-5": [3, 3.75, 6, 0.3, 15], "claude-sonnet-4": [3, 3.75, 6, 0.3, 15],
    "claude-haiku-5-5": [0.1, 0.125, 0.2, 0.01, 0.5],
    "claude-haiku-5-5:long": [0.5, 0.625, 1, 0.05, 2.5],  # Haiku 5.5: a request whose prompt exceeds 100K tokens pays this row
    "claude-haiku-4-5": [1, 1.25, 2, 0.1, 5], "claude-haiku-3-5": [0.8, 1, 1.6, 0.08, 4],
    # OpenAI models (Codex): no cache-write charge, so the uncached input (kept in cache_write for Codex traces) is at
    # the input rate; the write premium GPT-6 Astra charges on explicit cache writes is not modelled. Cached input at
    # 10% of input. gpt-5.4 and gpt-5.5 from openai.com/api/pricing; gpt-6-astra from the launch pricing (2026-10);
    # gpt-5.6-sol from one secondary source, unverified.
    "gpt-6-astra": [10, 10, 10, 1, 50], "gpt-5.6-sol": [4, 4, 4, 0.4, 20],
    "gpt-5.5": [5, 5, 5, 0.5, 30], "gpt-5.4": [2.5, 2.5, 2.5, 0.25, 15],
}
PRICE_KEYS = sorted((k for k in PRICES if ":" not in k), key=len, reverse=True)


def price_key(model):
    """The price row of a model id, also with a date suffix or a cloud prefix; None when unknown."""
    m = str(model or "")
    i = m.find("claude-")
    if i > 0:  # a cloud prefix (us.anthropic.claude-...)
        m = m[i:]
    for k in PRICE_KEYS:
        if m == k or m.startswith(k + "-"):
            return k
    return None


def turn_cost(turn, key):
    p = PRICES[key]
    if key == "claude-haiku-5-5" and turn.get("cache_read", 0) + turn.get("cache_write", 0) + turn.get("input", 0) > 100000:
        p = PRICES["claude-haiku-5-5:long"]
    return (turn.get("input", 0) * p[0] + turn.get("cache_write", 0) * p[2] + turn.get("cache_read", 0) * p[3]
            + turn.get("output", 0) * p[4]) / 1e6


def estimate_cost(turns, model):
    """What the turns cost at list prices (cache reads, 1-hour cache writes, uncached input, output); None for an unknown model."""
    key = price_key(model)
    if key is None:
        return None
    return round(sum(turn_cost(t, key) for t in turns), 6)


def claude_transcripts_for(cwd):
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(cwd))
    return sorted(glob.glob(os.path.join(PROJECTS, slug, "*.jsonl")), key=os.path.getmtime)


def transcripts_for(cwd, agent=None):
    """The transcripts of the sessions started in cwd, Claude Code's and Codex's, oldest first by modification time;
    agent = "claude" or "codex" keeps one kind."""
    files = []
    if agent in (None, "claude"):
        files += claude_transcripts_for(cwd)
    if agent in (None, "codex"):
        files += codex_trace.transcripts(cwd)
    return sorted(files, key=os.path.getmtime)


def agent_of(transcript):
    """"codex" for a Codex rollout file, else "claude"."""
    return "codex" if transcript.startswith(codex_trace.CODEX_SESSIONS) or codex_trace.is_codex_transcript(transcript) else "claude"


def transcript_id(transcript):
    return codex_trace.session_id(transcript) if agent_of(transcript) == "codex" else os.path.basename(transcript)[:-6]


def find_transcript(args):
    if args.transcript:
        return args.transcript
    files = transcripts_for(args.cwd, getattr(args, "agent", None))
    if getattr(args, "session", None):
        files = [f for f in files if transcript_id(f).startswith(args.session)]
    if not files:
            sys.exit(f"no transcript for {args.cwd} under {PROJECTS}")
    return files[-1]


def session_summary(f):
    """One transcript in a line: id, start time, API calls, first prompt, agent."""
    if agent_of(f) == "codex":
        return codex_summary(f)
    first, calls, started = "", set(), None
    for line in open(f, encoding="utf-8"):
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("type") == "user" and not first and isinstance((o.get("message") or {}).get("content"), str):
            first = o["message"]["content"].strip().splitlines()[0][:70]
            started = o.get("timestamp", "")[:16].replace("T", " ")
        elif o.get("type") == "assistant":
            calls.add((o.get("message") or {}).get("id") or o.get("requestId") or len(calls))
    return {"id": os.path.basename(f)[:-6], "started": started or "", "calls": len(calls), "prompt": first, "path": f, "agent": "claude"}


def codex_summary(f):
    meta = codex_trace.session_meta(f)
    first, records, counts = "", 0, 0
    for line in open(f, encoding="utf-8"):
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        p = o.get("payload") or {}
        if o.get("type") == "response_item" and p.get("type") == "message" and p.get("role") == "user" and not first:
            first = (codex_trace.user_text(p) or "")[:70]
        elif o.get("type") == "token_usage_record":
            records += 1
        elif o.get("type") == "event_msg" and p.get("type") == "token_count" and (p.get("info") or {}).get("last_token_usage"):
            counts += 1
    calls = records or counts  # newer files have both, one of each per API call
    started = (meta.get("timestamp") or "")[:16].replace("T", " ")
    return {"id": codex_trace.session_id(f), "started": started, "calls": calls, "prompt": first, "path": f, "agent": "codex"}


def sessions_of(cwd, agent=None):
    """The sessions started in cwd, newest first."""
    return [session_summary(f) for f in reversed(transcripts_for(cwd, agent))]


def list_sessions(cwd):
    """Print the sessions started in cwd, newest first: id, start time, API calls, first prompt."""
    for s in sessions_of(cwd):
        print(f"{s['id']}  {s['agent']:6}  {s['started']:16}  {s['calls']:4} calls  {s['prompt']}")


def result_text(block):
    content = block.get("content")
    if isinstance(content, list):
        return "".join(x.get("text", "") for x in content if isinstance(x, dict))
    return "" if content is None else str(content)


def prompt_text(o):
    """The user's own message if this transcript line is one: not a tool result, not a meta line, not command markup."""
    if o.get("type") != "user" or o.get("isMeta"):
        return None
    c = (o.get("message") or {}).get("content")
    if isinstance(c, str):
        text = c
    elif isinstance(c, list) and c and all(isinstance(b, dict) and b.get("type") == "text" for b in c):
        text = "".join(b.get("text", "") for b in c)
    else:
        return None
    text = text.strip()
    if not text or text.startswith("<"):  # <command-name>, <local-command-stdout> and other injected markup
        return None
    return text.splitlines()[0][:200]


def when(o):
    """The line's timestamp as seconds since the epoch, if it has one."""
    s = o.get("timestamp")
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def load(transcript):
    """Group the transcript into API calls (one per assistant message id) with their tool results attached.
    A turn that follows a message typed by the user carries it as "prompt". Timing: a turn starts at the prompt or
    tool result that triggered it, its model time ends with its last streamed block, its tool time with its last
    tool result."""
    turns, by_id, results, cwd, model, pending = [], {}, {}, None, "", None
    use_turn, last_user = {}, None
    for line in open(transcript, encoding="utf-8"):
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("cwd") and not cwd:
            cwd = o["cwd"]
        t = o.get("type")
        now = when(o)
        prompt = prompt_text(o)
        if prompt:
            pending = prompt
            if now:
                last_user = now
        if t == "assistant" and not model and (o.get("message") or {}).get("model") and not o["message"]["model"].startswith("<"):
            model = o["message"]["model"]  # "<synthetic>" marks the CLI's own messages, not a model
        m = o.get("message") or {}
        content = m.get("content")
        if t == "assistant" and isinstance(content, list):
            mid = m.get("id") or o.get("requestId") or str(len(turns))
            turn = by_id.get(mid)
            if turn is None:
                u = m.get("usage") or {}
                turn = {"n": len(turns) + 1, "input": u.get("input_tokens", 0),
                        "cache_read": u.get("cache_read_input_tokens", 0),
                        "cache_write": u.get("cache_creation_input_tokens", 0),
                        "output": u.get("output_tokens", 0), "text": "", "tools": [], "_uses": []}
                if pending:
                    turn["prompt"], pending = pending, None
                turn["_start"], turn["at"] = last_user, o.get("timestamp")
                by_id[mid] = turn
                turns.append(turn)
            if now:
                turn["_model_end"] = max(turn.get("_model_end") or 0, now)
            for b in content:
                if b.get("type") == "tool_use":
                    turn["_uses"].append(b)
                    use_turn[b.get("id")] = turn
                elif b.get("type") == "text" and b.get("text", "").strip() and not turn["text"]:
                    turn["text"] = b["text"].strip().splitlines()[0][:200]
        elif t == "user" and isinstance(content, list):
            for b in content:
                if b.get("type") == "tool_result":
                    results[b.get("tool_use_id")] = {"text": result_text(b), "error": bool(b.get("is_error")),
                                                     "structured": o.get("toolUseResult")}
                    tr = use_turn.get(b.get("tool_use_id"))
                    if tr and now:
                        tr["_tools_end"] = max(tr.get("_tools_end") or 0, now)
            if now:
                last_user = now
    for turn in turns:
        start, mend, tend = turn.pop("_start", None), turn.pop("_model_end", None), turn.pop("_tools_end", None)
        if start and mend and mend >= start:
            turn["seconds"] = {"model": round(mend - start, 1), "tools": round(max(0.0, tend - mend), 1) if tend else 0.0}
    return turns, results, cwd, model


def rel(path, root):
    path = path.strip().strip("'\"")
    if root and path.startswith(root):
        path = path[len(root):].lstrip("/")
    if path.startswith("./"):
        path = path[2:]
    return os.path.normpath(path) if path else path


def read_lines_hint(argv):
    """Lines a read command asks for, when it says so (sed -n A,Bp; head -N; tail -N)."""
    cmd = argv[0] if argv else ""
    if cmd == "sed":
        m = re.search(r"(\d+),(\d+)p", " ".join(argv))
        if m:
            return max(0, int(m.group(2)) - int(m.group(1)) + 1)
        m = re.search(r"(\d+)p\b", " ".join(argv))
        if m:
            return 1
    if cmd in ("head", "tail"):
        m = re.search(r"-n?\s*(\d+)", " ".join(argv[1:]))
        if m:
            return int(m.group(1))
        return 10
    return None


def split_heredocs(command):
    """Cut the heredoc bodies out of a command. Returns the command with each `<<EOF` followed by a __HEREDOCn__ marker
    instead of its body, and the bodies, so that the shell code is split into segments without the bodies' text."""
    lines, kept, bodies, i = command.split("\n"), [], [], 0
    while i < len(lines):
        line = lines[i]
        m = HEREDOC_RE.search(line)
        if not m:
            kept.append(line)
            i += 1
            continue
        delim, body = m.group(2), []
        i += 1
        while i < len(lines) and lines[i].strip() != delim:
            body.append(lines[i])
            i += 1
        i += 1  # the delimiter line
        bodies.append("\n".join(body))
        kept.append(line + f" __HEREDOC{len(bodies) - 1}__")
    return "\n".join(kept), bodies


def analyze_bash(command, out, root, cwd=""):
    """Classify a shell command and attribute its output to files.

    Returns (kind, files, lines, cwd): kind is the command's dominant kind, files a list of {path, kind, lines},
    cwd the working directory after the command (Claude Code keeps it between Bash calls).
    Pipelines are split on ; && || | and each segment classified by its first word; a leading `cd` moves the
    cwd for the relative paths that follow. Read segments (cat/sed/head/tail) get the output lines, split by
    what each asked for when the command says so; search segments (grep/rg/find) are attributed through the
    output: every `path:line:` or bare path line is a hit on that file."""
    kinds, files = [], []
    reads = []
    command, bodies = split_heredocs(command)
    segments = re.split(r"\s*(?:&&|\|\||;|\||\n)\s*", command.strip())
    for seg in segments:
        seg = seg.strip()
        hd = re.search(r"__HEREDOC(\d+)__", seg)
        body = bodies[int(hd.group(1))] if hd else None
        if hd:
            seg = seg.replace(hd.group(0), "").strip()
        if not seg:
            continue
        try:
            argv = shlex.split(seg)
        except ValueError:
            argv = seg.split()
        if not argv:
            continue
        cmd = argv[0]
        if cmd == "cd" and len(argv) > 1:
            target = rel(argv[1], root)
            cwd = target if os.path.isabs(argv[1]) or not cwd else os.path.normpath(os.path.join(cwd, target))
            if cwd == ".":
                cwd = ""
            continue
        paths = [rel(os.path.join(cwd, p) if cwd and not os.path.isabs(p) else p, root) for p in PATH_RE.findall(seg)
                 if "*" not in p]
        paths = [p for p in paths if p and not p.startswith("..")]
        # writes: `... > path` / `>> path` (a heredoc, echo, printf), `tee path`, and a script fed through a heredoc that
        # writes files (open(p, "w"), .write(...)): the paths named in the script count as its edits
        rd = REDIRECT_RE.search(seg)
        target = rd.group(2).strip("'\"") if rd else None
        if target and not PATH_RE.fullmatch(target):
            target = None
        if target or (cmd == "tee" and paths):
            written = [rel(os.path.join(cwd, target) if cwd and not os.path.isabs(target) else target, root)] if target else paths
            n_lines = len(body.splitlines()) if body is not None else (1 if cmd in ("echo", "printf") else None)
            kinds.append("edit")
            for p in written:
                if p and not p.startswith(".."):
                    files.append({"path": p, "kind": "edit", "lines": n_lines})
            continue
        if body is not None and cmd in SCRIPT_CMDS:
            if WRITE_MARK_RE.search(body):
                kinds.append("edit")
                seen = set()
                for p in PATH_RE.findall(body):
                    p = rel(os.path.join(cwd, p) if cwd and not os.path.isabs(p) else p, root)
                    if p and "*" not in p and not p.startswith("..") and p not in seen:
                        seen.add(p)
                        files.append({"path": p, "kind": "edit", "lines": None})
            else:
                kinds.append("shell")
            continue
        if cmd == "sed" and "-i" in argv[1:3]:
            kind = "edit"
        elif cmd in READ_CMDS or (cmd == "sed" and "-n" in argv):
            if not paths:
                continue  # a piped head/tail/cat is a filter, not a read
            kind = "read"
        elif cmd in SEARCH_CMDS:
            kind = "search"
        elif cmd in BUILD_CMDS or cmd.endswith("mvn"):
            kind = "build"
        elif cmd == "git":
            kind = "vcs"
        elif cmd in ("echo", "printf", "python3", "python", "awk", "xargs", "sort", "uniq", "cut", "tr", "diff", "patch", "cp", "mv", "mkdir", "rm", "touch", "test", "true"):
            kind = "shell"
        else:
            kind = "shell"
        kinds.append(kind)
        if kind in ("read", "edit") and paths:
            for p in paths:
                entry = {"path": p, "kind": kind, "lines": None}
                files.append(entry)
                if kind == "read":
                    reads.append((entry, read_lines_hint(argv)))
    # Output lines: to the read segments (by their stated sizes, remainder to the unsized ones).
    total = len(out.splitlines())
    if reads:
        stated = [(e, h) for e, h in reads if h is not None]
        unstated = [e for e, h in reads if h is None]
        used = 0
        for e, h in stated:
            e["lines"] = min(h, max(0, total - used))
            used += e["lines"]
        left = max(0, total - used)
        for e in unstated:
            e["lines"] = left // len(unstated)
    # Search hits from the output.
    if "search" in kinds:
        hits = defaultdict(int)
        for line in out.splitlines():
            m = GREP_HIT_RE.match(line.strip()) or GREP_LIST_RE.match(line.strip())
            if m:
                p = rel(os.path.join(cwd, m.group(1)) if cwd else m.group(1), root)
                if p and not p.startswith(".."):
                    hits[p] += 1
        for p, n in hits.items():
            files.append({"path": p, "kind": "hit", "lines": n})
    order = ["edit", "read", "build", "search", "vcs", "shell"]
    kind = next((k for k in order if k in kinds), "shell")
    return kind, files, total, cwd


def count_patch_lines(structured):
    n = 0
    if not isinstance(structured, dict):  # an error message instead of the patch
        return 0
    for hunk in structured.get("structuredPatch") or []:
        n += sum(1 for l in hunk.get("lines", []) if l.startswith("+"))
    return n


def build(turns, results, root, source_root):
    files = {}
    cwd = ""

    def touch(path, kind, lines, n):
        f = files.setdefault(path, {"path": path, "lines": None, "first": n, "read": 0, "edits": 0, "hits": 0})
        if kind == "read":
            f["read"] += lines or 0
        elif kind == "edit":
            f["edits"] += 1
        elif kind == "hit":
            f["hits"] += lines or 0

    for turn in turns:
        turn["result_chars"] = 0
        for use in turn.pop("_uses"):
            name, inp = use.get("name"), use.get("input") or {}
            res = results.get(use.get("id"), {"text": "", "error": False, "structured": None})
            if "shell" in use:  # a command another agent ran, with its output (codex_trace): classified like a Bash call
                command, out, failed = use["shell"][:3]
                res = {"text": out, "error": failed, "structured": None}
                use = {"name": "Bash", "input": {"command": command}, "_cwd": use["shell"][3] if len(use["shell"]) > 3 else None}
                name, inp = "Bash", use["input"]
            elif "entry" in use:  # a ready-made entry (edits from a patch, a web search, a plan update)
                out = use.get("result_text") or ""
                entry = dict(use["entry"])
                entry["files"] = [dict(f, path=rel(f["path"], root)) for f in entry.get("files") or []]
                if entry["kind"] in ("edit", "read") and entry["files"]:
                    entry["detail"] = entry["files"][0]["path"]
                turn["result_chars"] += len(out)
                if entry.get("error"):
                    entry["files"] = []
                for f in entry["files"]:
                    touch(f["path"], f["kind"], f["lines"], turn["n"])
                turn["tools"].append(entry)
                continue
            out = res["text"]
            entry = {"tool": name, "kind": "shell", "detail": "", "lines": len(out.splitlines()), "files": []}
            if res["error"]:
                entry["error"] = True
            if name == "Bash" and use.get("_cwd") is not None:  # the agent gave the command its own working directory
                entry["detail"] = inp.get("command", "")
                entry["kind"], entry["files"], entry["lines"], _ = analyze_bash(entry["detail"], out, root, rel(use["_cwd"], root) if use["_cwd"] else "")
            elif name == "Bash":
                entry["detail"] = inp.get("command", "")
                entry["kind"], entry["files"], entry["lines"], new_cwd = analyze_bash(entry["detail"], out, root, cwd)
                if not res["error"]:
                    cwd = new_cwd
            elif name == "Read":
                p = rel(inp.get("file_path", ""), root)
                entry["kind"], entry["detail"] = "read", p + (f" [{inp['offset']}+{inp.get('limit', '')}]" if inp.get("offset") else "")
                entry["files"] = [{"path": p, "kind": "read", "lines": entry["lines"]}]
            elif name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
                p = rel(inp.get("file_path", ""), root)
                lines = count_patch_lines(res["structured"]) if name != "Write" else len(inp.get("content", "").splitlines())
                entry["kind"], entry["detail"], entry["lines"] = "edit", p, lines
                entry["files"] = [{"path": p, "kind": "edit", "lines": lines}]
            elif name in ("Grep", "Glob"):
                entry["kind"] = "search"
                entry["detail"] = f"{name} {inp.get('pattern', '')} {inp.get('path', '') or ''}".strip()
                hits = defaultdict(int)
                for line in out.splitlines():
                    m = GREP_HIT_RE.match(line.strip()) or GREP_LIST_RE.match(line.strip())
                    if m:
                        hits[rel(m.group(1), root)] += 1
                entry["files"] = [{"path": p, "kind": "hit", "lines": n} for p, n in hits.items()]
            else:
                entry["detail"] = name + " " + json.dumps(inp)[:200]
            turn["result_chars"] += len(out)
            if res["error"]:
                entry["files"] = []
            for f in entry["files"]:
                touch(f["path"], f["kind"], f["lines"], turn["n"])
            turn["tools"].append(entry)
        if not turn["tools"]:
            turn["tools"].append({"tool": "text", "kind": "text", "detail": turn["text"], "lines": 0, "files": []})
    prev = None
    for turn in turns:
        new = turn["cache_write"]
        prev_output = min(prev["output"], new) if prev else 0
        tool_results = min(int(prev["result_chars"] / CHARS_PER_TOKEN), new - prev_output) if prev else 0
        rest = new - prev_output - tool_results
        # first call: the session setup (dynamic system prompt sections, skill and agent listings, CLAUDE.md, the task
        # prompt); later calls: the per-turn reminders and instructions the CLI injects
        turn["new"] = {"prev_output": prev_output, "tool_results": tool_results, "setup": rest if prev is None else 0,
                       "other": 0 if prev is None else rest}
        prev = turn
    if source_root:
        for f in files.values():
            p = os.path.join(source_root, f["path"])
            if os.path.isfile(p):
                try:
                    f["lines"] = sum(1 for _ in open(p, encoding="utf-8", errors="replace"))
                except OSError:
                    pass
    return sorted(files.values(), key=lambda f: (f["first"], f["path"]))


def trace_for(transcript, root=None, source_root=None, title=None):
    """The trace document of one transcript (see TRACE_FORMAT). root shortens file paths (default: the session's cwd);
    source_root says where the files are now, for their line counts (default: root)."""
    agent = agent_of(transcript)
    turns, results, cwd, model = (codex_trace.load if agent == "codex" else load)(transcript)
    prefix = (root or cwd or "").rstrip("/") + "/"
    meta = {"transcript": os.path.basename(transcript), "cwd": cwd, "turns": len(turns), "agent": agent,
            "cache_read_tokens": sum(t["cache_read"] for t in turns), "cache_write_tokens": sum(t["cache_write"] for t in turns),
            "input_tokens": sum(t["input"] for t in turns), "output_tokens": sum(t["output"] for t in turns), "model": model}
    timed = [t for t in turns if t.get("seconds")]
    if timed:
        meta["model_s"] = round(sum(t["seconds"]["model"] for t in timed), 1)
        meta["tools_s"] = round(sum(t["seconds"]["tools"] for t in timed), 1)
        meta["duration_s"] = round(meta["model_s"] + meta["tools_s"], 1)  # active time; the waits between prompts are not in it
    stamps = [t["at"] for t in turns if t.get("at")]
    if stamps:
        meta["started"], meta["ended"] = min(stamps), max(stamps)
    cost = estimate_cost(turns, model)
    if cost is not None:
        meta["cost_estimate_usd"] = cost
    files = build(turns, results, prefix, source_root or root or cwd)
    return {"title": title or transcript_id(transcript)[:8] + " · " + (cwd or ""), "model": model, "agent": agent,
            "meta": meta, "shared_prefix": turns[0]["cache_read"] if turns else 0, "turns": turns, "files": files}


def session_stats(trace, transcript=None):
    """One row of the sessions table: the trace's totals (see build_sessions.py). Tokens and time as in the page's tiles."""
    turns, files, m = trace.get("turns") or [], trace.get("files") or [], trace.get("meta") or {}
    cwd = m.get("cwd") or ""
    first = next((t["prompt"] for t in turns if t.get("prompt")), "")
    started, ended = m.get("started"), m.get("ended")
    span = None
    if started and ended:
        try:
            span = round(datetime.datetime.fromisoformat(ended.replace("Z", "+00:00")).timestamp()
                         - datetime.datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp(), 1)
        except ValueError:
            pass
    sid = (m.get("transcript") or os.path.basename(transcript or "") or trace.get("title") or "")
    sid = sid[:-6] if sid.endswith(".jsonl") else sid
    if m.get("transcript", "").startswith("rollout-"):
        sid = codex_trace.session_id(m["transcript"])
    row = {"id": sid, "title": trace.get("title") or sid, "cwd": cwd, "folder": os.path.basename(cwd.rstrip("/")) or cwd,
           "agent": trace.get("agent") or m.get("agent") or "claude",
           "model": m.get("model") or trace.get("model") or "", "started": started, "ended": ended, "span_s": span,
           "turns": len(turns), "prompts": sum(1 for t in turns if t.get("prompt")), "first_prompt": first,
           "tool_calls": sum(1 for t in turns for x in t.get("tools") or [] if x.get("kind") != "text"),
           "files_read": sum(1 for f in files if f.get("read")), "files_edited": sum(1 for f in files if f.get("edits")),
           "lines_read": sum(f.get("read") or 0 for f in files), "edits": sum(f.get("edits") or 0 for f in files),
           "cache_read": sum(t.get("cache_read", 0) for t in turns), "cache_write": sum(t.get("cache_write", 0) for t in turns),
           "input": sum(t.get("input", 0) for t in turns), "output": sum(t.get("output", 0) for t in turns),
           "cost_usd": m.get("cost_usd"), "cost_estimate_usd": m.get("cost_estimate_usd"),
           "model_s": m.get("model_s"), "tools_s": m.get("tools_s"), "duration_s": m.get("duration_s")}
    if transcript and os.path.isfile(transcript):
        row["transcript_bytes"] = os.path.getsize(transcript)
    return row


def project_folders(agent=None):
    """Every folder under ~/.claude/projects that holds transcripts, newest first, with its transcripts; with Codex's
    sessions folder (all of them in one entry) unless agent = "claude"."""
    out = []
    if agent in (None, "claude"):
        for d in glob.glob(os.path.join(PROJECTS, "*")):
            files = sorted(glob.glob(os.path.join(d, "*.jsonl")), key=os.path.getmtime)
            if files:
                out.append((d, files))
    if agent in (None, "codex"):
        files = codex_trace.transcripts()
        if files:
            out.append((codex_trace.CODEX_SESSIONS, files))
    return sorted(out, key=lambda x: os.path.getmtime(x[1][-1]), reverse=True)


HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLES_NOTE = ('The embedded examples are runs of the <a href="https://github.com/zeljkoobrenovic/sokrates-size-experiment">'
                 'sokrates-size-experiment</a>;')


def html_escape(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def json_for_html(obj):
    """JSON safe inside a <script> element."""
    return json.dumps(obj).replace("</", "<\\/")


def fill_template(name, **subs):
    """A page from one of the templates next to this script, its ${...} placeholders filled: examples (a list of traces),
    live (the live-mode config or None), nav (HTML above the controls), note (the footer sentence), sessions (the
    sessions page's rows); prices and generated are always filled."""
    text = open(os.path.join(HERE, name), encoding="utf-8").read()
    values = {"prices": json_for_html(PRICES), "generated": datetime.date.today().isoformat(),
              "examples": json_for_html(subs.pop("examples", [])), "live": json_for_html(subs.pop("live", None)),
              "nav": "", "note": "", "sessions": json_for_html(subs.pop("sessions", None)), "prices_date": PRICES_DATE}
    values.update(subs)
    for k, v in values.items():
        text = text.replace("${" + k + "}", v)
    return text


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transcript", help="a Claude Code session transcript (.jsonl)")
    ap.add_argument("--cwd", help="folder the session was started in (latest transcript, or the one given with --session)")
    ap.add_argument("--session", help="session id (or a prefix of it) among the sessions of --cwd; see --list")
    ap.add_argument("--agent", choices=["claude", "codex"], help="only that agent's sessions of --cwd (default: both)")
    ap.add_argument("--list", action="store_true", help="list the sessions started in --cwd and exit")
    ap.add_argument("--root", help="path prefix to strip from file paths (default: the session's cwd)")
    ap.add_argument("--source-root", help="where the files are now, for their total line counts (default: --root)")
    ap.add_argument("--title")
    ap.add_argument("--out", help="output JSON (default: stdout)")
    args = ap.parse_args()
    if not (args.transcript or args.cwd):
        ap.error("one of --transcript, --cwd is required")
    if args.list:
        for x in sessions_of(args.cwd or os.getcwd(), args.agent):
            print(f"{x['id']}  {x['agent']:6}  {x['started']:16}  {x['calls']:4} calls  {x['prompt']}")
        return
    transcript = find_transcript(args)
    trace = trace_for(transcript, root=args.root, source_root=args.source_root, title=args.title)
    text = json.dumps(trace, indent=1)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text)
        print(f"{args.out}: {len(trace['turns'])} turns, {len(trace['files'])} files", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
