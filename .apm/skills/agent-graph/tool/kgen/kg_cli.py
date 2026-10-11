#!/usr/bin/env python3
"""Query the generated code graph (graph.json). Auto-generated knowledge: check [medium] edges against the source."""

import argparse
import json
import re
import sys
from collections import defaultdict, deque
from pathlib import Path

RANK = {"high": 2, "medium": 1, "low": 0}
MODE = "confidence"
SHOW = MODE in ("confidence", "tags")


def tg(c):
    return f"[{c}] " if SHOW and c else ""
HERE = Path(__file__).resolve().parent


def load():
    g = json.loads((HERE / "graph.json").read_text(encoding="utf-8"))
    nodes = {n["id"]: n for n in g["nodes"]}
    for n in nodes.values():
        n.setdefault("confidence", "high")
        for f in n.get("facts", []):
            f.setdefault("confidence", "high")
    for e in g["edges"]:
        e.setdefault("confidence", "high")
    out, inc = defaultdict(list), defaultdict(list)
    for e in g["edges"]:
        out[e["from"]].append(e)
        inc[e["to"]].append(e)
    return g, nodes, out, inc


def resolve(nodes, text):
    if text in nodes:
        return [text]
    for prefix in ("type:", "method:", "route:", "table:", "template:"):
        if prefix + text in nodes:
            return [prefix + text]
    low = text.lower()
    exact = [i for i, n in nodes.items() if i.split(":", 1)[1].lower() == low or i.split(":", 1)[1].lower().endswith("." + low) or i.split(":", 1)[1].lower().endswith("#" + low)]
    if exact:
        return sorted(exact)
    return sorted(i for i in nodes if low in i.lower())[:20]


def pick(nodes, text):
    r = resolve(nodes, text)
    if not r:
        sys.exit(f"no match for {text!r}; try: kg find {text}")
    if len(r) > 1:
        print(f"ambiguous {text!r}, using first of: " + ", ".join(r[:8]), file=sys.stderr)
    return r[0]


def loc(x):
    s = x.get("source") or x
    return f"{s['file']}:{s['line']}" if s.get("file") else ""


def keep(e, args):
    return RANK[e["confidence"]] >= RANK[getattr(args, "min", "low")]


def fmt_edge(e, side):
    other = e["to"] if side == "to" else e["from"]
    extra = f" ({e['basis']})" if SHOW and e.get("basis") and e["confidence"] != "high" else ""
    syms = f" [imports {', '.join(e['symbols'])}]" if e.get("symbols") else ""
    return f"  {tg(e['confidence'])}{e['predicate']} {other}{syms}  {loc(e)}{extra}"


def cmd_show(args):
    g, nodes, out, inc = load()
    i = pick(nodes, args.name)
    n = nodes[i]
    print(f"{i}  {tg(n.get('confidence'))} {loc(n)}")
    for k in ("kind_of", "stereotypes", "annotations", "http", "path", "params", "returns"):
        if n.get(k):
            print(f"  {k}: {n[k]}")
    for f in n.get("facts", []):
        if keep(f, args):
            print(f"  fact {tg(f['confidence'])}{f['predicate']}: {f['value']}  {loc(f)}")
    print("outgoing:")
    for e in out[i]:
        if keep(e, args) and e["predicate"] != "has_method":
            print(fmt_edge(e, "to"))
    print("incoming:")
    for e in inc[i]:
        if keep(e, args):
            print(fmt_edge(e, "from"))
    if n.get("kind") == "type":
        print("methods:")
        for e in out[i]:
            if e["predicate"] == "has_method" and keep(e, args):
                m = nodes[e["to"]]
                print(f"  method {m.get('name')}({', '.join(m.get('params') or [])}) -> {m.get('returns', '')}  {loc(m)}")


def cmd_find(args):
    g, nodes, out, inc = load()
    for i in resolve(nodes, args.name):
        n = nodes[i]
        print(f"{i}  {tg(n.get('confidence'))} {loc(n)}")


def edges_to(args, preds, inbound):
    g, nodes, out, inc = load()
    i = pick(nodes, args.name)
    for e in (inc if inbound else out)[i]:
        if e["predicate"] in preds and keep(e, args):
            print(fmt_edge(e, "from" if inbound else "to"))


def cmd_deps(args):
    edges_to(args, {"depends_on", "instantiates", "extends", "implements", "implicitly_depends_on"}, False)


def cmd_dependents(args):
    edges_to(args, {"depends_on", "instantiates", "extends", "implements", "manages", "implicitly_depends_on"}, True)


