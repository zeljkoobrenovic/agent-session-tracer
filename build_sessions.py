#!/usr/bin/env python3
"""Build a sessions landscape: one page listing every agent session (Claude Code or Codex) of one or more folders (or of all of them)
with its totals, and one trace page per session, which the table's rows open.

Usage:
  build_sessions.py                                  # the sessions started in the current folder -> ./sessions/
  build_sessions.py --cwd /path/a --cwd /path/b      # those folders
  build_sessions.py --all                            # every folder under ~/.claude/projects
  build_sessions.py --trace a.json --trace b.json    # traces extracted earlier (turn_trace.py --out)
  ... [--out DIR] [--since YYYY-MM-DD] [--min-turns N] [--title TEXT] [--source-root DIR] [--jobs N]

Output: <out>/index.html (sessions_template.html with one row per session: started, folder, first prompt, model,
prompts, turns, tool calls, files, lines read, edits, tokens re-sent / new / output, cost, active time, span; sortable,
searchable, summed in tiles and per folder) and <out>/<session id>.html per session (template.html with that trace
embedded and a link back). Self-contained pages, no network. Python 3.8+, no dependencies.
"""
import argparse
import datetime
import json
import os
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor

import turn_trace

HERE = os.path.dirname(os.path.abspath(__file__))


def session_page(trace, out_dir, name, back_label):
    """Write the trace page of one session and return its file name."""
    nav = f'<p class="nav"><a href="index.html">← {turn_trace.html_escape(back_label)}</a></p>'
    agent = "Codex" if trace.get("agent") == "codex" else "Claude Code"
    note = f"Built from the agent's transcript {turn_trace.html_escape((trace.get('meta') or {}).get('transcript') or name)} ({agent})."
    page = turn_trace.fill_template("template.html", examples=[trace], nav=nav, note=note)
    page = page.replace('<span id="pickLabel">Example</span>', '<span id="pickLabel">Trace</span>')  # one trace, not an example
    with open(os.path.join(out_dir, name + ".html"), "w", encoding="utf-8") as f:
        f.write(page)
    return name + ".html"


def extract(job):
    """Worker: extract one transcript, write its page, return the row (or None when filtered out / empty)."""
    path, out_dir, source_root, since, min_turns, back_label = job
    try:
        trace = turn_trace.trace_for(path, source_root=source_root)
    except Exception as e:  # a damaged transcript should not stop the build
        return {"error": f"{os.path.basename(path)}: {e}"}
    if len(trace["turns"]) < min_turns:
        return None
    row = turn_trace.session_stats(trace, path)
    if since and (row.get("started") or "")[:10] < since:
        return None
    if row["first_prompt"]:
        trace["title"] = row["first_prompt"][:90]
    row["page"] = session_page(trace, out_dir, row["id"], back_label)
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cwd", action="append", help="folder whose sessions to list (repeatable; default: the current folder)")
    ap.add_argument("--all", action="store_true", help="every session: all folders under ~/.claude/projects and all Codex sessions")
    ap.add_argument("--agent", choices=["claude", "codex"], help="only that agent's sessions (default: both)")
    ap.add_argument("--trace", action="append", help="a trace JSON extracted earlier, instead of a transcript (repeatable)")
    ap.add_argument("--out", default="sessions", help="output folder (default: ./sessions)")
    ap.add_argument("--since", help="only sessions whose first API call is on or after this date (YYYY-MM-DD)")
    ap.add_argument("--min-turns", type=int, default=1, help="skip sessions with fewer API calls (default 1: skip only empty transcripts)")
    ap.add_argument("--title", help="what the pages call this set of sessions (default: the folder names)")
    ap.add_argument("--source-root", help="where the files are now, for their line counts (default: each session's folder)")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 1, help="transcripts extracted in parallel (default: the CPU count)")
    args = ap.parse_args()

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    transcripts, folders = [], []
    if args.all:
        for d, files in turn_trace.project_folders(args.agent):
            transcripts += files
            folders.append(os.path.basename(d))
    elif args.cwd or not args.trace:
        for cwd in args.cwd or [os.getcwd()]:
            files = turn_trace.transcripts_for(cwd, args.agent)
            if not files:
                print(f"no transcripts for {cwd} under {turn_trace.PROJECTS}", file=sys.stderr)
            transcripts += files
            folders.append(os.path.basename(os.path.abspath(cwd).rstrip("/")))
    title = args.title or ("all sessions" if args.all else ", ".join(folders) if folders else "traces")
    back = "all sessions" + (f" of {title}" if title and title != "traces" else "")

    rows, errors = [], []
    jobs = [(p, out_dir, args.source_root, args.since, max(1, args.min_turns), back) for p in transcripts]
    if jobs:
        print(f"extracting {len(jobs)} transcript{'s' if len(jobs) != 1 else ''} with {args.jobs} job{'s' if args.jobs != 1 else ''}…", flush=True)
        results = ProcessPoolExecutor(max_workers=args.jobs).map(extract, jobs, chunksize=4) if args.jobs > 1 else map(extract, jobs)
        for i, r in enumerate(results, 1):
            if r is None:
                continue
            if "error" in r:
                errors.append(r["error"])
                continue
            rows.append(r)
            if i % 50 == 0 or i == len(jobs):
                print(f"  {i}/{len(jobs)}", flush=True)
    for p in args.trace or []:
        trace = json.load(open(p, encoding="utf-8"))
        trace.setdefault("title", os.path.basename(p)[:-5])
        row = turn_trace.session_stats(trace)
        row["id"] = os.path.basename(p)[:-5]
        row["page"] = session_page(trace, out_dir, row["id"], back)
        rows.append(row)
    rows.sort(key=lambda r: r.get("started") or "", reverse=True)

    note = (f"{len(rows)} session{'s' if len(rows) != 1 else ''} of {turn_trace.html_escape(title)}, built from the transcripts "
            f"the agents keep under ~/.claude/projects and ~/.codex/sessions.")
    page = turn_trace.fill_template("sessions_template.html", sessions={"title": title, "sessions": rows}, note=note)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    for name in ("claude.png", "codex.png"):
        logo = os.path.join(HERE, "docs", name)
        if os.path.isfile(logo):
            shutil.copy(logo, os.path.join(out_dir, name))
    for e in errors:
        print("skipped " + e, file=sys.stderr)
    cost = sum((r.get("cost_usd") if r.get("cost_usd") is not None else r.get("cost_estimate_usd")) or 0 for r in rows)
    print(f"{os.path.join(out_dir, 'index.html')}: {len(rows)} sessions, {sum(r['turns'] for r in rows)} turns, "
          f"${cost:,.2f} (recorded or estimated){', ' + str(len(errors)) + ' skipped' if errors else ''}")


if __name__ == "__main__":
    main()
