"""Render OKF-style markdown docs, a skill and an AGENTS.md snippet from claims. Pure functions of the claims."""

from __future__ import annotations

import json
from collections import defaultdict

from . import VERSION
from .claims import Claim


MODE = "confidence"  # confidence | provenance | none | tags (per-line tags only) | instruction (verify-against-source guidance only)
TIERS = ("high", "medium")
COMPONENTS = "both"  # docs | graph | both


def tag(conf) -> str:
    return f" `[{conf}]`" if MODE in ("confidence", "tags") else ""


def q(v) -> str:
    return json.dumps(v, ensure_ascii=False)


class Doc:
    def __init__(self, path, type_, title, description, resource=None, tags=()):
        self.path, self.type, self.title, self.description, self.resource, self.tags = path, type_, title, description, resource, list(tags)
        self.body: list[str] = []
        self.used: dict[str, Claim] = {}

    def use(self, c: Claim) -> str:
        self.used[c.id] = c
        return f"{tag(c.confidence).strip()} `{c.file}:{c.line}`".strip()

    def add(self, line=""):
        self.body.append(line)

    def finish(self, manifest) -> dict:
        srcs = {}
        for c in self.used.values():
            srcs[c.file] = manifest["sources"].get(c.file, "")[:12]
        counts = {"high": 0, "medium": 0}
        for c in self.used.values():
            counts[c.confidence] = counts.get(c.confidence, 0) + 1
        lowest = "low" if counts.get("low") else "medium" if counts["medium"] else "high"
        gen_tags = ["generated"] if MODE in ("confidence", "provenance") else []
        fm = ["---", f"type: {self.type}", f"title: {q(self.title)}", f"description: {q(self.description)}"]
        if self.resource:
            fm.append(f"resource: {q(self.resource)}")
        fm.append(f"tags: {q(gen_tags + self.tags)}")
        if manifest.get("source_commit_time"):
            fm.append(f"timestamp: {q(manifest['source_commit_time'])}")
        if MODE in ("confidence", "provenance"):
            fm += ["generation:", "  generated: true", f"  generator: {q(manifest['generator'])}", f"  source_commit: {q(manifest['source_commit'])}"]
            if MODE == "confidence":
                fm += [f"  lowest_confidence: {lowest}", f"  claims: {q(counts)}"]
            fm.append("  sources:")
            fm += [f"    - {{path: {q(p)}, sha256: {q(h)}}}" for p, h in sorted(srcs.items())]
        fm.append("---")
        commit = (manifest['source_commit'] or 'unknown')[:12]
        if MODE == "confidence":
            banner = (f"> **Auto-generated** by {manifest['generator']} from commit `{commit}`. Not written or reviewed by a person. "
                      f"Each line carries a confidence tag and its source. {tier_sentence()} "
                      f"Confidence says how the claim was derived, not whether the source has changed since. Open the cited source before relying on {reliance()} or before editing.")
        elif MODE == "provenance":
            banner = f"> **Auto-generated** by {manifest['generator']} from commit `{commit}`. Not written or reviewed by a person."
        else:
            banner = ""
        return {"text": "\n".join(fm) + "\n\n" + (banner + "\n\n" if banner else "") + "\n".join(self.body).rstrip() + "\n", "sources": [{"path": p, "sha256": h} for p, h in sorted(srcs.items())], "confidence": counts}


def tier_sentence() -> str:
    parts = ["`[high]` is stated directly in the source"]
    if "medium" in TIERS:
        parts.append("`[medium]` is a deterministic inference")
    else:
        parts.append("heuristic inferences are omitted, so a missing edge does not mean there is no relationship")
    if "low" in TIERS:
        parts.append("`[low]` was inferred by a language model reading the source and may be wrong")
    return "; ".join(parts) + "."


def label_list() -> str:
    items = ["`[high]` (stated in the source)"]
    if "medium" in TIERS:
        items.append("`[medium]` (deterministic inference)")
    if "low" in TIERS:
        items.append("`[low]` (model-inferred, may be wrong)")
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


