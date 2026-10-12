"""Deterministic Java/Spring extractor built on tree-sitter. No network, no LLM.

Confidence tiers:
  high    a fact written directly in the source (declaration, annotation, literal path).
  medium  deterministic but resolved by a heuristic (receiver type by declared name, view name
          through a constant, test-to-subject by naming convention).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import tree_sitter_java as tsj
from tree_sitter import Language, Parser

from .claims import HIGH, MEDIUM, Claim

EXTRACTOR = "java-treesitter"
PARSER = Parser(Language(tsj.language()))

STEREOTYPES = {
    "Controller": "controller", "RestController": "controller", "Service": "service", "Repository": "repository",
    "Component": "component", "Configuration": "configuration", "Entity": "entity", "MappedSuperclass": "entity-base",
    "SpringBootApplication": "application", "ControllerAdvice": "controller-advice", "RestControllerAdvice": "controller-advice",
    "Path": "resource", "ApplicationScoped": "bean", "RequestScoped": "bean", "SessionScoped": "bean", "Singleton": "bean", "Dependent": "bean",
    "RegisterRestClient": "rest-client", "QuarkusMain": "application", "Provider": "provider",
}
JAXRS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
PANACHE_REPO = re.compile(r"\b(?:Reactive)?Panache(?:Mongo)?Repository(?:Base)?\s*<\s*(\w+)")
PANACHE_ENTITY = re.compile(r"\bextends\s+(?:Reactive)?Panache(?:Mongo)?Entity(?:Base)?\b")
MAPPINGS = {"GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT", "DeleteMapping": "DELETE", "PatchMapping": "PATCH", "RequestMapping": "ANY"}
REPO_BASES = {"Repository", "CrudRepository", "JpaRepository", "PagingAndSortingRepository", "ListCrudRepository"}
RELATIONS = {"OneToMany", "ManyToOne", "OneToOne", "ManyToMany"}
TYPE_DECLS = {"class_declaration": "class", "interface_declaration": "interface", "enum_declaration": "enum", "record_declaration": "record", "annotation_type_declaration": "annotation"}
STR = re.compile(r'"((?:[^"\\]|\\.)*)"')


def text(node) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


def line_of(node) -> int:
    return node.start_point[0] + 1


@dataclass
class Ann:
    name: str
    args: str
    line: int
    pairs: dict = field(default_factory=dict)

    def strings(self) -> list[str]:
        return STR.findall(self.args)

    def paths(self) -> list[str]:
        """The path literals of a mapping annotation: `value`, `path`, or a leading positional string. Not `produces`, `consumes`, `headers` or `params`."""
        for key in ("value", "path"):
            if key in self.pairs:
                return STR.findall(self.pairs[key])
        return STR.findall(re.split(r"\b\w+\s*=", self.args, maxsplit=1)[0])


def annotations(node) -> list[Ann]:
    out: list[Ann] = []
    mods = next((c for c in node.children if c.type == "modifiers"), None)
    if mods is None:
        return out
    for a in mods.children:
        if a.type not in ("annotation", "marker_annotation"):
            continue
        name = text(a.child_by_field_name("name")).split(".")[-1]
        args_node = a.child_by_field_name("arguments")
        args = text(args_node)[1:-1] if args_node is not None else ""
        pairs = {}
        if args_node is not None:
            for ch in args_node.children:
                if ch.type == "element_value_pair":
                    pairs[text(ch.child_by_field_name("key"))] = text(ch.child_by_field_name("value"))
        out.append(Ann(name, args, line_of(a), pairs))
    return out


def modifiers(node) -> list[str]:
    mods = next((c for c in node.children if c.type == "modifiers"), None)
    return [text(c) for c in mods.children if c.type not in ("annotation", "marker_annotation")] if mods is not None else []


def type_idents(node) -> list[str]:
    """Simple type names mentioned in a type node, e.g. Page<Owner> -> [Page, Owner]."""
    out: list[str] = []

    def walk(n):
        if n.type == "type_identifier":
            out.append(text(n))
        elif n.type == "scoped_type_identifier":
            out.append(text(n))
            return
        for c in n.children:
            walk(c)

    if node is not None:
        walk(node)
    return out


def raw_types(node) -> list[str]:
    """Top-level simple type names of an extends/implements clause, without generic arguments."""
    out: list[str] = []
    if node is None:
        return out
    for c in node.children:
        if c.type in ("type_identifier", "generic_type", "scoped_type_identifier"):
            out += type_idents(c)[:1]
        elif c.type == "type_list":
            out += raw_types(c)
    return out


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


@dataclass
class TypeInfo:
    fqn: str
    simple: str
    package: str
    kind: str
    file: str
    line: int
    end_line: int
    node: object
    imports: dict
    anns: list
    is_test: bool


class JavaExtractor:
    def __init__(self, files: dict[str, bytes], templates: set[str]):
        self.files = files
        self.templates = templates
        self.types: dict[str, TypeInfo] = {}
        self.by_simple: dict[str, list[str]] = {}
        self.claims: list[Claim] = []
        self.lines: dict[str, list[str]] = {p: d.decode("utf-8", "replace").splitlines() for p, d in files.items()}
        self.trees = {p: PARSER.parse(d) for p, d in files.items()}

    def add(self, subject, predicate, obj, conf, basis, file, node_or_line, **attrs):
        if isinstance(node_or_line, int):
            line, end = node_or_line, node_or_line
        else:
            line, end = line_of(node_or_line), node_or_line.end_point[0] + 1
        ev = self.lines[file][line - 1].strip()[:160] if 0 < line <= len(self.lines[file]) else ""
        self.claims.append(Claim(subject, predicate, obj, conf, basis, file, line, end, ev, attrs, EXTRACTOR))

    def resolve(self, ti: TypeInfo, simple: str) -> str | None:
        if "." in simple:
            if simple in self.types:
                return simple
            head, _, rest = simple.partition(".")
            outer = self.resolve(ti, head)
            return f"{outer}.{rest}" if outer and f"{outer}.{rest}" in self.types else None
        if simple in ti.imports:
            fqn = ti.imports[simple]
            return fqn if fqn in self.types else None
        same = f"{ti.package}.{simple}"
        if same in self.types:
            return same
        cands = self.by_simple.get(simple, [])
        return cands[0] if len(cands) == 1 else None

    def run(self) -> list[Claim]:
        for path in sorted(self.files):
            if path.endswith("package-info.java"):
                continue
            self.collect(path)
        for fqn in sorted(self.types):
            self.extract_type(self.types[fqn])
        return self.claims

    def collect(self, path):
        root = self.trees[path].root_node
        package, imports = "", {}
        for c in root.children:
            if c.type == "package_declaration":
                package = text(next(x for x in c.children if x.type in ("scoped_identifier", "identifier")))
            elif c.type == "import_declaration":
                t = text(c).removeprefix("import").strip().rstrip(";").strip()
                if not t.startswith("static ") and not t.endswith("*"):
                    imports[t.split(".")[-1]] = t
        is_test = re.search(r"(^|/)src/test/", path) is not None

        def visit(node, outer):
            for c in node.children:
                if c.type in TYPE_DECLS:
                    simple = text(c.child_by_field_name("name"))
                    fqn = f"{outer or package}.{simple}"
                    ti = TypeInfo(fqn, simple, package, TYPE_DECLS[c.type], path, line_of(c), c.end_point[0] + 1, c, imports, annotations(c), is_test)
                    self.types[fqn] = ti
                    self.by_simple.setdefault(simple, []).append(fqn)
                    body = c.child_by_field_name("body")
                    if body is not None:
                        visit(body, fqn)

        visit(root, "")

    def tid(self, fqn):
        return f"type:{fqn}"

    def extract_type(self, ti: TypeInfo):
        node, path = ti.node, ti.file
        t = self.tid(ti.fqn)
        stereos = sorted({STEREOTYPES[a.name] for a in ti.anns if a.name in STEREOTYPES})
        head = node.text[: (node.child_by_field_name("body").start_byte - node.start_byte) if node.child_by_field_name("body") is not None else len(node.text)].decode("utf-8", "replace")
        panache_repo = PANACHE_REPO.search(head)
        if panache_repo and "repository" not in stereos:
            stereos = sorted(stereos + ["repository"])
        if PANACHE_ENTITY.search(head) and "entity" not in stereos:
            stereos = sorted(stereos + ["entity"])
        superclass = node.child_by_field_name("superclass")
        supers = raw_types(superclass)
        ifaces = []
        for c in node.children:
            if c.type in ("super_interfaces", "extends_interfaces"):
                ifaces += raw_types(c)
        role = "test" if ti.is_test else None
        if ti.kind == "interface" and any(n in REPO_BASES for n in ifaces):
            stereos = sorted(set(stereos) | {"repository"})
        self.add(t, "is_a", ti.kind, HIGH, "declaration", path, node,
                 stereotypes=stereos, annotations=[a.name for a in ti.anns], modifiers=modifiers(node), role=role, simple=ti.simple, package=ti.package, end_line=ti.end_line)
        if "entity" in stereos:
            tab = next((x for x in ti.anns if x.name == "Table"), None)
            if tab is not None and (tab.pairs.get("name") or tab.strings()):
                name = (tab.pairs.get("name", "").strip('"') or tab.strings()[0])
                self.add(t, "maps_to_table", f"table:{name}", HIGH, "@Table annotation", path, tab.line)
            else:
                self.add(t, "maps_to_table", f"table:{snake(ti.simple)}", MEDIUM, "no @Table annotation: default JPA naming assumed", path, node)
        self.add(f"package:{ti.package}", "declares_type", t, HIGH, "declaration", path, node)
        for s in supers:
            self.edge(ti, t, "extends", s, node, superclass)
        for s in ifaces:
            self.edge(ti, t, "implements" if ti.kind != "interface" else "extends", s, node, node)
        body = node.child_by_field_name("body")
        if node.type == "record_declaration":
            for p in node.child_by_field_name("parameters").children:
                if p.type == "formal_parameter":
                    self.add_field(ti, t, p, text(p.child_by_field_name("name")), p.child_by_field_name("type"), [])
        if "repository" in stereos and ti.kind == "interface":
            gen = re.search(r"\b(?:%s)\s*<\s*(\w+)" % "|".join(sorted(REPO_BASES)), text(node).split("{", 1)[0])
            if gen:
                r = self.resolve(ti, gen.group(1))
                if r:
                    self.add(t, "manages", self.tid(r), HIGH, "generic argument of the Repository base interface", path, node)
        if panache_repo:
            r = self.resolve(ti, panache_repo.group(1))
            if r:
                self.add(t, "manages", self.tid(r), HIGH, "generic argument of the Panache repository interface", path, node)
        if body is None:
            return
        fields = {}
        for m in body.children:
            if m.type == "field_declaration":
                ftype = m.child_by_field_name("type")
                anns = annotations(m)
                for d in [x for x in m.children if x.type == "variable_declarator"]:
                    name = text(d.child_by_field_name("name"))
                    fields[name] = (ftype, d, m)
                    self.add_field(ti, t, m, name, ftype, anns, d)
        consts = {}
        for name, (ftype, d, m) in fields.items():
            v = d.child_by_field_name("value")
            if v is not None and v.type == "string_literal" and "static" in modifiers(m):
                consts[name] = text(v)[1:-1]
        prefixes = [""]
        for a in ti.anns:
            if a.name in ("RequestMapping", "Path"):
                prefixes = a.paths() or [""]
        depends: dict[str, set] = {}
        for m in body.children:
            if m.type == "constructor_declaration":
                for p in m.child_by_field_name("parameters").children:
                    if p.type == "formal_parameter":
                        for n in type_idents(p.child_by_field_name("type")):
                            tgt = self.resolve(ti, n)
                            if tgt and tgt != ti.fqn:
                                depends.setdefault(tgt, set()).add("constructor")
                self.add(t, "has_constructor", f"{ti.simple}({', '.join(self.param_types(m))})", HIGH, "declaration", path, m)
        for name, (ftype, d, m) in fields.items():
            for n in type_idents(ftype):
                tgt = self.resolve(ti, n)
                if tgt and tgt != ti.fqn and "static" not in modifiers(m):
                    depends.setdefault(tgt, set()).add("field")
        for tgt, vias in sorted(depends.items()):
            self.add(t, "depends_on", self.tid(tgt), HIGH, "declared field or constructor parameter type", path, self.dep_line(ti, body, tgt), via="+".join(sorted(vias)))
        scope_fields = {n: [x for x in type_idents(v[0])] for n, v in fields.items()}
        for m in body.children:
            if m.type == "method_declaration":
                self.extract_method(ti, t, m, prefixes, consts, scope_fields)
        if ti.is_test:
            self.extract_test(ti, t)

    def dep_line(self, ti, body, tgt):
        simple = tgt.rsplit(".", 1)[-1]
        for m in body.children:
            if m.type in ("field_declaration", "constructor_declaration") and simple in [x.rsplit(".", 1)[-1] for x in type_idents(m.child_by_field_name("type") if m.type == "field_declaration" else m.child_by_field_name("parameters"))]:
                return m
        return ti.line

    def param_types(self, m) -> list[str]:
        out = []
        for p in m.child_by_field_name("parameters").children:
            if p.type in ("formal_parameter", "spread_parameter"):
                tn = p.child_by_field_name("type") or next((c for c in p.children if c.type.endswith("type") or c.type in ("type_identifier", "generic_type", "integral_type")), None)
                out.append(text(tn))
        return out

    def edge(self, ti, subject, predicate, simple, node, where):
        tgt = self.resolve(ti, simple)
        if tgt:
            self.add(subject, predicate, self.tid(tgt), HIGH, "declaration", ti.file, where)
        else:
            self.add(subject, predicate, f"external:{ti.imports.get(simple, simple)}", HIGH, "declaration (type not in this repository)", ti.file, where, external=True)

    def add_field(self, ti, t, decl, name, ftype, anns, declarator=None):
        typestr = text(ftype)
        target = None
        for n in type_idents(ftype):
            r = self.resolve(ti, n)
            if r:
                target = r
                break
        column = next((a.strings()[0] for a in anns if a.name == "Column" and a.strings()), None)
        if column is None:
            column = next((a.pairs.get("name", "").strip('"') for a in anns if a.name == "Column" and "name" in a.pairs), None)
        relation = next((a.name for a in anns if a.name in RELATIONS), None)
        attrs = {"name": name, "type": typestr, "annotations": [a.name for a in anns], "column": column, "relation": relation}
        self.add(t, "has_field", f"{name}: {typestr}", HIGH, "declaration", ti.file, declarator or decl, **attrs)
        if relation and target:
            self.add(t, "relates_to", self.tid(target), HIGH, f"@{relation} on field {name}", ti.file, decl, relation=relation, field=name)

    def extract_method(self, ti, t, m, prefixes, consts, scope_fields):
        name = text(m.child_by_field_name("name"))
        anns = annotations(m)
        mid = f"method:{ti.fqn}#{name}"
        params = self.param_types(m)
        ret = text(m.child_by_field_name("type"))
        self.add(t, "has_method", mid, HIGH, "declaration", ti.file, m, name=name, params=params, returns=ret, annotations=[a.name for a in anns], modifiers=modifiers(m))
        for a in anns:
            if a.name == "Query":
                self.add(mid, "has_query", a.strings()[0] if a.strings() else a.args, HIGH, "@Query annotation", ti.file, a.line)
        body = m.child_by_field_name("body")
        class_stereos = {STEREOTYPES.get(x.name) for x in ti.anns}
        if "controller" in class_stereos:
            for a in anns:
                if a.name in MAPPINGS:
                    self.route(ti, mid, m, a, prefixes, body, consts)
        elif "rest-client" not in class_stereos and any(a.name in JAXRS for a in anns):
            sub = next((x.paths() for x in anns if x.name == "Path"), None) or [""]
            direct = any(x.name == "Path" for x in ti.anns) and ti.kind != "interface"
            conf = HIGH if direct else MEDIUM
            basis = "@%s on method of a @Path resource class" if direct else "@%s on method of a class without its own @Path or of an interface; path is relative to wherever this resource is mounted (locator method, or client proxy interface)"
            for a in anns:
                if a.name in JAXRS:
                    for pre in prefixes:
                        for p in sub:
                            full = "/" + "/".join(x for x in (pre.strip("/"), p.strip("/")) if x)
                            self.add(f"route:{a.name} {full}", "handled_by", mid, conf, basis % a.name, ti.file, m, http=a.name, path=full)
        if body is None:
            return
        local = {}
        for p in m.child_by_field_name("parameters").children:
            if p.type == "formal_parameter":
                local[text(p.child_by_field_name("name"))] = type_idents(p.child_by_field_name("type"))
        seen = set()

        def walk(n):
            if n.type == "local_variable_declaration":
                tn = n.child_by_field_name("type")
                for d in [x for x in n.children if x.type == "variable_declarator"]:
                    local[text(d.child_by_field_name("name"))] = type_idents(tn)
            if n.type == "object_creation_expression":
                for tn in type_idents(n.child_by_field_name("type"))[:1]:
                    r = self.resolve(ti, tn)
                    if r and (mid, r) not in seen:
                        seen.add((mid, r))
                        self.add(mid, "instantiates", self.tid(r), HIGH, "new expression", ti.file, n)
            if n.type == "method_invocation":
                self.call(ti, mid, n, local, scope_fields, seen)
            for c in n.children:
                walk(c)

        walk(body)

    def call(self, ti, mid, n, local, fields, seen):
        recv = n.child_by_field_name("object")
        name = text(n.child_by_field_name("name"))
        if recv is None:
            if self.declares(ti, name):
                key = (mid, f"method:{ti.fqn}#{name}")
                if key not in seen and key[1] != mid:
                    seen.add(key)
                    self.add(mid, "calls", key[1], MEDIUM, "unqualified call to a method of the same class (overloads not distinguished)", ti.file, n)
            return
        cand = []
        if recv.type == "identifier":
            r = text(recv)
            cand = local.get(r) or fields.get(r) or []
            if not cand and r[:1].isupper():
                cand = [r]
        elif recv.type == "field_access" and text(recv.child_by_field_name("object")) == "this":
            cand = fields.get(text(recv.child_by_field_name("field")), [])
        target = self.resolve(ti, cand[0]) if cand else None
        if target:
            owner, basis = self.method_owner(target, name)
            key = (mid, f"method:{owner}#{name}")
            if key not in seen:
                seen.add(key)
                self.add(mid, "calls", key[1], MEDIUM, basis, ti.file, n)

    def supertypes(self, ti):
        out = []
        for c in ti.node.children:
            if c.type in ("superclass", "super_interfaces", "extends_interfaces"):
                for n in raw_types(c):
                    r = self.resolve(ti, n)
                    if r:
                        out.append(r)
        return out

    def method_owner(self, fqn, name):
        """Find the project type that declares `name`, walking up supertypes; otherwise keep the named type and say so."""
        seen, queue = {fqn}, [fqn]
        while queue:
            cur = queue.pop(0)
            if self.declares(self.types[cur], name):
                how = "receiver type taken from its declared type (overloads not distinguished)"
                return cur, how if cur == fqn else f"method found on supertype {self.types[cur].simple} of the declared receiver type (overloads not distinguished)"
            for sup in self.supertypes(self.types[cur]):
                if sup not in seen:
                    seen.add(sup)
                    queue.append(sup)
        return fqn, "receiver type taken from its declared type; method not declared in project source, likely inherited from a library type"

    def declares(self, ti, name):
        body = ti.node.child_by_field_name("body")
        return body is not None and any(m.type == "method_declaration" and text(m.child_by_field_name("name")) == name for m in body.children)

    def helpers(self, ti):
        cls = ti.node.child_by_field_name("body")
        return {text(m.child_by_field_name("name")): m.child_by_field_name("body") for m in (cls.children if cls is not None else []) if m.type == "method_declaration"}

    def route(self, ti, mid, m, a, prefixes, body, consts):
        http = MAPPINGS[a.name]
        if a.name == "RequestMapping":
            mm = re.search(r"RequestMethod\.(\w+)", a.args)
            http = mm.group(1) if mm else "ANY"
        paths = a.paths() or [""]
        for pre in prefixes:
            for p in paths:
                full = "/" + "/".join(x for x in (pre.strip("/"), p.strip("/")) if x)
                rid = f"route:{http} {full}"
                self.add(rid, "handled_by", mid, HIGH, f"@{a.name} on method", ti.file, m, http=http, path=full)
                if any(x.name == "ResponseBody" for x in annotations(m)):
                    self.add(rid, "returns_body", text(m.child_by_field_name("type")).replace("@ResponseBody", "").strip(), HIGH, "@ResponseBody on method", ti.file, m)
                for view, conf, basis in self.views(body, consts, self.helpers(ti)):
                    if view.startswith("redirect:") or view.startswith("forward:"):
                        self.add(rid, "redirects_to", view, conf, basis, ti.file, m)
                    elif view in self.templates:
                        self.add(rid, "renders", f"template:{view}", conf, basis, ti.file, m)

    def views(self, body, consts, helpers=None, depth=0):
        out = []
        if body is None:
            return out
        helpers = helpers or {}

        def concat(e):
            parts, ok = [], False
            def flat(n):
                nonlocal ok
                if n.type == "binary_expression" and text(n.child_by_field_name("operator")) == "+":
                    flat(n.child_by_field_name("left"))
                    flat(n.child_by_field_name("right"))
                elif n.type == "string_literal":
                    parts.append(text(n)[1:-1])
                    ok = True
                else:
                    parts.append("${" + text(n) + "}")
            flat(e)
            return "".join(parts) if ok and not parts[0].startswith("${") else None

        def walk(n):
            if n.type == "return_statement" and n.named_child_count:
                e = n.named_children[0]
                if e.type == "string_literal":
                    out.append((text(e)[1:-1], HIGH, "string literal returned"))
                elif e.type == "identifier" and text(e) in consts:
                    out.append((consts[text(e)], MEDIUM, "view name held in a constant of the same class"))
                elif e.type == "binary_expression":
                    full = concat(e)
                    if full:
                        out.append((full, MEDIUM, "concatenated string; non-literal parts shown as ${expression}"))
                elif e.type == "method_invocation" and depth < 2:
                    obj = e.child_by_field_name("object")
                    name = text(e.child_by_field_name("name"))
                    if (obj is None or text(obj) == "this") and name in helpers:
                        for v, _c, _b in self.views(helpers[name], consts, helpers, depth + 1):
                            out.append((v, MEDIUM, f"view returned by same-class helper method {name}()"))
            if n.type == "object_creation_expression" and "ModelAndView" in text(n.child_by_field_name("type")):
                lits = STR.findall(text(n.child_by_field_name("arguments")))
                if lits:
                    out.append((lits[0], HIGH, "ModelAndView view name"))
            for c in n.children:
                walk(c)

        walk(body)
        return sorted(set(out))

    def extract_test(self, ti, t):
        targets = {}
        for a in ti.anns:
            args = re.sub(r"\b\w*(?:include|exclude)Filters\s*=\s*(?:\{[^}]*\}|@[\w.]+(?:\([^)]*\))?)", "", a.args)
            for c in re.findall(r"(\w+)\.class", args):
                r = self.resolve(ti, c)
                if r:
                    targets[r] = (HIGH, f"@{a.name}({c}.class)", a.line)
        m = re.match(r"(.+?)(Tests?|IT|IntegrationTests?)$", ti.simple)
        if m:
            r = self.resolve(ti, m.group(1))
            if r and r not in targets:
                targets[r] = (MEDIUM, "test class name matches subject class name", ti.line)
        for r, (conf, basis, ln) in sorted(targets.items()):
            self.add(t, "tests", self.tid(r), conf, basis, ti.file, ln)
        body = ti.node.child_by_field_name("body")
        n = sum(1 for mm in (body.children if body is not None else []) if mm.type == "method_declaration" and any(a.name in ("Test", "ParameterizedTest") for a in annotations(mm)))
        if n:
            self.add(t, "has_test_methods", str(n), HIGH, "count of @Test methods", ti.file, ti.line)
