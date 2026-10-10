#!/usr/bin/env python3
"""Codex CLI sessions for turn_trace.py: reads a Codex rollout transcript into the same turns that the Claude Code
reader produces, so one builder, one page and one sessions table serve both agents.

Codex writes every session (TUI, VS Code, `codex exec`) to ~/.codex/sessions/<year>/<month>/<day>/rollout-<time>-<id>.jsonl.
Each line is {"timestamp", "type", "payload"}: a `session_meta` line first (session id, cwd, cli_version), then
`response_item` lines for what went to and came from the model (user and assistant messages, reasoning, the tool calls
and their outputs), `event_msg` lines for what the CLI did (the user's prompts, `task_started`/`task_complete`, the
token counts, and since Codex 0.14x `item_completed` items: CommandExecution with the command, its output, exit code and
duration, FileChange with a unified diff per file, Extension for web searches, ImageView; before that
`exec_command_end` and `patch_apply_end` carried the same), `turn_context` (model, cwd, policies) and, since 0.15x,
a `token_usage_record` per API call (input, cached input, cache-write and output tokens, reasoning included in output).

API calls are recognized from the response items: a run of model output (reasoning, message, tool calls) that follows
a user message or a tool output is one call; its usage is the first token record (`token_usage_record`, else a
`token_count` event with usage) that follows it. Codex sends the tool outputs with the next call, so the token record
of a call may come before (new layout) or after (old layout) its outputs; the window up to the next call covers both.

Token mapping: `cache_read` = cached input tokens; `cache_write` = the input tokens that were not cached (plus the
cache-write tokens when the API reports them): the context the call added, which the next call reads from the cache,
billed at the input rate; `input` = 0; `output` = output tokens, reasoning included. The page labels the row "new,
not from cache" for Codex traces (trace["agent"] == "codex").
"""
import datetime
import glob
import json
import os
import re

CODEX_SESSIONS = os.path.expanduser("~/.codex/sessions")

RESPONSE_KINDS = {"reasoning", "message", "function_call", "custom_tool_call", "web_search_call", "local_shell_call"}
CALL_KINDS = {"function_call", "custom_tool_call", "web_search_call", "local_shell_call"}
OUTPUT_KINDS = {"function_call_output", "custom_tool_call_output"}
EXEC_RE = re.compile(r"exec_command\(\s*\{(.*?)\}\s*\)", re.S)
CMD_RE = re.compile(r'"?cmd"?\s*:\s*("(?:[^"\\]|\\.)*")')
PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.M)


def is_codex_transcript(path):
    """True for a Codex rollout file (its first line is the session_meta)."""
    try:
        with open(path, encoding="utf-8") as f:
            head = f.readline()
    except OSError:
        return False
    return '"type":"session_meta"' in head.replace(" ", "") or '"type": "session_meta"' in head


def session_meta(path):
    """The first line's payload (id, cwd, timestamp, cli_version...), or {} when it is not a rollout file."""
    try:
        with open(path, encoding="utf-8") as f:
            o = json.loads(f.readline())
    except (OSError, ValueError):
        return {}
    return o.get("payload") or {} if o.get("type") == "session_meta" else {}


def transcripts(cwd=None):
    """Every rollout file, oldest first by modification time; with cwd, only the sessions started in that folder."""
    files = sorted(glob.glob(os.path.join(CODEX_SESSIONS, "*", "*", "*", "rollout-*.jsonl")), key=os.path.getmtime)
    if cwd:
        want = os.path.abspath(cwd).rstrip("/")
        files = [f for f in files if (session_meta(f).get("cwd") or "").rstrip("/") == want]
    return files


