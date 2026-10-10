"""Deterministic extractors for non-Java sources: properties, SQL schemas, pom.xml, templates."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET

from .claims import HIGH, MEDIUM, Claim

SECRET = re.compile(r"(password|passwd|secret|token|api[-_.]?key|private[-_.]?key|credential)", re.I)


def _claim(subject, predicate, obj, conf, basis, file, line, evidence, extractor, **attrs):
    return Claim(subject, predicate, obj, conf, basis, file, line, line, evidence.strip()[:160], attrs, extractor)


def properties(path: str, data: bytes) -> list[Claim]:
    out = []
    profile = re.search(r"application-(\w+)\.properties$", path)
    for i, raw in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(("#", "!")) or "=" not in line:
            continue
        key, value = (x.strip() for x in line.split("=", 1))
        shown = "[redacted]" if SECRET.search(key) else value
        out.append(_claim(f"config:{key}", "has_value", shown, HIGH, "properties file entry", path, i, raw if shown == value else f"{key}=[redacted]", "properties",
                          profile=profile.group(1) if profile else "default"))
    return out


def sql_schema(path: str, data: bytes) -> list[Claim]:
    text = data.decode("utf-8", "replace")
    out = []
    db = path.split("/")[-2]
    for m in re.finditer(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"]?(\w+)[`\"]?\s*\((.*?)\)\s*(?:ENGINE[^;]*)?;", text, re.S | re.I):
        table, body = m.group(1), m.group(2)
        line0 = text[: m.start()].count("\n") + 1
        out.append(_claim(f"table:{table}", "defined_in", path, HIGH, "CREATE TABLE statement", path, line0, text.splitlines()[line0 - 1], "sql", db=db))
        depth, cur, parts = 0, "", []
        for ch in body:
            depth += ch == "("
            depth -= ch == ")"
            if ch == "," and depth == 0:
                parts.append(cur)
                cur = ""
            else:
                cur += ch
        parts.append(cur)
        for part in parts:
            p = " ".join(part.split())
            if not p:
                continue
            ln = line0 + text[m.start(): m.start() + m.group(0).find(part.strip()[:20])].count("\n") if part.strip()[:20] in m.group(0) else line0
            fk = re.search(r"FOREIGN\s+KEY\s*\(\s*[`\"]?(\w+)[`\"]?\s*\)\s*REFERENCES\s+[`\"]?(\w+)", p, re.I)
            if fk:
                out.append(_claim(f"table:{table}", "references", f"table:{fk.group(2)}", HIGH, "FOREIGN KEY clause", path, ln, p, "sql", column=fk.group(1), db=db))
            elif not re.match(r"(CONSTRAINT|PRIMARY|UNIQUE|KEY|INDEX|FULLTEXT)\b", p, re.I):
                col = re.match(r"[`\"]?(\w+)[`\"]?\s+(.*)", p)
                if col:
                    out.append(_claim(f"table:{table}", "has_column", col.group(1), HIGH, "column definition", path, ln, p, "sql", definition=col.group(2)[:80], db=db))
    return out


def pom(path: str, data: bytes) -> list[Claim]:
    out = []
    lines = data.decode("utf-8", "replace").splitlines()

    def find(needle, start=0):
        for i in range(start, len(lines)):
            if needle in lines[i]:
                return i + 1
        return 1

    def ev(n):
        return lines[n - 1] if 0 < n <= len(lines) else ""

    root = ET.fromstring(data)
    ns = {"m": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
    q = (lambda t: f"m:{t}") if ns else (lambda t: t)

    def get(el, tag):
        x = el.find(q(tag), ns)
        return x.text.strip() if x is not None and x.text else None

    out.append(_claim("build:project", "has_coordinates", f"{get(root, 'groupId')}:{get(root, 'artifactId')}", HIGH, "pom.xml coordinates", path, find("<artifactId>", find("</parent>")), ev(find("<artifactId>", find("</parent>"))), "pom"))
    parent = root.find(q("parent"), ns)
    if parent is not None:
        out.append(_claim("build:project", "has_parent", f"{get(parent, 'artifactId')}:{get(parent, 'version')}", HIGH, "pom.xml parent", path, find("<parent>"), ev(find("<parent>")), "pom"))
    props = root.find(q("properties"), ns)
    if props is not None:
        for p in props:
            tag = p.tag.split("}")[-1]
            if tag in ("java.version", "maven.compiler.release", "maven.compiler.source"):
                out.append(_claim("build:project", "has_property", f"{tag}={p.text.strip()}", HIGH, "pom.xml property", path, find(f"<{tag}>"), ev(find(f"<{tag}>")), "pom"))
    for dep in root.findall(f"{q('dependencies')}/{q('dependency')}", ns):
        aid, scope = get(dep, "artifactId"), get(dep, "scope") or "compile"
        ln = find(f"<artifactId>{aid}</artifactId>")
        out.append(_claim("build:project", "depends_on_library", f"{get(dep, 'groupId')}:{aid}", HIGH, "pom.xml dependency", path, ln, ev(ln), "pom", scope=scope))
    for plug in root.findall(f".//{q('plugins')}/{q('plugin')}", ns):
        aid = get(plug, "artifactId")
        ln = find(f"<artifactId>{aid}</artifactId>")
        out.append(_claim("build:project", "uses_plugin", aid or "", HIGH, "pom.xml plugin", path, ln, ev(ln), "pom"))
    return out


def template_view(path: str) -> str | None:
    m = re.match(r"src/main/resources/templates/(.+)\.html$", path)
    return m.group(1) if m else None


def template_claim(path: str, view: str) -> Claim:
    return _claim(f"template:{view}", "defined_in", path, HIGH, "template file exists", path, 1, "", "templates")


OPENAPI_FILE = re.compile(r"(^|/)(openapi|swagger)[^/]*\.(ya?ml|json)$", re.I)
HTTP_VERBS = ("get", "post", "put", "delete", "patch", "head", "options")


def openapi_operations(path: str, data: bytes) -> list[tuple[str, str, str | None, int]]:
    """(http method, path, operationId, line) for every operation in an OpenAPI/Swagger file. Line scanner for YAML (no YAML library), json for JSON."""
    text = data.decode("utf-8", "replace")
    lines = text.splitlines()
    ops = []
    if path.lower().endswith(".json"):
        try:
            doc = json.loads(text)
        except ValueError:
            return []
        for pth, item in (doc.get("paths") or {}).items():
            for verb, op in (item or {}).items():
                if verb.lower() in HTTP_VERBS and isinstance(op, dict):
                    ln = next((i for i, l in enumerate(lines, 1) if f'"{pth}"' in l), 1)
                    ops.append((verb.upper(), pth, op.get("operationId"), ln))
        return ops
    start = next((i for i, l in enumerate(lines) if re.match(r"^paths\s*:\s*$", l)), None)
    if start is None:
        return []
    pth, path_indent, verb, verb_indent = None, None, None, None
    for i in range(start + 1, len(lines)):
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent == 0:
            break
        m = re.match(r"^\s+['\"]?(/[^'\":]*)['\"]?\s*:\s*$", raw)
        if m and (path_indent is None or indent == path_indent):
            pth, path_indent, verb = m.group(1), indent, None
            continue
        if pth is None:
            continue
        m = re.match(r"^\s+(%s)\s*:\s*$" % "|".join(HTTP_VERBS), raw, re.I)
        if m and indent > path_indent and (verb_indent is None or indent == verb_indent):
            verb, verb_indent = m.group(1).upper(), indent
            ops.append([verb, pth, None, i + 1])
            continue
        m = re.match(r"^\s+operationId\s*:\s*['\"]?([\w.-]+)", raw)
        if m and verb and ops and ops[-1][2] is None and ops[-1][1] == pth and ops[-1][0] == verb:
            ops[-1][2] = m.group(1)
    return [tuple(o) for o in ops]


def openapi_claims(path: str, data: bytes, methods: dict[str, list[tuple[str, str]]]) -> list[Claim]:
    """Routes declared by an OpenAPI file. `methods` maps a method name to [(method id, source file)]; an operationId that names such a method in the same module links the route to it; classes named like web endpoints are preferred when several match."""
    out = []
    lines = data.decode("utf-8", "replace").splitlines()
    module = path.split("/src/")[0] + "/" if "/src/" in path else ""
    for verb, pth, opid, ln in openapi_operations(path, data):
        rid = f"route:{verb} {pth}"
        ev = lines[ln - 1] if 0 < ln <= len(lines) else ""
        out.append(_claim(rid, "defined_in", path, HIGH, "operation declared in an OpenAPI file", path, ln, ev, "openapi", http=verb, path=pth, operation_id=opid or ""))
        if opid:
            hits = [mid for mid, f in methods.get(opid, []) if f.startswith(module) and not f.startswith(module + "src/test/")]
            web = [h for h in hits if re.search(r"(Resource|Controller|Endpoint|Handler|Api)$", h.split("#")[0].rsplit(".", 1)[-1])]
            for mid in (web or hits)[:3]:
                out.append(_claim(rid, "handled_by", mid, MEDIUM, "OpenAPI operationId equals a method name in the same module (the interface itself may be generated at build time)", path, ln, ev, "openapi", http=verb, path=pth))
    return out
