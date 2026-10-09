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
drop the file on the page.
"""
import argparse
import glob
import json
import os
import re
import shlex
import sys
from collections import defaultdict

PROJECTS = os.path.expanduser("~/.claude/projects")

TRACE_FORMAT = """
{
  "title": "...", "model": "...", "meta": {"task": ..., "variant": ..., "turns": N, ...},
  "shared_prefix": 10874,   # cache read by the first call: the static system prompt and tool definitions, shared by every session
  "turns": [ {"n": 1, "input": 2, "cache_read": 10874, "cache_write": 8434, "output": 105,
              "text": "what the agent said (first line)",
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
READ_CMDS = {"cat", "head", "tail", "nl", "less", "more", "bat"}
SEARCH_CMDS = {"grep", "rg", "find", "ls", "wc", "fgrep", "egrep", "ag", "tree"}
BUILD_CMDS = {"mvn", "java", "javac", "./mvnw", "make"}
CHARS_PER_TOKEN = 2.3  # measured on the experiment transcripts: (cache_write - previous output) / tool result chars, median 2.3 (code, paths, grep output)


def transcripts_for(cwd):
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(cwd))
    return sorted(glob.glob(os.path.join(PROJECTS, slug, "*.jsonl")), key=os.path.getmtime)


def find_transcript(args):
    if args.transcript:
        return args.transcript
    files = transcripts_for(args.cwd)
    if getattr(args, "session", None):
        files = [f for f in files if os.path.basename(f).startswith(args.session)]
    if not files:
            sys.exit(f"no transcript for {args.cwd} under {PROJECTS}")
    return files[-1]


def list_sessions(cwd):
    """Print the sessions started in cwd, newest first: id, start time, API calls, first prompt."""
    import datetime
    for f in reversed(transcripts_for(cwd)):
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
        sid = os.path.basename(f)[:-6]
        print(f"{sid}  {started or '':16}  {len(calls):4} calls  {first}")


def result_text(block):
    content = block.get("content")
    if isinstance(content, list):
        return "".join(x.get("text", "") for x in content if isinstance(x, dict))
    return "" if content is None else str(content)


def load(transcript):
    """Group the transcript into API calls (one per assistant message id) with their tool results attached."""
    turns, by_id, results, cwd, model = [], {}, {}, None, ""
    for line in open(transcript, encoding="utf-8"):
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("cwd") and not cwd:
            cwd = o["cwd"]
        t = o.get("type")
        if t == "assistant" and not model and (o.get("message") or {}).get("model"):
            model = o["message"]["model"]
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
                by_id[mid] = turn
                turns.append(turn)
            for b in content:
                if b.get("type") == "tool_use":
                    turn["_uses"].append(b)
                elif b.get("type") == "text" and b.get("text", "").strip() and not turn["text"]:
                    turn["text"] = b["text"].strip().splitlines()[0][:200]
        elif t == "user" and isinstance(content, list):
            for b in content:
                if b.get("type") == "tool_result":
                    results[b.get("tool_use_id")] = {"text": result_text(b), "error": bool(b.get("is_error")),
                                                     "structured": o.get("toolUseResult")}
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
    segments = re.split(r"\s*(?:&&|\|\||;|\|)\s*", command.strip())
    for seg in segments:
        seg = seg.strip()
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
    for hunk in (structured or {}).get("structuredPatch") or []:
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
            out = res["text"]
            entry = {"tool": name, "kind": "shell", "detail": "", "lines": len(out.splitlines()), "files": []}
            if res["error"]:
                entry["error"] = True
            if name == "Bash":
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transcript", help="a Claude Code session transcript (.jsonl)")
    ap.add_argument("--cwd", help="folder the session was started in (latest transcript, or the one given with --session)")
    ap.add_argument("--session", help="session id (or a prefix of it) among the sessions of --cwd; see --list")
    ap.add_argument("--list", action="store_true", help="list the sessions started in --cwd and exit")
    ap.add_argument("--root", help="path prefix to strip from file paths (default: the session's cwd)")
    ap.add_argument("--source-root", help="where the files are now, for their total line counts (default: --root)")
    ap.add_argument("--title")
    ap.add_argument("--out", help="output JSON (default: stdout)")
    args = ap.parse_args()
    if not (args.transcript or args.cwd):
        ap.error("one of --transcript, --cwd is required")
    if args.list:
        list_sessions(args.cwd or os.getcwd())
        return
    transcript = find_transcript(args)
    turns, results, cwd, model = load(transcript)
    root = (args.root or cwd or "").rstrip("/") + "/"
    meta = {"transcript": os.path.basename(transcript), "cwd": cwd, "turns": len(turns),
            "cache_read_tokens": sum(t["cache_read"] for t in turns), "cache_write_tokens": sum(t["cache_write"] for t in turns),
            "input_tokens": sum(t["input"] for t in turns), "output_tokens": sum(t["output"] for t in turns), "model": model}
    source_root = args.source_root or args.root or cwd
    files = build(turns, results, root, source_root)
    trace = {"title": args.title or os.path.basename(transcript)[:8] + " · " + (cwd or ""), "model": model,
             "meta": meta, "shared_prefix": turns[0]["cache_read"] if turns else 0, "turns": turns, "files": files}
    text = json.dumps(trace, indent=1)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text)
        print(f"{args.out}: {len(turns)} turns, {len(files)} files", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
