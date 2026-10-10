"""Orchestrates extraction and writes .agent-knowledge/ (claims, graph, manifest, docs)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from . import VERSION
from .claims import HIGH, LOW, MEDIUM, RANK, Claim, dump_claims, sha256_file
from .java import JavaExtractor
from . import resources

OUT = ".agent-knowledge"
SKIP_DIRS = {".git", "target", "build", "node_modules", ".gradle", ".idea", OUT}
EDGE_PREDICATES = {"declares_type", "extends", "implements", "depends_on", "calls", "instantiates", "handled_by", "renders", "maps_to_table",
                   "relates_to", "manages", "tests", "references", "has_method", "defined_in", "implicitly_depends_on"}


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def source_files(repo: Path) -> dict[str, bytes]:
    listed = git(repo, "ls-files", "-z")
    paths = [p for p in listed.split("\0") if p] if listed else [p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file()]
    out = {}
    for rel in sorted(paths):
        if set(Path(rel).parts) & SKIP_DIRS:
            continue
        low = rel.lower()
        if low.endswith((".java", ".properties", ".sql", ".html")) or rel == "pom.xml" or resources.OPENAPI_FILE.search(rel):
            if low.endswith(".properties") and "messages" in low:
                continue
            f = repo / rel
            if f.is_file():
                out[rel] = f.read_bytes()
    return out


def extract(files: dict[str, bytes]) -> list[Claim]:
    templates = {v for p in files if (v := resources.template_view(p))}
    claims = JavaExtractor({p: d for p, d in files.items() if p.endswith(".java")}, templates).run()
    for p, d in files.items():
        if p.endswith(".properties"):
            claims += resources.properties(p, d)
        elif p.endswith(".sql") and "schema" in p:
            claims += resources.sql_schema(p, d)
        elif p == "pom.xml":
            claims += resources.pom(p, d)
    methods: dict[str, list[tuple[str, str]]] = {}
    for c in claims:
        if c.predicate == "has_method":
            methods.setdefault(c.attrs.get("name", ""), []).append((c.object, c.file))
    for p, d in sorted(files.items()):
        if resources.OPENAPI_FILE.search(p):
            claims += resources.openapi_claims(p, d, methods)
    for v in sorted(templates):
        claims.append(resources.template_claim(f"src/main/resources/templates/{v}.html", v))
    return claims


def build_graph(claims: list[Claim]) -> dict:
    nodes: dict[str, dict] = {}
    edges = []

    def node(nid):
        if nid not in nodes:
            kind = nid.split(":", 1)[0]
            nodes[nid] = {"id": nid, "kind": kind, "label": nid.split(":", 1)[1]}
        return nodes[nid]

    for c in sorted(claims, key=lambda c: c.id):
        n = node(c.subject)
        src = {"file": c.file, "line": c.line}
        if c.predicate == "is_a":
            n.update(kind_of=c.object, file=c.file, line=c.line, end_line=c.end_line, **{k: v for k, v in c.attrs.items() if k in ("stereotypes", "annotations", "role", "simple", "package", "modifiers")})
            n["confidence"] = c.confidence
        elif c.predicate == "has_method":
            m = node(c.object)
            m.update(file=c.file, line=c.line, end_line=c.end_line, owner=c.subject, **{k: v for k, v in c.attrs.items() if k in ("name", "params", "returns", "annotations")})
            m["confidence"] = c.confidence
            edges.append({"from": c.subject, "to": c.object, "predicate": "has_method", "confidence": c.confidence, "source": src})
        elif c.predicate in EDGE_PREDICATES and ":" in c.object and c.object.split(":", 1)[0] in ("type", "method", "table", "template", "route", "package", "external"):
            node(c.object)
            edges.append({"from": c.subject, "to": c.object, "predicate": c.predicate, "confidence": c.confidence, "basis": c.basis, "source": src,
                          **({"via": c.attrs["via"]} if "via" in c.attrs else {}), **({"relation": c.attrs["relation"]} if "relation" in c.attrs else {})})
            if c.predicate == "handled_by":
                n.update(http=c.attrs["http"], path=c.attrs["path"], file=c.file, line=c.line)
            if c.predicate == "defined_in" and c.subject.startswith("template:"):
                n.update(file=c.file, line=1)
        else:
            n.setdefault("facts", []).append({"predicate": c.predicate, "value": c.object, "confidence": c.confidence, "source": src, **({"attrs": c.attrs} if c.attrs and c.predicate in ("has_field", "has_column", "has_value") else {})})
    for n in nodes.values():
        if "facts" in n:
            n["facts"].sort(key=lambda f: (f["source"]["file"], f["source"]["line"], f["predicate"], f["value"]))
    edges.sort(key=lambda e: (e["from"], e["predicate"], e["to"], e["source"]["file"], e["source"]["line"]))
    return {"nodes": [nodes[k] for k in sorted(nodes)], "edges": edges}


def strip_confidence(o):
    if isinstance(o, dict):
        return {k: strip_confidence(v) for k, v in o.items() if k not in ("confidence", "basis")}
    if isinstance(o, list):
        return [strip_confidence(v) for v in o]
    return o


def neutral_cli(cli: str) -> str:
    cli = cli.replace('Auto-generated knowledge: check [medium] edges against the source.', 'Repository code graph.')
    cli = cli.replace('description="Auto-generated code graph. [high]=stated in source, [medium]=deterministic inference."', 'description="Repository code graph."')
    cli = re.sub(r'    p.add_argument\("--min".*\n', '', cli)
    cli = re.sub(r'        s.add_argument\("--min".*\n', '', cli)
    cli = cli.replace("claims_by_confidence", "claims_by_tier").replace('"confidence"', '"_c"').replace("'confidence'", "'_c'").replace('"basis"', '"_b"').replace("'basis'", "'_b'")
    return cli


def load_extra(path: Path) -> list[Claim]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        s = r["source"]
        out.append(Claim(r["subject"], r["predicate"], r["object"], r.get("confidence", LOW), r.get("basis", "llm-inferred"), s["file"], int(s["line"]), int(s.get("end_line", 0)), r.get("evidence", ""), r.get("attrs", {}), "llm"))
    return out


def generate(repo: Path, out_dir: Path | None = None, mode: str = "confidence", freshness: bool = False, min_tier: str = "medium", components: str = "both", extra_claims: Path | None = None) -> dict:
    from . import render

    files = source_files(repo)
    claims = [c for c in extract(files) if RANK[c.confidence] >= RANK[min_tier]]
    if extra_claims:
        claims += load_extra(extra_claims)
    graph = build_graph(claims)
    head = git(repo, "rev-parse", "HEAD")
    when = git(repo, "log", "-1", "--format=%cI")
    used = sorted({c.file for c in claims})
    by_conf = {k: sum(1 for c in claims if c.confidence == k) for k in (HIGH, MEDIUM, LOW) if k != LOW or any(c.confidence == LOW for c in claims)}
    manifest = {
        "generator": f"kgen {VERSION}",
        "source_commit": head or None,
        "source_commit_time": when or None,
        "claims": len(claims),
        "claims_by_confidence": by_conf,
        "sources": {p: sha256_file(files[p]) for p in used},
        "scanned": {p: sha256_file(d) for p, d in sorted(files.items())},
        "docs": {},
        "min_tier": min_tier,
        "confidence_tiers": {
            "high": "Written directly in the source: declarations, annotations, literal paths, config entries.",
            "medium": "Deterministic but heuristic: call targets resolved by declared receiver type, view names via constants, tests linked by naming.",
            "low": "Inferred by a language model reading the source. May be wrong; check the cited lines.",
        },
    }
    out = out_dir or repo / OUT
    if mode not in ("confidence", "tags"):
        for k in ("claims_by_confidence", "confidence_tiers"):
            manifest.pop(k)
    docs = render.render_all(claims, graph, manifest, mode, freshness, components)
    for rel in list(docs):
        manifest["docs"][rel] = {"sources": docs[rel]["sources"], "confidence": docs[rel]["confidence"]}
    claims_text = dump_claims(claims)
    if mode not in ("confidence", "tags"):
        claims_text = "".join(json.dumps(strip_confidence(json.loads(l)), sort_keys=True, ensure_ascii=False) + "\n" for l in claims_text.splitlines())
        graph = strip_confidence(graph)
    files_out = {"claims.jsonl": claims_text, "graph.json": json.dumps(graph, sort_keys=True, indent=None, ensure_ascii=False) + "\n"}
    if components == "docs":
        del files_out["graph.json"], files_out["claims.jsonl"]
    for rel, d in docs.items():
        files_out[rel] = d["text"]
    cli = (Path(__file__).parent / "kg_cli.py").read_text(encoding="utf-8").replace('MODE = "confidence"', f'MODE = "{mode}"', 1)
    if not freshness:
        cli = re.sub(r"# @@STALE_BEGIN.*?# @@STALE_END\n\n\n", "", cli, flags=re.S)
    if mode not in ("confidence", "tags"):
        cli = neutral_cli(cli)
    if components != "docs":
        files_out["kg.py"] = cli
        files_out["bin/kg"] = '#!/bin/sh\nexec python3 "$(dirname "$0")/../kg.py" "$@"\n'
        files_out["bin/kg.cmd"] = '@echo off\r\nwhere python >nul 2>nul && (python "%~dp0..\\kg.py" %*) || (py -3 "%~dp0..\\kg.py" %*)\r\nexit /b %errorlevel%\r\n'
    if components == "graph":
        files_out["claims.jsonl"] = claims_text
    if mode not in ("confidence", "tags"):
        manifest = strip_confidence(manifest)
    files_out["manifest.json"] = json.dumps(manifest, sort_keys=True, indent=2) + "\n"
    written = []
    for rel, body in sorted(files_out.items()):
        f = out / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        if not f.exists() or f.read_bytes() != body.encode("utf-8"):
            f.write_text(body, encoding="utf-8", newline="\n")
            written.append(rel)
    if components != "docs":
        (out / "bin" / "kg").chmod(0o755)
    stale = [p.relative_to(out).as_posix() for p in out.rglob("*") if out_dir is None and p.is_file() and p.relative_to(out).as_posix() not in files_out]
    for rel in stale:
        (out / rel).unlink()
    return {"claims": len(claims), "by_confidence": by_conf, "written": written, "removed": stale, "docs": len(docs)}
