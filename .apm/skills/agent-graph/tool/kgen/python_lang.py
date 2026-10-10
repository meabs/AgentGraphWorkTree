"""Python extractor (standard-library `ast`). Emits the same claim records as the Java extractor.

Modules become types of kind `module` (their top-level functions are methods), classes become types of kind
`class`. Calls are resolved only through imports, annotations, `self` and constructor assignments, so a call
whose target cannot be determined produces no claim.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

from .claims import HIGH, MEDIUM, Claim

EXTRACTOR = "python-ast"
HTTP = {"get", "post", "put", "delete", "patch", "head", "options"}
STEREOTYPES = {"dataclass": "dataclass"}


@dataclass
class Mod:
    name: str
    path: str
    pkg: str
    tree: ast.Module
    is_test: bool
    names: dict = field(default_factory=dict)   # bound name -> ("cls", fqn) | ("func", mod, name) | ("mod", dotted) | ("from", mod, name)
    imported: set = field(default_factory=set)  # dotted modules this module imports


@dataclass
class Cls:
    fqn: str
    simple: str
    mod: Mod
    node: ast.ClassDef
    bases: list = field(default_factory=list)
    methods: dict = field(default_factory=dict)  # name -> FunctionDef
    attrs: dict = field(default_factory=dict)    # attribute -> (class fqn, confidence)


def dotted(node) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def is_overload(f) -> bool:
    return any(decorator_name(d) == "overload" for d in f.decorator_list)


def decorator_name(d) -> str:
    d = d.func if isinstance(d, ast.Call) else d
    return (dotted(d) or "").split(".")[-1]


def package_roots(paths: set[str]) -> dict[str, str]:
    """Dotted module name per file: walk up while the directory has __init__.py; otherwise use the repository-relative path."""
    out = {}
    for p in sorted(paths):
        parts = p.split("/")
        d = len(parts) - 1
        while d > 0 and "/".join(parts[:d] + ["__init__.py"]) in paths:
            d -= 1
        stem = parts[-1][:-3]
        chain = parts[d:-1] + ([] if stem == "__init__" else [stem])
        out[p] = ".".join(chain) if chain else "__root__"
    return out


class PyExtractor:
    def __init__(self, files: dict[str, bytes]):
        self.lines = {p: d.decode("utf-8", "replace").splitlines() for p, d in files.items()}
        self.claims: list[Claim] = []
        self.mods: dict[str, Mod] = {}
        self.classes: dict[str, Cls] = {}
        self.funcs: dict[tuple[str, str], ast.AST] = {}
        self.skipped: list[str] = []
        trees = {}
        for p, d in files.items():
            try:
                trees[p] = ast.parse(d.decode("utf-8", "replace"), filename=p)
            except (SyntaxError, ValueError):
                self.skipped.append(p)
        names = package_roots(set(trees))
        taken: dict[str, str] = {}
        for p in sorted(trees):
            name = names[p]
            if name in taken:  # two files map to one dotted name: fall back to the path
                name = p[:-3].replace("/", ".")
            taken[name] = p
            parts = p.split("/")
            pkg = name if parts[-1] == "__init__.py" else (name.rsplit(".", 1)[0] if "." in name else "root")
            test = bool(re.search(r"(^|/)(tests?|testing)/", p)) or parts[-1].startswith("test_") or parts[-1].endswith("_test.py") or parts[-1] == "conftest.py"
            self.mods[name] = Mod(name, p, pkg, trees[p], test)
        self.by_simple: dict[str, list[str]] = {}
        for m in self.mods.values():
            if not m.is_test:
                self.by_simple.setdefault(m.name.rsplit(".", 1)[-1], []).append(m.name)

    # ---- recording
    def add(self, subject, predicate, obj, conf, basis, mod, node, **attrs):
        line = node if isinstance(node, int) else node.lineno
        end = line if isinstance(node, int) else (getattr(node, "end_lineno", None) or line)
        ls = self.lines[mod.path]
        ev = ls[line - 1].strip()[:160] if 0 < line <= len(ls) else ""
        self.claims.append(Claim(subject, predicate, obj, conf, basis, mod.path, line, end, ev, attrs, EXTRACTOR))

    # ---- symbol tables
    def collect(self):
        for m in self.mods.values():
            self.scan_scope(m, m.tree.body, m.name, None)
        for m in self.mods.values():
            for n in self.top_level(m.tree.body):
                if isinstance(n, (ast.Import, ast.ImportFrom)):
                    self.bind_import(m, n)

    def top_level(self, body):
        for n in body:
            yield n
            if isinstance(n, ast.If):
                yield from self.top_level(n.body + n.orelse)
            elif isinstance(n, ast.Try):
                yield from self.top_level(n.body + [x for h in n.handlers for x in h.body] + n.orelse)

    def scan_scope(self, m, body, prefix, owner):
        for n in self.top_level(body):
            if isinstance(n, ast.ClassDef):
                fqn = f"{prefix}.{n.name}"
                c = Cls(fqn, n.name, m, n)
                for x in n.body:
                    if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)) and (x.name not in c.methods or not is_overload(x)):
                        c.methods[x.name] = x  # the last real definition wins over @overload stubs
                self.classes[fqn] = c
                if owner is None:
                    m.names[n.name] = ("cls", fqn)
                self.scan_scope(m, n.body, fqn, c)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and owner is None:
                if (m.name, n.name) not in self.funcs or not is_overload(n):
                    self.funcs[(m.name, n.name)] = n
                m.names[n.name] = ("func", m.name, n.name)

    def bind_import(self, m, n):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name in self.mods:
                    m.imported.add(a.name)
                if a.asname:
                    m.names[a.asname] = ("mod", a.name)
                else:
                    m.names.setdefault(a.name.split(".")[0], ("mod", a.name.split(".")[0]))
            return
        base = n.module or ""
        if n.level:
            pkg = m.name if m.path.endswith("__init__.py") else (m.name.rsplit(".", 1)[0] if "." in m.name else "")
            up = pkg.split(".") if pkg else []
            up = up[: len(up) - (n.level - 1)] if n.level > 1 else up
            base = ".".join(x for x in (*up, n.module or "") if x)
        for a in n.names:
            if a.name == "*":
                continue
            bound = a.asname or a.name
            if f"{base}.{a.name}" in self.mods:
                m.names[bound] = ("mod", f"{base}.{a.name}")
                m.imported.add(f"{base}.{a.name}")
            else:
                m.names[bound] = ("from", base, a.name)
                if base in self.mods:
                    m.imported.add(base)

    def symbol(self, modname, name, depth=0):
        mi = self.mods.get(modname)
        if mi is None or depth > 6:
            return None
        e = mi.names.get(name)
        if e is None:
            return None
        if e[0] == "from":
            return self.symbol(e[1], e[2], depth + 1)
        return e

    def lookup(self, m, name):
        e = m.names.get(name)
        if e and e[0] == "from":
            return self.symbol(e[1], e[2], 1)
        return e

    # ---- expression resolution
    def entity(self, m, node, env, cur):
        """Resolve an expression to ("mod", n) | ("cls", fqn) | ("inst", fqn) | ("func", mod, n) | ("meth", owner fqn, n) | None."""
        if isinstance(node, ast.Name):
            if node.id in env:
                return env[node.id]
            if node.id in ("self", "cls") and cur is not None:
                return ("inst", cur.fqn)
            return self.lookup(m, node.id)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "super" and cur is not None:
                for b in cur.bases:
                    return ("inst", b)
                return None
            e = self.entity(m, node.func, env, cur)
            if e and e[0] == "cls":
                return ("inst", e[1])
            if e and e[0] in ("func", "meth"):
                return self.returns(e)
            return None
        if isinstance(node, ast.Attribute):
            e = self.entity(m, node.value, env, cur)
            if not e:
                return None
            if e[0] == "mod":
                sub = f"{e[1]}.{node.attr}"
                return ("mod", sub) if sub in self.mods else self.symbol(e[1], node.attr)
            if e[0] in ("cls", "inst"):
                c = self.classes.get(e[1])
                if c is None:
                    return None
                owner = self.owner(c.fqn, node.attr)
                if owner:
                    return ("meth", owner, node.attr)
                hit = self.attr_type(c.fqn, node.attr)
                return ("inst", hit[0]) if hit else None
        return None

    def annotation_class(self, m, ann):
        if ann is None:
            return None
        if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
            try:
                ann = ast.parse(ann.value, mode="eval").body
            except SyntaxError:
                return None
        for n in ast.walk(ann):
            if isinstance(n, (ast.Name, ast.Attribute)):
                e = self.entity(m, n, {}, None)
                if e and e[0] == "cls":
                    return e[1]
        return None

    def returns(self, e):
        node = self.funcs.get((e[1], e[2])) if e[0] == "func" else (self.classes[e[1]].methods.get(e[2]) if e[1] in self.classes else None)
        if node is None or node.returns is None:
            return None
        mod = self.mods[e[1]] if e[0] == "func" else self.classes[e[1]].mod
        fqn = self.annotation_class(mod, node.returns)
        return ("inst", fqn) if fqn else None

    def supers(self, fqn):
        return self.classes[fqn].bases if fqn in self.classes else []

    def owner(self, fqn, name):
        seen, queue = {fqn}, [fqn]
        while queue:
            cur = queue.pop(0)
            if cur in self.classes and name in self.classes[cur].methods:
                return cur
            for b in self.supers(cur):
                if b not in seen:
                    seen.add(b)
                    queue.append(b)
        return None

    def attr_type(self, fqn, name):
        seen, queue = {fqn}, [fqn]
        while queue:
            cur = queue.pop(0)
            if cur in self.classes and name in self.classes[cur].attrs:
                return self.classes[cur].attrs[name]
            for b in self.supers(cur):
                if b not in seen:
                    seen.add(b)
                    queue.append(b)
        return None

    def local_env(self, m, c, f):
        env = {}
        args = f.args.posonlyargs + f.args.args + f.args.kwonlyargs
        for a in args + [x for x in (f.args.vararg, f.args.kwarg) if x]:
            t = self.annotation_class(m, a.annotation)
            if t:
                env[a.arg] = ("inst", t)
        for n in sorted((x for x in ast.walk(f) if isinstance(x, (ast.Assign, ast.AnnAssign, ast.With))), key=lambda x: (x.lineno, x.col_offset)):
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                e = self.entity(m, n.value, env, c)
                if e and e[0] == "inst":
                    env.setdefault(n.targets[0].id, e)
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                t = self.annotation_class(m, n.annotation)
                if t:
                    env.setdefault(n.target.id, ("inst", t))
            elif isinstance(n, ast.With):
                for item in n.items:
                    if isinstance(item.optional_vars, ast.Name):
                        e = self.entity(m, item.context_expr, env, c)
                        if e and e[0] == "inst":
                            env.setdefault(item.optional_vars.id, e)
        return env

    def collect_attrs(self):
        for c in self.classes.values():
            c.bases = []
            for b in c.node.bases:
                e = self.entity(c.mod, b, {}, None)
                if e and e[0] == "cls" and e[1] != c.fqn:
                    c.bases.append(e[1])
        for c in self.classes.values():
            for x in c.node.body:
                if isinstance(x, ast.AnnAssign) and isinstance(x.target, ast.Name):
                    t = self.annotation_class(c.mod, x.annotation)
                    if t:
                        c.attrs.setdefault(x.target.id, (t, HIGH, x.lineno))
        for c in self.classes.values():
            for f in c.methods.values():
                env = None
                for n in ast.walk(f):
                    tgt = None
                    if isinstance(n, ast.Assign) and len(n.targets) == 1:
                        tgt, value, ann = n.targets[0], n.value, None
                    elif isinstance(n, ast.AnnAssign):
                        tgt, value, ann = n.target, n.value, n.annotation
                    if tgt is not None and isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name) and tgt.value.id == "self" and tgt.attr not in c.attrs:
                        t = self.annotation_class(c.mod, ann) if ann is not None else None
                        if t:
                            c.attrs[tgt.attr] = (t, HIGH, n.lineno)
                        elif value is not None:
                            env = env if env is not None else self.local_env(c.mod, c, f)
                            e = self.entity(c.mod, value, env, c)
                            if e and e[0] == "inst":
                                c.attrs[tgt.attr] = (e[1], MEDIUM, n.lineno)

    # ---- emit
    def run(self) -> list[Claim]:
        self.collect()
        self.collect_attrs()
        for name in sorted(self.mods):
            self.emit_module(self.mods[name])
        return self.claims

    def tid(self, fqn):
        return f"type:{fqn}"

    def params(self, f):
        a = f.args
        out = [x.arg + (f": {ast.unparse(x.annotation)}" if x.annotation is not None else "") for x in a.posonlyargs + a.args + a.kwonlyargs]
        return [p for p in out if p.split(":")[0] not in ("self", "cls")]

    def method_claim(self, m, owner_id, fname, f):
        mods = (["async"] if isinstance(f, ast.AsyncFunctionDef) else []) + [d for d in (decorator_name(x) for x in f.decorator_list) if d in ("staticmethod", "classmethod", "property", "abstractmethod")]
        self.add(owner_id, "has_method", f"method:{owner_id.split(':', 1)[1]}#{fname}", HIGH, "declaration", m, f, name=fname, params=self.params(f), returns=ast.unparse(f.returns) if f.returns is not None else "",
                 annotations=[decorator_name(d) for d in f.decorator_list], modifiers=mods)

    def emit_module(self, m: Mod):
        t = self.tid(m.name)
        self.add(t, "is_a", "module", HIGH, "declaration", m, 1, stereotypes=[], annotations=[], modifiers=[], role="test" if m.is_test else None, simple=m.name.rsplit(".", 1)[-1], package=m.pkg, end_line=len(self.lines[m.path]) or 1)
        self.add(f"package:{m.pkg}", "declares_type", t, HIGH, "declaration", m, 1)
        for dep in sorted(m.imported - {m.name}):
            syms = sorted({v[2] for v in m.names.values() if v[0] == "from" and v[1] == dep})
            self.add(t, "depends_on", self.tid(dep), HIGH, "import statement", m, self.import_line(m, dep), via="import", symbols=syms)
        for (mod, fname), f in sorted(self.funcs.items()):
            if mod == m.name:
                self.method_claim(m, t, fname, f)
                self.emit_body(m, None, f, f"method:{m.name}#{fname}")
        for c in sorted((c for c in self.classes.values() if c.mod is m), key=lambda c: c.fqn):
            self.emit_class(m, c)
        if m.is_test:
            self.emit_test_links(m, t)

    def import_line(self, m, dep):
        for n in self.top_level(m.tree.body):
            if isinstance(n, ast.Import) and any(a.name == dep or a.name.startswith(dep + ".") for a in n.names):
                return n.lineno
            if isinstance(n, ast.ImportFrom) and any((e := m.names.get(a.asname or a.name)) and (e == ("mod", dep) or (e[0] == "from" and e[1] == dep)) for a in n.names):
                return n.lineno
        return 1

    def emit_class(self, m, c: Cls):
        t = self.tid(c.fqn)
        decos = [decorator_name(d) for d in c.node.decorator_list]
        self.add(t, "is_a", "class", HIGH, "declaration", m, c.node, stereotypes=sorted({STEREOTYPES[d] for d in decos if d in STEREOTYPES}), annotations=decos, modifiers=[], role="test" if m.is_test else None,
                 simple=c.simple, package=m.pkg, end_line=getattr(c.node, "end_lineno", c.node.lineno))
        self.add(f"package:{m.pkg}", "declares_type", t, HIGH, "declaration", m, c.node)
        for b in c.node.bases:
            e = self.entity(m, b, {}, None)
            text = dotted(b) or ast.unparse(b)
            if e and e[0] == "cls":
                self.add(t, "extends", self.tid(e[1]), HIGH, "declaration", m, b)
            elif text != "object":
                resolved = m.names.get(text.split(".")[0])
                label = text
                if resolved and resolved[0] == "from":
                    label = f"{resolved[1]}.{resolved[2]}" + ("." + ".".join(text.split(".")[1:]) if "." in text else "")
                elif resolved and resolved[0] == "mod":
                    label = ".".join([resolved[1], *text.split(".")[1:]])
                self.add(t, "extends", f"external:{label}", HIGH, "declaration (type not in this repository)", m, b, external=True)
        for name, (target, conf, line) in sorted(c.attrs.items()):
            if target != c.fqn:
                self.add(t, "depends_on", self.tid(target), conf, "annotated attribute type" if conf == HIGH else "attribute assigned from a constructor call or typed value", m, line, via="attribute")
        for fname, f in sorted(c.methods.items()):
            self.method_claim(m, t, fname, f)
            self.emit_body(m, c, f, f"method:{c.fqn}#{fname}")
        if m.is_test:
            n = sum(1 for f in c.methods if f.startswith("test"))
            if n:
                self.add(t, "has_test_methods", str(n), HIGH, "count of test_ methods", m, c.node)

    def emit_body(self, m, c, f, mid):
        for d in f.decorator_list:
            self.route(m, mid, d)
        env = self.local_env(m, c, f)
        seen = set()
        for n in sorted((x for x in ast.walk(f) if isinstance(x, ast.Call)), key=lambda x: (x.lineno, x.col_offset)):
            e = self.entity(m, n.func, env, c)
            if not e:
                continue
            if e[0] == "cls":
                key, pred, basis, target = ("i", e[1]), "instantiates", "call of a class resolved through imports or local definitions", self.tid(e[1])
            elif e[0] == "func":
                key, pred, basis, target = ("c", e[1], e[2]), "calls", "call of a function resolved through imports or local definitions", f"method:{e[1]}#{e[2]}"
            elif e[0] == "meth":
                key, pred, target = ("c", e[1], e[2]), "calls", f"method:{e[1]}#{e[2]}"
                basis = "method called on a receiver whose class is known from an annotation, self or a constructor call"
            else:
                continue
            if target == mid or key in seen:
                continue
            seen.add(key)
            self.add(mid, pred, target, MEDIUM, basis, m, n)

    def route(self, m, mid, d):
        if not (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)):
            return
        attr = d.func.attr
        if attr not in HTTP and attr not in ("route", "api_route"):
            return
        if not d.args or not (isinstance(d.args[0], ast.Constant) and isinstance(d.args[0].value, str)):
            return
        path = d.args[0].value
        verbs = [attr.upper()] if attr in HTTP else ["GET"]
        if attr in ("route", "api_route"):
            for kw in d.keywords:
                if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                    got = [e.value.upper() for e in kw.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                    verbs = got or verbs
        direct = isinstance(d.func.value, ast.Name) and d.func.value.id == "app"
        for v in verbs:
            self.add(f"route:{v} {path}", "handled_by", mid, HIGH if direct else MEDIUM,
                     "decorator on the application object" if direct else "decorator on a router or blueprint; the path is relative to where it is mounted", m, d, http=v, path=path)

    def emit_test_links(self, m, t):
        stem = m.name.rsplit(".", 1)[-1]
        subject = re.sub(r"^test_|_tests?$", "", stem)
        cands = self.by_simple.get(subject, []) if subject != stem else []
        if len(cands) == 1:
            self.add(t, "tests", self.tid(cands[0]), MEDIUM, "test module name matches module name", m, 1)
        n = sum(1 for (mod, f) in self.funcs if mod == m.name and f.startswith("test"))
        if n:
            self.add(t, "has_test_methods", str(n), HIGH, "count of test_ functions", m, 1)
        for c in sorted((c for c in self.classes.values() if c.mod is m), key=lambda c: c.fqn):
            mt = re.match(r"Test(\w+)$", c.simple)
            if mt:
                hits = [k for k, x in self.classes.items() if x.simple == mt.group(1) and not x.mod.is_test]
                if len(hits) == 1:
                    self.add(self.tid(c.fqn), "tests", self.tid(hits[0]), MEDIUM, "test class name matches class name", m, c.node)
