#!/usr/bin/env python3
"""kgen: deterministic, confidence-labelled repository knowledge (claims -> graph -> OKF docs)."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from kgen import generate as gen  # noqa: E402
from kgen.claims import sha256_file  # noqa: E402


def cmd_sync(a):
    r = gen.generate(Path(a.repo).resolve(), Path(a.out).resolve() if a.out else None, a.labels, a.freshness, a.min, a.components, Path(a.extra_claims).resolve() if a.extra_claims else None)
    print(f"{r['claims']} claims {r['by_confidence']}; {r['docs']} docs; wrote {len(r['written'])} files, removed {len(r['removed'])}")
    return 0


def cmd_check(a):
    repo = Path(a.repo).resolve()
    out = repo / gen.OUT
    m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    stale = []
    files = gen.source_files(repo)
    for p, h in m["sources"].items():
        cur = sha256_file(files[p]) if p in files else None
        if cur != h:
            stale.append((p, "deleted" if cur is None else "changed"))
    scanned = m.get("scanned", {})
    new = sorted(p for p in files if p not in scanned)
    for p, d in files.items():
        if p in scanned and p not in m["sources"] and sha256_file(d) != scanned[p]:
            stale.append((p, "changed"))
    docs = sorted(d for d, meta in m["docs"].items() if any(s["path"] in {p for p, _ in stale} for s in meta["sources"]))
    for p, why in stale:
        print(f"stale source ({why}): {p}")
    for p in new:
        print(f"new source file not in graph: {p}")
    for d in docs:
        print(f"stale doc: .agent-knowledge/{d}")
    if not stale and not new:
        print("up to date")
        return 0
    print("run: uv run kgen.py sync --repo", a.repo)
    return 1


def cmd_verify(a):
    repo = Path(a.repo).resolve()
    bad = total = 0
    for line in (repo / gen.OUT / "claims.jsonl").read_text(encoding="utf-8").splitlines():
        c = json.loads(line)
        src = c["source"]
        f = repo / src["file"]
        total += 1
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines() if f.exists() else []
        ev = " ".join((c.get("evidence") or "").split()).rstrip(",")
        if ev.endswith("=[redacted]"):
            ev = ev[: -len("[redacted]")]
        ok = f.exists() and 0 < src["line"] <= len(lines) and ev in " ".join(lines[src["line"] - 1].split())
        if not ok:
            bad += 1
            if bad <= 20:
                print(f"evidence moved or gone: {c['id']} {src['file']}:{src['line']} {c['predicate']}")
    print(f"{total - bad}/{total} claims still match their cited line")
    return 1 if bad else 0


def main():
    p = argparse.ArgumentParser(prog="kgen")
    sub = p.add_subparsers(dest="cmd", required=True)
    for n, fn in (("sync", cmd_sync), ("check", cmd_check), ("verify", cmd_verify)):
        s = sub.add_parser(n)
        s.add_argument("--repo", required=True)
        if n == "sync":
            s.add_argument("--labels", choices=("confidence", "provenance", "none", "tags", "instruction"), default="confidence")
            s.add_argument("--freshness", action="store_true")
            s.add_argument("--out")
            s.add_argument("--min", choices=("high", "medium"), default="medium", help="drop claims below this tier")
            s.add_argument("--components", choices=("docs", "graph", "both"), default="both")
            s.add_argument("--extra-claims", help="JSONL of model-inferred claims (tier low)")
        s.set_defaults(fn=fn)
    a = p.parse_args()
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
