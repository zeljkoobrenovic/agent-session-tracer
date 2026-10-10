#!/usr/bin/env python3
"""Build docs/index.html: the trace page (template.html) with the example traces of examples/*.json embedded.

Usage: build_page.py [trace.json ...]      # default: every examples/*.json, in name order
The page also accepts any trace dropped on it, so rebuilding is only needed to change the embedded examples
(or the price table in turn_trace.py).
"""
import glob
import json
import os
import sys

import turn_trace

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    paths = sys.argv[1:] or sorted(glob.glob(os.path.join(HERE, "examples", "*.json")))
    traces = []
    for p in paths:
        t = json.load(open(p))
        t.setdefault("title", os.path.basename(p)[:-5])
        traces.append(t)
        print(f"{os.path.basename(p)}: {len(t['turns'])} turns, {len(t['files'])} files")
    nav = '<p class="nav"><a href="sessions/">All example sessions in one table \u2192</a></p>'  # docs/sessions, built by build_sessions.py
    page = turn_trace.fill_template("template.html", examples=traces, nav=nav, note=turn_trace.EXAMPLES_NOTE)
    os.makedirs(os.path.join(HERE, "docs"), exist_ok=True)
    with open(os.path.join(HERE, "docs", "index.html"), "w") as f:
        f.write(page)
    print("docs/index.html")


if __name__ == "__main__":
    main()