def cmd_callers(args):
    if args.depth <= 1:
        return edges_to(args, {"calls"}, True)
    g, nodes, out, inc = load()
    start = pick(nodes, args.name)
    seen = {start}
    q = deque([(start, 0)])
    n = 0
    while q:
        cur, d = q.popleft()
        if d >= args.depth:
            continue
        for e in sorted(inc[cur], key=lambda x: x["from"]):
            if e["predicate"] != "calls" or not keep(e, args) or e["from"] in seen:
                continue
            seen.add(e["from"])
            q.append((e["from"], d + 1))
            n += 1
            print(f"  d={d + 1} {tg(e['confidence'])}{e['from']} -> {cur}  {loc(e)}")
    print(f"{n} callers within {args.depth} levels" if n else "no callers (in the graph)")


TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec|specs|testing)(/|$)|\.(test|spec)\.[a-z]+$|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)t/")


def is_test_node(n):
    return n.get("role") == "test" or bool(TEST_PATH.search(n.get("file") or ""))


def cmd_subclasses(args):
    """Every class that extends or implements the type, at any depth, one line each."""
    g, nodes, out, inc = load()
    types = {i: n for i, n in nodes.items() if i.startswith("type:") and n.get("kind_of") != "module"} or {i: n for i, n in nodes.items() if i.startswith("type:")}
    start = pick(types, args.name)
    wanted = args.overrides
    seen = {start}
    q = deque([(start, 0)])
    rows = []
    while q:
        cur, d = q.popleft()
        for e in sorted(inc[cur], key=lambda x: x["from"]):
            if e["predicate"] not in ("extends", "implements") or not keep(e, args) or e["from"] in seen:
                continue
            seen.add(e["from"])
            q.append((e["from"], d + 1))
            if is_test_node(nodes[e["from"]]) and not args.include_tests:
                continue
            own = {nodes[x["to"]].get("name") for x in out[e["from"]] if x["predicate"] == "has_method"}
            rows.append((d + 1, e, own))
    shown = [r for r in rows if not wanted or wanted in r[2]]
    for d, e, own in shown:
        mark = f" defines {wanted}" if wanted else ""
        print(f"  d={d} {tg(e['confidence'])}{e['from'].split(':', 1)[1]} ({e['predicate']} {e['to'].split(':', 1)[1].rsplit('.', 1)[-1]}){mark}  {loc(e)}")
    if not shown:
        print("no subclasses (in the graph)" if not wanted else f"no subclass defines {wanted} (in the graph)")
    else:
        print(f"{len(shown)} of {len(rows)} subclasses" if wanted else f"{len(rows)} subclasses")


def cmd_impact(args):
    g, nodes, out, inc = load()
    start = pick(nodes, args.name)
    rev = {"implicitly_depends_on", "depends_on", "instantiates", "extends", "implements", "calls", "handled_by", "renders", "manages", "tests"}
    methods_of = lambda t: [e["to"] for e in out[t] if e["predicate"] == "has_method"]
    owner_of = lambda m: nodes[m].get("owner", m)
    seen = {start: (0, "high")}
    q = deque([start])
    targets = {start}
    if start.startswith("type:"):
        for m in methods_of(start):
            targets.add(m)
            seen[m] = (0, "high")
            q.append(m)
    while q:
        cur = q.popleft()
        d, conf = seen[cur]
        if d >= args.depth:
            continue
        frontier = [cur]
        if cur.startswith("type:") and cur != start:
            frontier += methods_of(cur)
        for f in frontier:
            for e in inc[f]:
                if e["predicate"] not in rev or not keep(e, args):
                    continue
                c = min((conf, e["confidence"]), key=lambda x: RANK[x])
                nxt = e["from"]
                if nxt.startswith("method:"):
                    nxt = owner_of(nxt)
                if nxt not in seen:
                    seen[nxt] = (d + 1, c)
                    q.append(nxt)
    groups = defaultdict(list)
    for i, (d, c) in seen.items():
        if i in targets:
            continue
        groups[i.split(":", 1)[0]].append((d, i, c))
    for kind in ("route", "type", "template", "table"):
        for d, i, c in sorted(groups.get(kind, [])):
            role = ""
            if kind == "type":
                role = "test" if nodes[i].get("role") == "test" else ",".join(nodes[i].get("stereotypes") or [])
            print(f"{kind:8} d={d} {tg(c)}{i.split(':', 1)[1]} {role} {loc(nodes[i])}")
    if not groups:
        print("nothing depends on it (in the graph)")


