"""Check the call graph against the source text. Both numbers are deliberately simple and reproducible:

precision: of the sampled `calls` edges, the share whose recorded line really contains the callee's name followed by `(`.
recall (lower bound): for methods whose name is defined once in the repository, the share of textual call sites
(`name(` outside comments and declarations, in non-test source) that some `calls` edge to that method covers at that
file and line. Calls through values of unknown type are the usual misses; the list shows examples so you can judge.
"""
import json
import random
import re
from collections import defaultdict
from pathlib import Path

from . import generate as gen

DECL = re.compile(r"^\s*(?:(?:public|private|protected|static|final|abstract|async|override|export|default|get|set)\s+)*(?:def|function|class|interface)?\s*")
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec|specs|testing)(/|$)|\.(test|spec)\.[a-z]+$|(^|/)test_[^/]*\.py$|_test\.py$")


def short(node_id):
    return re.split(r"[#.]", node_id.split(":", 1)[1])[-1].split(" ")[-1]


def read_lines(cache, repo, rel):
    if rel not in cache:
        f = repo / rel
        cache[rel] = f.read_text(encoding="utf-8", errors="replace").splitlines() if f.exists() else []
    return cache[rel]


def is_declaration(line, name):
    s = line.strip()
    if s.startswith(("#", "//", "*", "/*")):
        return True
    return bool(re.match(rf"(?:(?:public|private|protected|static|final|abstract|async|override|export|default)\s+)*(?:def|function)\s+{re.escape(name)}\b", s) or re.search(rf"=\s*(?:async\s+)?function\s+{re.escape(name)}\s*\(", s))


def audit(repo: Path, sample: int = 300, seed: int = 1, show: int = 8) -> dict:
    graph = json.loads((repo / gen.OUT / "graph.json").read_text(encoding="utf-8"))
    nodes = {n["id"]: n for n in graph["nodes"]}
    calls = [e for e in graph["edges"] if e["predicate"] == "calls" and e.get("source", {}).get("file")]
    calls = [e for e in calls if not TEST_PATH.search(e["source"]["file"]) and e["to"] in nodes]
    named = [e for e in calls if short(e["to"]) != "default"]  # `require('./x')(...)` calls a module's default export by no name
    cache: dict = {}
    rnd = random.Random(seed)

    picked = rnd.sample(named, min(sample, len(named)))
    wrong = []
    for e in picked:
        line = read_lines(cache, repo, e["source"]["file"])
        i = e["source"]["line"] - 1
        text = line[i] if 0 <= i < len(line) else ""
        if not re.search(rf"\b{re.escape(short(e['to']))}\s*\(", text):
            wrong.append((e, text.strip()[:100]))

    by_name = defaultdict(list)
    for nid, n in nodes.items():
        if nid.startswith("method:") and n.get("file") and not TEST_PATH.search(n["file"]):
            by_name[short(nid)].append(nid)
    unique = {name: ids[0] for name, ids in by_name.items() if len(ids) == 1 and len(name) > 3 and not name.startswith("__")}
    # One edge is kept per caller and callee pair, so a site counts as covered when its calling function has an edge to the callee.
    covered = defaultdict(list)
    for e in calls:
        c = nodes.get(e["from"], {})
        covered[(e["to"], e["source"]["file"])].append((c.get("line", e["source"]["line"]), c.get("end_line", e["source"]["line"])))
    names = sorted(unique)
    rnd.shuffle(names)
    sites = hit = own_sites = own_hit = 0
    missed = []
    sources = [p for p in gen.source_files(repo) if not TEST_PATH.search(p) and p.endswith((".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".java"))]
    wanted = {n: re.compile(rf"\b{re.escape(n)}\s*\(") for n in names[: max(50, sample // 3)]}
    for rel in sources:
        for ln, text in enumerate(read_lines(cache, repo, rel), 1):
            for name, rx in wanted.items():
                m = rx.search(text) if name in text else None
                if m and not is_declaration(text, name):
                    sites += 1
                    in_covered = any(a <= ln <= b for a, b in covered.get((unique[name], rel), ()))
                    hit += in_covered
                    # the extractor resolves bare calls and self./this. calls; other receivers need type knowledge it may not have
                    own = bool(re.search(rf"(?<![\w.]){re.escape(name)}\s*\(|\b(?:self|this|cls)\.{re.escape(name)}\s*\(", text))
                    if own:
                        own_sites += 1
                        own_hit += in_covered
                    if not in_covered:
                        missed.append((("" if own else "[other receiver] ") + name, f"{rel}:{ln}", text.strip()[:100]))
    return {
        "edges_sampled": len(picked), "edges_total": len(calls), "edges_wrong": len(wrong),
        "wrong_examples": [(e["to"], f"{e['source']['file']}:{e['source']['line']}", t) for e, t in wrong[:show]],
        "methods_checked": len(wanted), "sites": sites, "sites_covered": hit, "own_sites": own_sites, "own_covered": own_hit,
        "missed_examples": rnd.sample(missed, min(show, len(missed))),
    }


def report(r: dict) -> str:
    p = 1 - r["edges_wrong"] / r["edges_sampled"] if r["edges_sampled"] else 0.0
    rc = r["sites_covered"] / r["sites"] if r["sites"] else 0.0
    own = r["own_covered"] / r["own_sites"] if r["own_sites"] else 0.0
    out = [f"precision: {p:.1%}  ({r['edges_sampled'] - r['edges_wrong']} of {r['edges_sampled']} sampled call edges, of {r['edges_total']}, cite a line that calls the callee)",
           f"recall (lower bound): {rc:.1%}  ({r['sites_covered']} of {r['sites']} textual call sites of {r['methods_checked']} uniquely named methods are covered by an edge)"]
    out.append(f"  of which bare, self. and this. calls: {own:.1%}  ({r['own_covered']} of {r['own_sites']}); the rest go through a receiver whose type is not known")
    if r["wrong_examples"]:
        out.append("edges whose line does not mention the callee:")
        out += [f"  {a} at {b}: {t}" for a, b, t in r["wrong_examples"]]
    if r["missed_examples"]:
        out.append("call sites with no edge (examples):")
        out += [f"  {n} at {loc}: {t}" for n, loc, t in r["missed_examples"]]
    return "\n".join(out)