def reliance() -> str:
    return "a `[low]` or `[medium]` line" if "low" in TIERS else "a `[medium]` line" if "medium" in TIERS else "a line"


def short(fqn: str) -> str:
    return fqn.split(":", 1)[-1].rsplit(".", 1)[-1]


def md_escape(s: str) -> str:
    return s.replace("|", "\\|")


FRESHNESS = False


def render_all(claims: list[Claim], graph: dict, manifest: dict, mode: str = "confidence", freshness: bool = False, components: str = "both") -> dict:
    global MODE, FRESHNESS, TIERS, COMPONENTS
    MODE, FRESHNESS, COMPONENTS = mode, freshness, components
    present = {c.confidence for c in claims}
    TIERS = tuple(t for t in ("high", "medium", "low") if t in present or t == "high" or (t == "medium" and manifest.get("min_tier", "medium") == "medium"))
    by_pred = defaultdict(list)
    for c in claims:
        by_pred[c.predicate].append(c)
    types = {}
    for c in by_pred["is_a"]:
        types[c.subject] = c
    methods_of = defaultdict(list)
    for c in by_pred["has_method"]:
        methods_of[c.subject].append(c)
    out: dict[str, Doc] = {}

    def is_test(t):
        return types[t].attrs.get("role") == "test"

    packages = sorted({types[t].attrs["package"] for t in types if not is_test(t)})
    main_types = sorted(t for t in types if not is_test(t))
    last = [p.split(".")[-1] for p in packages]
    pkg_link = {p: f"packages/{p.split('.')[-1] if last.count(p.split('.')[-1]) == 1 else p}.md" for p in packages}
    for p in packages:
        d = Doc(f"okf/{pkg_link[p]}", "module", f"Package {p}", f"Types, routes and dependencies of the {p} package.", resource=None, tags=["package"])
        ts = [t for t in main_types if types[t].attrs["package"] == p]
        d.add(f"# Package `{p}`")
        d.add()
        d.add("| Type | Kind | Role | Source |")
        d.add("|---|---|---|---|")
        for t in ts:
            c = types[t]
            role = ", ".join(c.attrs.get("stereotypes") or []) or "-"
            d.add(f"| `{short(t)}` | {c.object} | {role} | {d.use(c)} |")
        deps = [c for c in by_pred["depends_on"] if c.subject in ts]
        ext = [c for c in by_pred["extends"] + by_pred["implements"] if c.subject in ts]
        if ext:
            d.add()
            d.add("## Inheritance")
            for c in sorted(ext, key=lambda c: (c.subject, c.object)):
                d.add(f"- `{short(c.subject)}` {c.predicate} `{short(c.object)}` {d.use(c)}")
        if deps:
            d.add()
            d.add("## Depends on (constructor or field types)")
            for c in sorted(deps, key=lambda c: (c.subject, c.object)):
                d.add(f"- `{short(c.subject)}` -> `{short(c.object)}` ({c.attrs.get('via')}) {d.use(c)}")
        rts = [c for c in by_pred["handled_by"] if c.object.split("#")[0].replace("method:", "type:") in ts]
        if rts:
            d.add()
            d.add("## Routes")
            for c in sorted(rts, key=lambda c: (c.attrs["path"], c.attrs["http"])):
                d.add(f"- `{c.attrs['http']} {c.attrs['path']}` -> `{short(c.object.split('#')[0])}#{c.object.split('#')[1]}` {d.use(c)}")
        calls = [c for c in by_pred["calls"] if c.subject.split("#")[0].replace("method:", "type:") in ts and c.object.split("#")[0].replace("method:", "type:") not in (c.subject.split("#")[0].replace("method:", "type:"),)]
        if calls:
            d.add()
            d.add("## Cross-class calls")
            for c in sorted(calls, key=lambda c: (c.subject, c.object)):
                a, b = c.subject.split(":", 1)[1], c.object.split(":", 1)[1]
                d.add(f"- `{short(a.split('#')[0])}#{a.split('#')[1]}` calls `{short(b.split('#')[0])}#{b.split('#')[1]}` {d.use(c)}")
        out[d.path] = d

    d = Doc("okf/routes.md", "api", "HTTP routes", "Every request mapping with its handler method, view and redirects.", tags=["java", "spring", "routes"])
    d.add("# HTTP routes")
    d.add()
    d.add("| Method | Path | Handler | View | Redirect | Source |")
    d.add("|---|---|---|---|---|---|")
    rend = defaultdict(list)
    for c in by_pred["renders"]:
        rend[c.subject].append(c)
    redir = defaultdict(list)
    for c in by_pred["redirects_to"]:
        redir[c.subject].append(c)
    body_of = {c.subject: c for c in by_pred["returns_body"]}
    for c in sorted(by_pred["handled_by"], key=lambda c: (c.attrs["path"], c.attrs["http"])):
        views = ", ".join(f"`{x.object.split(':', 1)[1]}`{tag(x.confidence)}" for x in rend.get(c.subject, []))
        if c.subject in body_of:
            b = body_of[c.subject]
            views = f"response body `{b.object}`{tag(b.confidence)}"
            d.use(b)
        views = views or "-"
        reds = ", ".join(f"`{x.object}`{tag(x.confidence)}" for x in redir.get(c.subject, [])) or "-"
        for x in rend.get(c.subject, []) + redir.get(c.subject, []):
            d.use(x)
        h = c.object.split(":", 1)[1]
        d.add(f"| {c.attrs['http']} | `{c.attrs['path']}` | `{short(h.split('#')[0])}#{h.split('#')[1]}` | {views} | {reds} | {d.use(c)} |")
    out[d.path] = d

    d = Doc("okf/data-model.md", "data_model", "Data model", "JPA entities, their fields and relations, repositories, and the SQL schema.", tags=["java", "jpa", "sql"])
    d.add("# Data model")
    d.add()
    d.add("## Entities")
    ents = [t for t in main_types if {"entity", "entity-base"} & set(types[t].attrs.get("stereotypes") or [])]
    fields_of = defaultdict(list)
    for c in by_pred["has_field"]:
        fields_of[c.subject].append(c)
    tab = {c.subject: c for c in by_pred["maps_to_table"]}
    ext_of = {c.subject: c.object for c in by_pred["extends"]}
    rel_of = defaultdict(list)
    for c in by_pred["relates_to"]:
        rel_of[c.subject].append(c)
    manages = defaultdict(list)
    for c in by_pred["manages"]:
        manages[c.object].append(c)
    for t in ents:
        c = types[t]
        d.add()
        d.add(f"### `{short(t)}` ({', '.join(c.attrs.get('stereotypes') or [])}) {d.use(c)}")
        if t in ext_of:
            d.add(f"- extends `{short(ext_of[t])}`")
        if t in tab:
            d.add(f"- table `{tab[t].object.split(':', 1)[1]}` {d.use(tab[t])}")
        for f in fields_of[t]:
            extra = f", column `{f.attrs['column']}`" if f.attrs.get("column") else ""
            rel = f", relation @{f.attrs['relation']}" if f.attrs.get("relation") else ""
            d.add(f"- field `{f.object}`{extra}{rel} {d.use(f)}")
        for r in rel_of[t]:
            d.add(f"- @{r.attrs['relation']} -> `{short(r.object)}` via `{r.attrs['field']}` {d.use(r)}")
        for m in manages.get(t, []):
            d.add(f"- managed by repository `{short(m.subject)}` {d.use(m)}")
    d.add()
    d.add("## Repository query methods")
    for c in sorted(by_pred["has_query"], key=lambda c: c.subject):
        d.add(f"- `{short(c.subject.split('#')[0])}#{c.subject.split('#')[1]}`: `{md_escape(c.object)}` {d.use(c)}")
    d.add()
    d.add("## SQL schema")
    cols = defaultdict(list)
    for c in by_pred["has_column"]:
        if c.subject.startswith("table:"):
            cols[(c.subject, c.attrs.get("db"))].append(c)
    fks = defaultdict(list)
    for c in by_pred["references"]:
        fks[(c.subject, c.attrs.get("db"))].append(c)
    defs = {(c.subject, c.attrs.get("db")): c for c in by_pred["defined_in"] if c.subject.startswith("table:") and c.extractor == "sql"}
    for key in sorted(defs, key=lambda k: (k[1] or "", k[0])):
        c = defs[key]
        d.add(f"- `{key[0].split(':', 1)[1]}` in {key[1]} schema: {', '.join('`' + x.object + '`' for x in cols[key])} {d.use(c)}")
        for f in fks[key]:
            d.add(f"  - `{f.attrs['column']}` references `{f.object.split(':', 1)[1]}` {d.use(f)}")
    out[d.path] = d

    d = Doc("okf/configuration.md", "config", "Configuration", "Spring properties by file and profile.", tags=["spring", "config"])
    d.add("# Configuration")
    byfile = defaultdict(list)
    for c in by_pred["has_value"]:
        byfile[c.file].append(c)
    for f in sorted(byfile):
        d.add()
        d.add(f"## `{f}`")
        for c in byfile[f]:
            d.add(f"- `{c.subject.split(':', 1)[1]}` = `{c.object}` {d.use(c)}")
    out[d.path] = d

    d = Doc("okf/build.md", "build", "Build and dependencies", "Coordinates, properties, scripts, libraries and plugins declared in pom.xml or package.json.", tags=["build"])
    d.add("# Build")
    for pred, title in (("has_coordinates", "Project"), ("has_parent", "Parent"), ("has_property", "Properties"), ("depends_on_library", "Libraries"), ("uses_plugin", "Plugins")):
        cs = by_pred[pred]
        if cs:
            d.add()
            d.add(f"## {title}")
            for c in cs:
                d.add(f"- `{c.object}`{' (' + c.attrs['scope'] + ')' if 'scope' in c.attrs else ''} {d.use(c)}")
    out[d.path] = d

    d = Doc("okf/testing.md", "testing", "Tests", "Test classes, the class each one targets, and how the link was derived.", tags=["tests"])
    d.add("# Tests")
    d.add()
    d.add("| Test class | Tests | Target | Link | Source |")
    d.add("|---|---|---|---|---|")
    tgt = defaultdict(list)
    for c in by_pred["tests"]:
        tgt[c.subject].append(c)
    nt = {c.subject: c for c in by_pred["has_test_methods"]}
    for t in sorted(t for t in types if is_test(t)):
        cs = tgt.get(t, [])
        tm = nt[t].object if t in nt else "0"
        if nt.get(t):
            d.use(nt[t])
        target = ", ".join(f"`{short(c.object)}`{tag(c.confidence)}" for c in cs) or "-"
        basis = "; ".join(c.basis for c in cs) or "-"
        for c in cs:
            d.use(c)
        d.add(f"| `{short(t)}` | {tm} | {target} | {md_escape(basis)} | {d.use(types[t])} |")
    out[d.path] = d

    if by_pred["purpose"] or by_pred["implicitly_depends_on"]:
        d = Doc("okf/inferred.md", "inferred", "Inferred relationships", "Purposes and implicit couplings inferred by a language model from the source. Not verified.", tags=["inferred"])
        d.add("# Inferred relationships")
        d.add()
        d.add("Written by a language model that read the source, not derived by a parser. Each entry names the lines it was inferred from.")
        if by_pred["purpose"]:
            d.add()
            d.add("## Purposes")
            for c in sorted(by_pred["purpose"], key=lambda c: c.subject):
                d.add(f"- `{short(c.subject.split('#')[0])}{'#' + c.subject.split('#')[1] if '#' in c.subject else ''}`: {c.object} {d.use(c)}")
        if by_pred["implicitly_depends_on"]:
            d.add()
            d.add("## Couplings not visible as a code reference")
            for c in sorted(by_pred["implicitly_depends_on"], key=lambda c: (c.subject, c.object)):
                d.add(f"- `{c.subject.split(':', 1)[1]}` depends on `{c.object.split(':', 1)[1]}`: {c.basis} {d.use(c)}")
        out[d.path] = d

    has = {
        "okf/routes.md": bool(by_pred["handled_by"]),
        "okf/data-model.md": any(by_pred[k] for k in ("has_field", "maps_to_table", "has_column", "relates_to", "manages", "references", "has_query")),
        "okf/configuration.md": bool(by_pred["has_value"]),
        "okf/build.md": any(c.file in ("pom.xml", "package.json") for c in claims),
    }
    for rel, present in has.items():  # a page with nothing on it only costs an agent a read
        if not present:
            out.pop(rel, None)

    d = Doc("okf/index.md", "index", "Repository knowledge" if MODE not in ("confidence", "provenance") else "Generated repository knowledge", "Entry point to documentation of this repository.", tags=["index"])
    d.add("# Repository knowledge" if MODE not in ("confidence", "provenance") else "# Generated repository knowledge")
    d.add()
    if MODE == "confidence":
        d.add(f"Produced by `{manifest['generator']}` from the source" + (", with model-inferred additions marked `[low]`." if "low" in TIERS else ", without a model.") + f" Every claim has a source location and one of {len(TIERS)} confidence tier{'s' if len(TIERS) > 1 else ''}:")
        d.add()
        for k, v in manifest["confidence_tiers"].items():
            if k in TIERS:
                d.add(f"- `{k}`: {v}")
        d.add()
        tot = ", ".join(f"{manifest['claims_by_confidence'].get(k, 0)} {k}" for k in TIERS)
        d.add(f"Totals: {manifest['claims']} claims ({tot})." + (" Query them with `.agent-knowledge/bin/kg` (see the skill `use-generated-knowledge`)." if COMPONENTS != "docs" else ""))
    elif MODE == "provenance":
        d.add(f"Produced by `{manifest['generator']}` from the source, without a model. Every line has a source location." + (" Query the graph with `.agent-knowledge/bin/kg`." if COMPONENTS != "docs" else ""))
    else:
        d.add("Documentation" + (" and a code graph" if COMPONENTS != "docs" else "") + " for this repository. Every line has a source location." + (" Query the graph with `.agent-knowledge/bin/kg`." if COMPONENTS != "docs" else ""))
    d.add()
    d.add("## Contents")
    for p in packages:
        d.add(f"- [{p}]({pkg_link[p]})")
    for name, rel in (("HTTP routes", "routes.md"), ("Data model", "data-model.md"), ("Configuration", "configuration.md"), ("Build", "build.md"), ("Tests", "testing.md")) + ((("Inferred relationships", "inferred.md"),) if by_pred["purpose"] or by_pred["implicitly_depends_on"] else ()):
        if f"okf/{rel}" in out:
            d.add(f"- [{name}]({rel})")
    out[d.path] = d

    sk = Doc("skills/use-generated-knowledge/SKILL.md", "skill", "use-generated-knowledge", "")
    cmds = """## Commands (`.agent-knowledge/bin/kg`)
- `kg show <Class|Class#method|route>`: what it is, where it is, everything it connects to.
- `kg callers <Class#method>` and `kg dependents <Class>`: who uses it.
- `kg impact <Class>`: transitive users, routes, templates and tests to re-check after a change.
- `kg routes [text]`, `kg entity <Name>`, `kg tests <Class>`, `kg path <A> <B>`, `kg find <text>`.
"""
    if MODE == "confidence":
        if "medium" in TIERS:
            cmds += "- Add `--min high` to hide medium- and low-confidence edges, or `--min medium` to hide low-confidence ones.\n" if "low" in TIERS else "- Add `--min high` to hide medium-confidence edges.\n"
    if FRESHNESS:
        cmds += "- `kg stale`: lists source files that changed since this knowledge was generated.\n"
    name = "use-generated-knowledge" if MODE in ("confidence", "provenance") else "use-repository-knowledge"
    if MODE == "confidence":
        intro = "Everything under `.agent-knowledge/` was generated by a script from the source. No person wrote or reviewed it."
        labels = """## Confidence labels
- `[high]`: stated directly in the source (a declaration, annotation, literal path, config entry). Safe to plan from; still cite the file when you change it.
""" + ("""- `[medium]`: deterministic but heuristic (a call target found from a declared type, a view found through a constant, a test linked by its name). Open the cited file before relying on it.
""" if "medium" in TIERS else """- Heuristic inferences (call targets, constant-resolved views, name-matched tests) are not included, so a missing edge is not evidence that there is none.
""") + ("""- `[low]`: inferred by a language model reading the source. It may be wrong. Treat it as a lead and check the cited lines.
""" if "low" in TIERS else "") + """- Confidence says how a claim was derived. It does not say whether the source changed after the knowledge was generated.
- Anything not in the graph is unknown, not absent. Search the source.

"""
    elif MODE == "provenance":
        intro = "Everything under `.agent-knowledge/` was generated by a script from the source. No person wrote or reviewed it."
        labels = ""
    else:
        intro = "`.agent-knowledge/` holds documentation and a code graph of this repository."
        if MODE == "instruction":
            intro += " Open the cited source file before relying on a line. If a line disagrees with the source, trust the source and say so in your final message."
        labels = ""
    desc = "Use before exploring this repository by hand. " + ("Explains the generated knowledge graph, its confidence labels, and how to check a claim." if MODE == "confidence" else "Explains the knowledge graph and how to query it.")
    text = f"""---
name: {name}
description: {desc}
---

# Using the repository knowledge

{intro}

{labels}{cmds}"""
    out[sk.path] = None
    if MODE == "confidence":
        snippet = """## Generated repository knowledge

`.agent-knowledge/` holds documentation and a code graph generated by a script, not written by people. Start with `.agent-knowledge/okf/index.md`, or query the graph with `.agent-knowledge/bin/kg`.
Each line is labelled """ + label_list() + f". Open the cited file before relying on {reliance()}. If a generated claim is wrong, trust the source and say so in your final message.\n"""
    elif MODE == "provenance":
        snippet = """## Generated repository knowledge

`.agent-knowledge/` holds documentation and a code graph generated by a script, not written by people. Start with `.agent-knowledge/okf/index.md`, or query the graph with `.agent-knowledge/bin/kg`.
"""
    else:
        snippet = """## Repository knowledge

`.agent-knowledge/` holds documentation and a code graph of this repository. Start with `.agent-knowledge/okf/index.md`, or query the graph with `.agent-knowledge/bin/kg`.
""" + ("Open the cited source file before relying on a line. If a line disagrees with the source, trust the source and say so in your final message.\n" if MODE == "instruction" else "")
    if COMPONENTS == "docs":
        snippet = snippet.replace(", or query the graph with `.agent-knowledge/bin/kg`", "").replace("documentation and a code graph", "documentation").replace(" and a code graph", "")
    elif COMPONENTS == "graph":
        snippet = snippet.replace("Start with `.agent-knowledge/okf/index.md`, or query the graph with `.agent-knowledge/bin/kg`.", "Query it with `.agent-knowledge/bin/kg`.").replace("documentation and a code graph", "a code graph").replace("holds documentation and", "holds")
    if COMPONENTS != "docs":
        snippet += f"Command reference: `.agent-knowledge/skills/{name}/SKILL.md`.\n"
    if FRESHNESS:
        snippet += "Run `.agent-knowledge/bin/kg stale` first. It lists source files that changed since the knowledge was generated; treat generated claims that cite those files as out of date and read the source instead.\n"
    res = {p: d.finish(manifest) for p, d in out.items() if d is not None}
    if COMPONENTS == "graph":
        res = {}
    if COMPONENTS != "docs":
        res[f"skills/{name}/SKILL.md"] = {"text": text, "sources": [], "confidence": {}}
    res["AGENTS.snippet.md"] = {"text": snippet, "sources": [], "confidence": {}}
    return res