def cmd_path(args):
    g, nodes, out, inc = load()
    a, b = pick(nodes, args.a), pick(nodes, args.b)
    prev = {a: None}
    q = deque([a])
    while q:
        cur = q.popleft()
        if cur == b:
            break
        for e in out[cur] + inc[cur]:
            if not keep(e, args) or e["predicate"] in ("declares_type", "has_method"):
                continue
            nxt = e["to"] if e["from"] == cur else e["from"]
            if nxt not in prev:
                prev[nxt] = (cur, e)
                q.append(nxt)
    if b not in prev:
        sys.exit("no path")
    chain, cur = [], b
    while prev[cur]:
        p, e = prev[cur]
        chain.append((p, e, cur))
        cur = p
    for p, e, c in reversed(chain):
        arrow = "->" if e["from"] == p else "<-"
        print(f"{p} {arrow}[{e['predicate']}{' ' + e['confidence'] if SHOW else ''}] {c}  {loc(e)}")


def cmd_routes(args):
    g, nodes, out, inc = load()
    for i, n in sorted(nodes.items()):
        if n["kind"] != "route":
            continue
        if args.text and args.text.lower() not in i.lower():
            continue
        hs = [e for e in out[i] if e["predicate"] == "handled_by"]
        vs = [e for e in out[i] if e["predicate"] == "renders"]
        extra = [("redirect=" if f["predicate"] == "redirects_to" else "body=") + f["value"] + (" [" + f["confidence"] + "]" if SHOW else "") for f in n.get("facts", []) if f["predicate"] in ("redirects_to", "returns_body")]
        print(f"{i}  -> {', '.join(h['to'].split(':', 1)[1] for h in hs)}  {' '.join(['view=' + v['to'].split(':', 1)[1] + (' [' + v['confidence'] + ']' if SHOW else '') for v in vs] + extra)}  {loc(n)}")


def cmd_entity(args):
    g, nodes, out, inc = load()
    i = pick(nodes, args.name)
    n = nodes[i]
    print(f"{i} {loc(n)}")
    for f in n.get("facts", []):
        print(f"  {tg(f['confidence'])}{f['predicate']}: {f['value']} {loc(f)}")
    for e in out[i]:
        if e["predicate"] in ("maps_to_table", "relates_to", "extends"):
            print(fmt_edge(e, "to"))
    for e in inc[i]:
        if e["predicate"] == "manages":
            print(fmt_edge(e, "from"))


def cmd_tests(args):
    g, nodes, out, inc = load()
    i = pick(nodes, args.name)
    for e in inc[i]:
        if e["predicate"] == "tests":
            print(fmt_edge(e, "from"))


def cmd_stats(args):
    g, nodes, out, inc = load()
    m = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    print(f"{m['generator']} from {m['source_commit']}: {m['claims']} claims{' ' + str(m['claims_by_confidence']) if SHOW else ''}")
    kinds = defaultdict(int)
    for n in nodes.values():
        kinds[n["kind"]] += 1
    print("nodes:", dict(sorted(kinds.items())))


# @@STALE_BEGIN
def cmd_stale(args):
    import hashlib
    m = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    root = HERE.parent
    bad = 0
    for path, digest in sorted(m["sources"].items()):
        f = root / path
        cur = hashlib.sha256(f.read_bytes()).hexdigest() if f.exists() else None
        if cur != digest:
            bad += 1
            print(f"{'deleted' if cur is None else 'changed'} since generation: {path}")
    print(f"{bad} of {len(m['sources'])} source files behind this knowledge have changed since it was generated" if bad else "generated knowledge matches the current source files")
# @@STALE_END


def main(argv=None):
    p = argparse.ArgumentParser(prog="kg", description="Auto-generated code graph. [high]=stated in source, [medium]=deterministic inference.")
    p.add_argument("--min", choices=("high", "medium", "low"), default="low", help="hide edges below this confidence")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn, extra in (("show", cmd_show, ["name"]), ("find", cmd_find, ["name"]), ("deps", cmd_deps, ["name"]), ("dependents", cmd_dependents, ["name"]),
                            ("callers", cmd_callers, ["name"]), ("subclasses", cmd_subclasses, ["name"]), ("impact", cmd_impact, ["name"]), ("entity", cmd_entity, ["name"]), ("tests", cmd_tests, ["name"]),
                            ("path", cmd_path, ["a", "b"]), ("routes", cmd_routes, []), ("stats", cmd_stats, [])) + ((("stale", cmd_stale, []),) if "cmd_stale" in globals() else ()):
        s = sub.add_parser(name)
        s.add_argument("--min", choices=("high", "medium", "low"), default="low")
        for a in extra:
            s.add_argument(a)
        if name == "routes":
            s.add_argument("text", nargs="?")
        if name == "impact":
            s.add_argument("--depth", type=int, default=4)
        if name == "callers":
            s.add_argument("--depth", type=int, default=1, help="follow callers of callers this many levels")
        if name == "subclasses":
            s.add_argument("--overrides", metavar="METHOD", help="only subclasses that define this method")
            s.add_argument("--include-tests", action="store_true", help="also list classes in test files")
        s.set_defaults(fn=fn)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