def session_id(path):
    """The session id of a rollout file: the uuid at the end of its name."""
    m = re.search(r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(.+)\.jsonl$", os.path.basename(path))
    return m.group(1) if m else os.path.basename(path)[:-6]


def when(o):
    s = o.get("timestamp")
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def user_text(p):
    """The text of a user message the user typed; None for the AGENTS.md, environment and other injected messages."""
    c = p.get("content")
    if isinstance(c, str):
        text = c
    elif isinstance(c, list):
        text = "".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") in ("input_text", "text"))
    else:
        return None
    text = text.strip()
    if not text or text.startswith("<") or text.startswith("# AGENTS.md"):
        return None
    return text.splitlines()[0][:200]


def output_text(p):
    """The text of a tool output: the old exec_command format ("...Output:\\n<text>"), apply_patch's JSON, or the exec
    script's list of text items (each a JSON result of a command, or plain text)."""
    out = p.get("output")
    if isinstance(out, list):
        parts = []
        for item in out:
            t = item.get("text", "") if isinstance(item, dict) else str(item)
            try:
                j = json.loads(t)
                v = j.get("value") if isinstance(j, dict) else None
                t = (v or j).get("output", t) if isinstance(v or j, dict) else t
            except (ValueError, AttributeError):
                pass
            parts.append(str(t))
        return "\n".join(parts)
    out = "" if out is None else str(out)
    if out.startswith("{"):
        try:
            j = json.loads(out)
            if isinstance(j, dict) and "output" in j:
                return str(j["output"])
        except ValueError:
            pass
    i = out.find("\nOutput:\n")
    return out[i + 9:] if i >= 0 else out


def output_failed(p):
    out = p.get("output")
    if isinstance(out, str):
        m = re.search(r"Process exited with code (\d+)", out)
        if m:
            return m.group(1) != "0"
        if out.startswith("{"):
            try:
                return (json.loads(out).get("metadata") or {}).get("exit_code", 0) != 0
            except ValueError:
                return False
        return out.startswith("aborted")
    return False


def added_lines(changes):
    """Lines added per path in a changes map {path: {type, unified_diff}} (patch_apply_end and FileChange alike)."""
    out = {}
    for path, ch in (changes or {}).items():
        diff = (ch or {}).get("unified_diff") or ""
        out[path] = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    return out


def load(transcript):
    """(turns, results, cwd, model) in the shape of turn_trace.load: each turn's `_uses` are ready-made tool entries
    ({"entry": {...}, "result_text": "..."}) which turn_trace.build takes as they are."""
    turns, cwd, model, pending, last_user = [], None, "", None, None
    cur = None  # the turn being read: {"turn": ..., "events": [], "calls": [], "outputs": [], "usage": None}
    windows = []
    has_items = has_exec_end = False
    for line in open(transcript, encoding="utf-8"):
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        t, p, now = o.get("type"), o.get("payload") or {}, when(o)
        if t == "session_meta":
            cwd = cwd or p.get("cwd")
            continue
        if t == "turn_context":
            cwd = cwd or p.get("cwd")
            model = model or p.get("model") or ""
            continue
        if t == "event_msg" and p.get("type") == "thread_settings_applied":
            model = model or (p.get("thread_settings") or {}).get("model") or ""
            continue
        pt = p.get("type")
        if t == "response_item" and pt == "message" and p.get("role") != "assistant":  # the user, or injected context
            text = user_text(p) if p.get("role") == "user" else None
            if text:
                pending = text
            if now:
                last_user = now
            cur = None  # the next model output starts a new call
            continue
        if t == "response_item" and pt in RESPONSE_KINDS:
            if cur is None or cur.get("closed"):
                turn = {"n": len(turns) + 1, "input": 0, "cache_read": 0, "cache_write": 0, "output": 0, "text": "", "tools": [],
                        "_uses": [], "_start": last_user, "at": o.get("timestamp")}
                if pending:
                    turn["prompt"], pending = pending, None
                turns.append(turn)
                cur = {"turn": turn, "events": [], "calls": [], "outputs": [], "usage": None}
                windows.append(cur)
            turn = cur["turn"]
            if now:
                turn["_model_end"] = max(turn.get("_model_end") or 0, now)
            if pt == "message" and not turn["text"]:
                c = p.get("content") or []
                text = "".join(b.get("text", "") for b in c if isinstance(b, dict)).strip() if isinstance(c, list) else str(c)
                turn["text"] = text.splitlines()[0][:200] if text else ""
            elif pt in CALL_KINDS:
                cur["calls"].append(p)
            continue
        if cur is None:
            continue
        if t == "response_item" and pt in OUTPUT_KINDS:
            cur["outputs"].append(p)
            if now:
                cur["turn"]["_tools_end"] = max(cur["turn"].get("_tools_end") or 0, now)
                last_user = now
            cur["closed"] = True  # a tool output ends the model's turn: the next response item is a new call
        elif t == "token_usage_record" and cur["usage"] is None:
            cur["usage"] = p.get("usage") or (p.get("turn_token_usage") or {})
        elif t == "event_msg" and pt == "token_count" and cur["usage"] is None:
            info = p.get("info") or {}
            if info.get("last_token_usage"):
                cur["usage"] = info["last_token_usage"]
        elif t == "event_msg" and pt == "item_completed":
            item = p.get("item") or {}
            if item.get("type") in ("CommandExecution", "FileChange", "Extension", "ImageView"):
                has_items = True
                cur["events"].append(item)
                if now:
                    cur["turn"]["_tools_end"] = max(cur["turn"].get("_tools_end") or 0, now)
        elif t == "event_msg" and pt in ("exec_command_end", "patch_apply_end"):
            has_exec_end = has_exec_end or pt == "exec_command_end"
            cur["events"].append(p)
            if now:
                cur["turn"]["_tools_end"] = max(cur["turn"].get("_tools_end") or 0, now)
    # usage, tool entries
    for w in windows:
        turn, u = w["turn"], w["usage"] or {}
        cached, written = u.get("cached_input_tokens", 0) or 0, u.get("cache_write_input_tokens", 0) or 0
        turn["cache_read"] = cached
        turn["cache_write"] = max(0, (u.get("input_tokens", 0) or 0) - cached - written) + written
        turn["output"] = u.get("output_tokens", 0) or 0
        turn["_uses"] = entries(w, has_items, has_exec_end)
    for turn in turns:
        start, mend, tend = turn.pop("_start", None), turn.pop("_model_end", None), turn.pop("_tools_end", None)
        if start and mend and mend >= start:
            turn["seconds"] = {"model": round(mend - start, 1), "tools": round(max(0.0, tend - mend), 1) if tend else 0.0}
    return turns, {}, cwd, model


def shell_entry(command, out, failed, cwd=""):
    return {"entry_kind": "shell", "command": command, "out": out, "error": failed, "cwd": cwd}


def entries(w, has_items, has_exec_end):
    """The tool entries of one call, from the richest source the file has: item_completed items, else the
    exec_command_end / patch_apply_end events, else the tool calls and their outputs."""
    uses = []
    by_call = {p.get("call_id"): p for p in w["outputs"]}
    edits_seen = set()
    if has_items or has_exec_end:
        for ev in w["events"]:
            k = ev.get("type")
            if k in ("CommandExecution", "exec_command_end"):
                cmd = ev.get("command") or []
                command = cmd[-1] if isinstance(cmd, list) and cmd else str(cmd)
                out = ev.get("aggregated_output") or ev.get("stdout") or ev.get("formatted_output") or ""
                code = ev.get("exit_code")
                failed = (ev.get("status") not in (None, "completed")) or (code not in (None, 0, "0"))
                uses.append({"entry": None, "shell": (command, out, failed)})
            elif k in ("FileChange", "patch_apply_end"):
                for path, n in added_lines(ev.get("changes")).items():
                    edits_seen.add(path)
                    uses.append({"entry": {"tool": "apply_patch", "kind": "edit", "detail": path, "lines": n,
                                           "files": [{"path": path, "kind": "edit", "lines": n}]}, "result_text": ""})
            elif k == "Extension" and ev.get("kind") == "web.search":
                qs = (ev.get("action") or {}).get("queries") or [ev.get("query") or ""]
                uses.append({"entry": {"tool": "web_search", "kind": "search", "detail": "web: " + "; ".join(q for q in qs if q), "lines": 0, "files": []}, "result_text": ""})
            elif k == "ImageView":
                path = ev.get("path") or ev.get("file") or ""
                uses.append({"entry": {"tool": "view_image", "kind": "read", "detail": path, "lines": 0,
                                       "files": [{"path": path, "kind": "read", "lines": 0}] if path else []}, "result_text": ""})
    for call in w["calls"]:
        kind, name = call.get("type"), call.get("name")
        res = by_call.get(call.get("call_id"), {})
        out, failed = output_text(res), output_failed(res)
        if kind == "function_call" and name == "exec_command":
            if has_items or has_exec_end:
                continue  # the events above carried it
            try:
                args = json.loads(call.get("arguments") or "{}")
            except ValueError:
                args = {}
            uses.append({"entry": None, "shell": (args.get("cmd") or "", out, failed, args.get("workdir") or "")})
        elif kind == "custom_tool_call" and name == "exec":
            if has_items:
                if not any(u.get("shell") for u in uses):  # a script that ran no command: show it as shell work
                    uses.append({"entry": {"tool": "exec", "kind": "shell", "detail": (call.get("input") or "")[:200], "lines": len(out.splitlines()), "files": [], "error": failed}, "result_text": out})
                continue
            for m in EXEC_RE.finditer(call.get("input") or ""):  # old files without items: the commands in the script
                cm = CMD_RE.search(m.group(1))
                if cm:
                    try:
                        uses.append({"entry": None, "shell": (json.loads(cm.group(1)), out, failed)})
                    except ValueError:
                        pass
        elif name == "apply_patch":
            if edits_seen:
                continue
            for path in PATCH_FILE_RE.findall(call.get("input") or call.get("arguments") or ""):
                uses.append({"entry": {"tool": "apply_patch", "kind": "edit", "detail": path, "lines": None,
                                       "files": [{"path": path, "kind": "edit", "lines": None}], "error": failed}, "result_text": out})
        elif kind == "web_search_call":
            if has_items:
                continue
            qs = (call.get("action") or {}).get("queries") or [(call.get("action") or {}).get("query") or ""]
            uses.append({"entry": {"tool": "web_search", "kind": "search", "detail": "web: " + "; ".join(q for q in qs if q), "lines": 0, "files": []}, "result_text": ""})
        elif kind == "local_shell_call":
            cmd = (call.get("action") or {}).get("command") or []
            uses.append({"entry": None, "shell": (cmd[-1] if cmd else "", out, failed)})
        elif name in ("update_plan", "write_stdin", "wait", "view_image", "spawn_agent", "send_message"):
            detail = name
            try:
                a = json.loads(call.get("arguments") or "{}")
                detail = name + " " + (a.get("path") or a.get("task_name") or a.get("explanation") or "")[:160]
            except ValueError:
                pass
            uses.append({"entry": {"tool": name, "kind": "shell", "detail": detail.strip(), "lines": len(out.splitlines()), "files": [], "error": failed}, "result_text": out})
    return uses


def list_sessions(cwd=None):
    """[(path, meta)] of the Codex sessions, newest first."""
    return [(f, session_meta(f)) for f in reversed(transcripts(cwd))]
