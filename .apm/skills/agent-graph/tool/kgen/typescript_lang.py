"""TypeScript / JavaScript / React / Node extractor (tree-sitter). Emits the same claim records as the Java extractor.

Modules become types of kind `module` (plain functions and hooks are their methods). Classes, interfaces and enums
become types; a React function component becomes a type of kind `component` with a `render` method. Names are
resolved through imports (relative paths and tsconfig `paths`), type annotations, `this` and `new`; a use whose
target cannot be determined produces no claim.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field

import tree_sitter_typescript as tsts
from tree_sitter import Language, Parser

from .claims import HIGH, MEDIUM, Claim

EXTRACTOR = "typescript-treesitter"
TS = Parser(Language(tsts.language_typescript()))
TSX = Parser(Language(tsts.language_tsx()))
EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
HTTP = {"get", "post", "put", "delete", "patch", "head", "options", "all"}
NEST_HTTP = {"Get": "GET", "Post": "POST", "Put": "PUT", "Delete": "DELETE", "Patch": "PATCH", "All": "ANY", "Head": "HEAD", "Options": "OPTIONS"}
STEREOTYPES = {"Controller": "controller", "Injectable": "injectable", "Module": "module", "Entity": "entity", "Component": "angular-component", "Resolver": "resolver"}
WRAPPERS = {"memo", "forwardRef", "observer", "styled", "withRouter", "connect"}
TEST_FILE = re.compile(r"(\.(test|spec)\.[cm]?[jt]sx?$)|(^|/)__tests__/")
NEXT_VERBS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")


def text(n) -> str:
    return n.text.decode("utf-8", "replace") if n is not None else ""


def line(n) -> int:
    return n.start_point[0] + 1


def strip_json(raw: str) -> dict:
    raw = re.sub(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/', lambda m: m.group(1) or "", raw, flags=re.S)
    raw = re.sub(r",(\s*[}\]])", r"\1", raw)
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def string_value(n) -> str | None:
    if n is None or n.type not in ("string", "template_string"):
        return None
    if n.type == "template_string" and any(c.type == "template_substitution" for c in n.children):
        return None
    return "".join(text(c) for c in n.children if c.type in ("string_fragment", "string_content")) if n.type == "string" else text(n)[1:-1]


@dataclass
class Mod:
    name: str
    path: str
    pkg: str
    root: object
    is_test: bool
    names: dict = field(default_factory=dict)    # local name -> entity | ("from", mod, exported) | ("ext", spec)
    exports: dict = field(default_factory=dict)  # exported name -> ("local", name) | ("from", mod, exported) | ("mod", mod)
    stars: list = field(default_factory=list)    # modules re-exported with `export *`
    imported: dict = field(default_factory=dict)  # dotted module -> line of the import
    objects: set = field(default_factory=set)     # module-level names bound to an object literal (may gain methods)
    cjs_default: bool = False                     # module.exports = <something>: require() returns that, not the namespace


@dataclass
class Cls:
    fqn: str
    simple: str
    mod: Mod
    node: object
    kind: str
    fn: object = None                              # the function node of a function component
    decorators: list = field(default_factory=list)
    bases: list = field(default_factory=list)      # resolved project classes
    methods: dict = field(default_factory=dict)    # name -> (node, decorators)
    attrs: dict = field(default_factory=dict)      # attribute -> (class fqn, confidence, line)


def dyn(segment: str) -> str:
    """Next.js dynamic segments: [id] -> {id}, [...rest] -> {rest*}, [[...rest]] -> {rest*?}."""
    m = re.fullmatch(r"\[\[\.\.\.([\w-]+)\]\]", segment)
    if m:
        return "{" + m.group(1) + "*?}"
    m = re.fullmatch(r"\[\.\.\.([\w-]+)\]", segment)
    if m:
        return "{" + m.group(1) + "*}"
    m = re.fullmatch(r"\[([\w-]+)\]", segment)
    return "{" + m.group(1) + "}" if m else segment


def is_component_name(name: str) -> bool:
    return name[:1].isupper()


def has_jsx(n) -> bool:
    stack = [n]
    while stack:
        x = stack.pop()
        if x.type in ("jsx_element", "jsx_self_closing_element", "jsx_fragment"):
            return True
        stack.extend(x.children)
    return False


def function_of(value):
    """The function node behind `const X = <value>`, looking through memo(...), forwardRef(...) and the like."""
    while value is not None:
        if value.type in ("arrow_function", "function_expression", "function", "generator_function"):
            return value
        if value.type == "call_expression":
            callee = text(value.child_by_field_name("function")).split(".")[-1]
            args = value.child_by_field_name("arguments")
            if callee in WRAPPERS and args is not None and args.named_child_count:
                value = args.named_children[0]
                continue
        if value.type in ("parenthesized_expression", "as_expression", "satisfies_expression"):
            value = value.named_children[0] if value.named_child_count else None
            continue
        return None
    return None


class TsExtractor:
    def __init__(self, files: dict[str, bytes], configs: dict[str, bytes] | None = None):
        configs = configs or {}
        self.lines = {p: d.decode("utf-8", "replace").splitlines() for p, d in files.items()}
        self.claims: list[Claim] = []
        self.mods: dict[str, Mod] = {}
        self.by_path: dict[str, str] = {}
        self.classes: dict[str, Cls] = {}
        self.funcs: dict[tuple[str, str], object] = {}
        self.func_decl: dict[tuple[str, str], tuple] = {}
        self.skipped: list[str] = []
        self.aliases: list[tuple[str, list[str]]] = []
        cfg = {}
        for name in ("tsconfig.json", "jsconfig.json"):
            if name in configs:
                cfg = strip_json(configs[name].decode("utf-8", "replace")).get("compilerOptions") or {}
                break
        base = cfg.get("baseUrl", ".") if isinstance(cfg.get("baseUrl", "."), str) else "."
        self.base_url = posixpath.normpath(base)
        for pat, targets in (cfg.get("paths") or {}).items() if isinstance(cfg.get("paths"), dict) else []:
            if isinstance(targets, list):
                self.aliases.append((pat, [t for t in targets if isinstance(t, str)]))
        taken: dict[str, str] = {}
        for p in sorted(files):
            stem = posixpath.splitext(p)[0]
            if posixpath.basename(stem) == "index" and posixpath.dirname(stem):
                stem = posixpath.dirname(stem)
            name = stem.replace("/", ".")
            if name in taken:
                name = p.replace("/", ".")
            taken[name] = p
            parser = TS if p.endswith((".ts", ".mts", ".cts")) else TSX
            tree = parser.parse(files[p])
            pkg = posixpath.dirname(p).replace("/", ".") or "root"
            self.mods[name] = Mod(name, p, pkg, tree.root_node, bool(TEST_FILE.search(p)))
            self.by_path[p] = name
        self.by_simple: dict[str, list[str]] = {}
        for m in self.mods.values():
            if not m.is_test:
                self.by_simple.setdefault(m.name.rsplit(".", 1)[-1], []).append(m.name)

    # ---- recording
    def add(self, subject, predicate, obj, conf, basis, mod, node, **attrs):
        ln = node if isinstance(node, int) else line(node)
        end = ln if isinstance(node, int) else node.end_point[0] + 1
        ls = self.lines[mod.path]
        ev = ls[ln - 1].strip()[:160] if 0 < ln <= len(ls) else ""
        self.claims.append(Claim(subject, predicate, obj, conf, basis, mod.path, ln, end, ev, attrs, EXTRACTOR))

    # ---- module resolution
    def resolve_spec(self, m: Mod, spec: str) -> str | None:
        bases = []
        if spec.startswith("."):
            bases.append(posixpath.normpath(posixpath.join(posixpath.dirname(m.path), spec)))
        else:
            for pat, targets in self.aliases:
                if "*" in pat:
                    head, _, tail = pat.partition("*")
                    if spec.startswith(head) and spec.endswith(tail) and len(spec) >= len(head) + len(tail):
                        mid = spec[len(head): len(spec) - len(tail)]
                        bases += [posixpath.normpath(posixpath.join(self.base_url, t.replace("*", mid))) for t in targets]
                elif spec == pat:
                    bases += [posixpath.normpath(posixpath.join(self.base_url, t)) for t in targets]
            if not bases and self.base_url not in (".", "") and not spec.startswith(("@", "node:")):
                bases.append(posixpath.normpath(posixpath.join(self.base_url, spec)))
        for base in bases:
            cands = [base]
            stripped = re.sub(r"\.(js|jsx|mjs|cjs)$", "", base)
            for stem in {base, stripped}:
                cands += [stem + e for e in EXTS] + [posixpath.join(stem, "index" + e) for e in EXTS]
            for c in cands:
                if c in self.by_path:
                    return self.by_path[c]
        return None

    # ---- collection
    def collect(self):
        for m in self.mods.values():
            for stmt in m.root.named_children:
                self.top(m, stmt, False, False, [])
        for m in self.mods.values():
            self.collect_imports(m)

    def define_class(self, m, node, kind, name, decorators):
        fqn = f"{m.name}.{name}"
        c = Cls(fqn, name, m, node, kind, decorators=decorators)
        body = node.child_by_field_name("body")
        pending = []
        nested = []
        for member in (body.children if body is not None else []):
            if member.type == "decorator":
                pending.append(member)
                continue
            mname = member.child_by_field_name("name")
            if member.type in ("method_definition", "method_signature", "abstract_method_signature") and mname is not None:
                has_body = member.child_by_field_name("body") is not None
                if has_body or text(mname) not in c.methods:  # overload signatures never replace the implementation
                    c.methods[text(mname)] = (member, pending)
            elif member.type == "public_field_definition" and mname is not None:
                value = member.child_by_field_name("value")
                if value is not None and value.type in ("arrow_function", "function_expression"):
                    c.methods[text(mname)] = (member, pending)
                elif value is not None and value.type in ("class", "class_expression"):  # static Proxy = class implements X { ... }
                    nested.append((value, f"{name}.{text(mname)}"))
            pending = []
        self.classes[fqn] = c
        m.names[name] = ("cls", fqn)
        for value, nested_name in nested:
            self.define_class(m, value, "class", nested_name, [])
        return c

    def define_function(self, m, node, name, fn, decorators=()):
        if fn is None:
            return
        if is_component_name(name) and has_jsx(fn.child_by_field_name("body") or fn):
            c = Cls(f"{m.name}.{name}", name, m, node, "component", fn=fn)
            self.classes[c.fqn] = c
            m.names[name] = ("cls", c.fqn)
        else:
            self.funcs[(m.name, name)] = fn
            m.names[name] = ("func", m.name, name)

    def top(self, m, stmt, exported, default, decorators):
        t = stmt.type
        if t == "export_statement":
            decos = [c for c in stmt.children if c.type == "decorator"]
            is_default = any(c.type == "default" for c in stmt.children)
            source = string_value(stmt.child_by_field_name("source"))
            decl = stmt.child_by_field_name("declaration")
            value = stmt.child_by_field_name("value")
            if decl is not None:
                self.top(m, decl, True, is_default, decos)
            elif value is not None and is_default:
                fn = function_of(value)
                if value.type == "identifier":
                    m.exports["default"] = ("local", text(value))
                elif fn is not None:
                    self.define_function(m, value, "default", fn)
                    m.exports["default"] = ("local", "default")
            for c in stmt.children:
                if c.type == "export_clause":
                    for spec in c.named_children:
                        if spec.type != "export_specifier":
                            continue
                        n = spec.child_by_field_name("name")
                        alias = spec.child_by_field_name("alias")
                        exported_as = text(alias or n)
                        m.exports[exported_as] = ("from", source, text(n)) if source else ("local", text(n))
                elif c.type == "namespace_export" and source:
                    m.exports[text(c.named_children[0])] = ("modspec", source)
            if source and not any(c.type in ("export_clause", "namespace_export") for c in stmt.children):
                m.stars.append(source)
            return
        if t in ("function_declaration", "generator_function_declaration"):
            name = text(stmt.child_by_field_name("name"))
            self.define_function(m, stmt, name, stmt)
            if exported:
                m.exports["default" if default else name] = ("local", name)
        elif t in ("class_declaration", "abstract_class_declaration"):
            name = text(stmt.child_by_field_name("name"))
            decos = decorators + [c for c in stmt.children if c.type == "decorator"]
            self.define_class(m, stmt, "class", name, decos)
            if exported:
                m.exports["default" if default else name] = ("local", name)
        elif t in ("interface_declaration", "enum_declaration"):
            name = text(stmt.child_by_field_name("name"))
            self.define_class(m, stmt, "interface" if t == "interface_declaration" else "enum", name, [])
            if exported:
                m.exports[name] = ("local", name)
        elif t in ("lexical_declaration", "variable_declaration"):
            for d in stmt.named_children:
                if d.type != "variable_declarator":
                    continue
                nm = d.child_by_field_name("name")
                value = d.child_by_field_name("value")
                if nm is None or nm.type != "identifier" or value is None:
                    continue
                name = text(nm)
                req = self.require_spec(value)
                chain = value
                while chain is not None and chain.type == "assignment_expression":  # var app = exports = module.exports = {}
                    if text(chain.child_by_field_name("left")) == "module.exports":
                        m.cjs_default = True
                        m.exports["default"] = ("local", name)
                    chain = chain.child_by_field_name("right")
                if req:
                    m.names[name] = ("require", req)
                elif chain is not None and (chain.type == "object" or (chain.type == "call_expression" and text(chain.child_by_field_name("function")) == "Object.create")):
                    m.objects.add(name)  # `var res = Object.create(proto)` is an object that later `res.x = function` lines extend
                elif value.type in ("class", "class_expression"):
                    self.define_class(m, value, "class", name, [])
                else:
                    self.define_function(m, d, name, function_of(value))
                if exported:
                    m.exports[name] = ("local", name)
            for d in stmt.named_children:  # const { a, b } = require('./x')
                nm, value = d.child_by_field_name("name"), d.child_by_field_name("value")
                req = self.require_spec(value) if value is not None else None
                if req and nm is not None and nm.type == "object_pattern":
                    for p in nm.named_children:
                        if p.type == "shorthand_property_identifier_pattern":
                            m.names[text(p)] = ("requirefrom", req, text(p))
                        elif p.type == "pair_pattern":
                            key, val = p.child_by_field_name("key"), p.child_by_field_name("value")
                            if val is not None and val.type == "identifier":
                                m.names[text(val)] = ("requirefrom", req, text(key))
        elif t == "import_statement":
            pass  # bound in collect_imports, once every module's exports are known
        elif t == "expression_statement" and stmt.named_children and stmt.named_children[0].type == "assignment_expression":
            self.commonjs(m, stmt.named_children[0])

    def require_spec(self, value):
        if value is not None and value.type == "call_expression" and text(value.child_by_field_name("function")) == "require":
            args = value.child_by_field_name("arguments")
            if args is not None and args.named_child_count:
                return string_value(args.named_children[0])
        return None

    def commonjs(self, m, asg):
        left, right = asg.child_by_field_name("left"), asg.child_by_field_name("right")
        lt = text(left)
        if right is None:
            return
        proto = re.fullmatch(r"(\w+)\.prototype\.(\w+)", lt)
        member = re.fullmatch(r"(\w+)\.(\w+)", lt)
        fnode = function_of(right)
        if fnode is not None and (proto or (member and member.group(1) not in ("module", "exports") and (member.group(1) in m.objects or member.group(1) in m.names))):
            owner, prop = (proto or member).group(1), (proto or member).group(2)
            c = self.object_class(m, owner, fnode if proto else None)
            if c is not None:
                c.methods[prop] = (fnode, [])
                return
        if lt == "module.exports":
            m.cjs_default = True
            if right.type == "identifier":
                m.exports["default"] = ("local", text(right))
            elif right.type == "object":
                for p in right.named_children:
                    if p.type == "shorthand_property_identifier":
                        m.exports[text(p)] = ("local", text(p))
                    elif p.type == "pair":
                        key, val = p.child_by_field_name("key"), p.child_by_field_name("value")
                        if val is not None and val.type == "identifier":
                            m.exports[text(key)] = ("local", text(val))
                        elif function_of(val) is not None:
                            self.define_function(m, p, text(key), function_of(val))
                            m.exports[text(key)] = ("local", text(key))
                    elif p.type == "method_definition":
                        nm = text(p.child_by_field_name("name"))
                        self.define_function(m, p, nm, p)
                        m.exports[nm] = ("local", nm)
            elif function_of(right) is not None:
                self.define_function(m, asg, "default", function_of(right))
                m.exports["default"] = ("local", "default")
            elif right.type == "call_expression":
                pass
        elif re.fullmatch(r"(module\.)?exports\.\w+", lt):
            name = lt.split(".")[-1]
            if right.type == "identifier":
                m.exports[name] = ("local", text(right))
            elif function_of(right) is not None:
                self.define_function(m, asg, name, function_of(right))
                m.exports[name] = ("local", name)

    def object_class(self, m, name, ctor):
        """A class-like type for `Foo.prototype.x = ...` (Foo a constructor function) or `obj.x = ...` (obj an object literal)."""
        e = m.names.get(name)
        if e and e[0] == "cls":
            return self.classes[e[1]]
        if e and e[0] == "func" and (name[:1].isupper() or ctor is not None):
            node = self.funcs.pop((m.name, name))
        elif name in m.objects:
            node = m.root
        else:
            return None
        fqn = f"{m.name}.{name}"
        c = Cls(fqn, name, m, node, "class" if node is not m.root else "object")
        self.classes[fqn] = c
        m.names[name] = ("cls", fqn)
        return c

    def collect_imports(self, m):
        for stmt in m.root.named_children:
            self.bind_import(m, stmt)
        for src in m.stars:
            tgt = self.resolve_spec(m, src)
            if tgt:
                m.imported.setdefault(tgt, 1)
        for k, v in list(m.exports.items()):
            if v[0] == "from":
                v2 = self.resolve_spec(m, v[1])
                m.exports[k] = ("from", v2, v[2]) if v2 else ("ext", v[1])
                if v2:
                    m.imported.setdefault(v2, 1)
            elif v[0] == "modspec":
                v2 = self.resolve_spec(m, v[1])
                m.exports[k] = ("mod", v2) if v2 else ("ext", v[1])
        m.stars = [t for t in (self.resolve_spec(m, s) for s in m.stars) if t]
        for k, v in list(m.names.items()):
            if v[0] in ("require", "requirefrom"):
                tgt = self.resolve_spec(m, v[1])
                if tgt:
                    m.imported.setdefault(tgt, 1)
                    m.names[k] = ("cjs", tgt) if v[0] == "require" else ("from", tgt, v[2])
                else:
                    m.names[k] = ("ext", v[1])

    def bind_import(self, m, stmt):
        if stmt.type != "import_statement":
            return
        spec = string_value(stmt.child_by_field_name("source"))
        if spec is None:
            return
        tgt = self.resolve_spec(m, spec)
        if tgt:
            m.imported.setdefault(tgt, line(stmt))
        clause = next((c for c in stmt.children if c.type == "import_clause"), None)
        if clause is None:
            return
        for c in clause.children:
            if c.type == "identifier":
                m.names[text(c)] = ("from", tgt, "default") if tgt else ("ext", spec)
            elif c.type == "namespace_import":
                m.names[text(c.named_children[0])] = ("mod", tgt) if tgt else ("ext", spec)
            elif c.type == "named_imports":
                for s in c.named_children:
                    if s.type == "import_specifier":
                        n, alias = s.child_by_field_name("name"), s.child_by_field_name("alias")
                        m.names[text(alias or n)] = ("from", tgt, text(n)) if tgt else ("ext", spec)

    # ---- symbol resolution
    def symbol(self, modname, exported, depth=0):
        mi = self.mods.get(modname)
        if mi is None or depth > 8:
            return None
        e = mi.exports.get(exported)
        if e:
            if e[0] == "local":
                return self.lookup_local(mi, e[1], depth + 1)
            if e[0] == "from":
                return self.symbol(e[1], e[2], depth + 1)
            if e[0] == "mod":
                return ("mod", e[1])
            return None
        for s in mi.stars:
            hit = self.symbol(s, exported, depth + 1)
            if hit:
                return hit
        if exported != "default":
            return self.lookup_local(mi, exported, depth + 1)
        return None

    def lookup_local(self, m, name, depth=0):
        e = m.names.get(name)
        if e is None:
            return None
        if e[0] == "from":
            return self.symbol(e[1], e[2], depth + 1) if e[1] else None
        if e[0] == "cjs":
            return self.cjs_value(e[1])
        return e if e[0] in ("cls", "func", "mod") else None

    def cjs_value(self, modname):
        return self.symbol(modname, "default") if self.mods[modname].cjs_default else ("mod", modname)

    # ---- expression resolution
    def entity(self, m, node, env, cur):
        if node is None:
            return None
        t = node.type
        if t in ("parenthesized_expression", "await_expression", "as_expression", "satisfies_expression", "non_null_expression", "type_assertion"):
            kids = [c for c in node.named_children if c.type not in ("type_annotation",)]
            return self.entity(m, kids[0], env, cur) if kids else None
        if t == "identifier":
            name = text(node)
            return env.get(name) or self.lookup_local(m, name)
        if t == "this":
            return ("inst", cur.fqn) if cur is not None else None
        if t == "new_expression":
            e = self.entity(m, node.child_by_field_name("constructor"), env, cur)
            return ("inst", e[1]) if e and e[0] == "cls" else None
        if t == "call_expression":
            fn = node.child_by_field_name("function")
            spec = self.require_spec(node)
            if spec:
                tgt = self.resolve_spec(m, spec)
                return self.cjs_value(tgt) if tgt else None
            if fn is not None and fn.type == "super":
                return None
            e = self.entity(m, fn, env, cur)
            if e and e[0] in ("func", "meth"):
                return self.returns(e)
            return None
        if t == "member_expression":
            obj, prop = node.child_by_field_name("object"), text(node.child_by_field_name("property"))
            if obj is not None and obj.type == "super" and cur is not None:
                for b in cur.bases:
                    owner = self.owner(b, prop)
                    if owner:
                        return ("meth", owner, prop)
                return None
            e = self.entity(m, obj, env, cur)
            if not e:
                return None
            if e[0] == "mod":
                return self.symbol(e[1], prop)
            if e[0] in ("cls", "inst"):
                c = self.classes.get(e[1])
                if c is None:
                    return None
                owner = self.owner(c.fqn, prop)
                if owner:
                    return ("meth", owner, prop)
                hit = self.attr_type(c.fqn, prop)
                return ("inst", hit[0]) if hit else None
        return None

    def annotation_class(self, m, ann):
        if ann is None:
            return None
        for n in self.walk(ann):
            if n.type in ("type_identifier", "identifier", "nested_type_identifier"):
                e = self.lookup_local(m, text(n).split(".")[0]) if n.type != "nested_type_identifier" else None
                if n.type == "nested_type_identifier":
                    parts = text(n).split(".")
                    e = self.lookup_local(m, parts[0])
                    for part in parts[1:]:
                        e = self.symbol(e[1], part) if e and e[0] == "mod" else None
                if e and e[0] == "cls":
                    return e[1]
        return None

    def walk(self, n):
        stack = [n]
        while stack:
            x = stack.pop()
            yield x
            stack.extend(reversed(x.children))

    def returns(self, e):
        if e[0] == "func":
            node, mod = self.funcs.get((e[1], e[2])), self.mods[e[1]]
        else:
            c = self.classes.get(e[1])
            node, mod = (c.methods.get(e[2], (None,))[0] if c else None), (c.mod if c else None)
        if node is None:
            return None
        ret = node.child_by_field_name("return_type")
        fqn = self.annotation_class(mod, ret) if ret is not None else None
        return ("inst", fqn) if fqn else None

    def owner(self, fqn, name):
        seen, queue = {fqn}, [fqn]
        while queue:
            cur = queue.pop(0)
            c = self.classes.get(cur)
            if c is not None and name in c.methods:
                return cur
            for b in (c.bases if c else []):
                if b not in seen:
                    seen.add(b)
                    queue.append(b)
        return None

    def attr_type(self, fqn, name):
        seen, queue = {fqn}, [fqn]
        while queue:
            cur = queue.pop(0)
            c = self.classes.get(cur)
            if c is not None and name in c.attrs:
                return c.attrs[name]
            for b in (c.bases if c else []):
                if b not in seen:
                    seen.add(b)
                    queue.append(b)
        return None

    def params_of(self, fn):
        p = fn.child_by_field_name("parameters")
        if p is not None:
            return [x for x in p.named_children if x.type in ("required_parameter", "optional_parameter")]
        single = fn.child_by_field_name("parameter")
        return [single] if single is not None else []

    def local_env(self, m, c, fn):
        env = {}
        for p in self.params_of(fn):
            pat = p.child_by_field_name("pattern") if p.type != "identifier" else p
            if pat is not None and pat.type == "identifier":
                t = self.annotation_class(m, p.child_by_field_name("type")) if p.type != "identifier" else None
                if t:
                    env[text(pat)] = ("inst", t)
        decls = sorted((x for x in self.walk(fn) if x.type == "variable_declarator"), key=lambda x: x.start_byte)
        for d in decls:
            nm = d.child_by_field_name("name")
            if nm is None or nm.type != "identifier" or text(nm) in env:
                continue
            t = self.annotation_class(m, d.child_by_field_name("type"))
            if t:
                env[text(nm)] = ("inst", t)
                continue
            e = self.entity(m, d.child_by_field_name("value"), env, c)
            if e and e[0] == "inst":
                env[text(nm)] = e
        return env

    def collect_attrs(self):
        for c in self.classes.values():
            c.bases = []
            heritage = []
            for k in c.node.children:
                if k.type == "class_heritage":
                    for h in k.children:
                        if h.type == "extends_clause":
                            heritage += [x for x in h.named_children if x.type in ("identifier", "member_expression", "type_identifier", "nested_type_identifier")][:1]
                elif k.type == "extends_type_clause":
                    heritage += [x for x in k.named_children if x.type in ("type_identifier", "nested_type_identifier", "identifier")]
            for h in heritage:
                e = self.entity(c.mod, h, {}, None) if h.type in ("identifier", "member_expression") else (("cls", self.annotation_class(c.mod, h)) if self.annotation_class(c.mod, h) else None)
                if e and e[0] == "cls" and e[1] != c.fqn:
                    c.bases.append(e[1])
        for c in self.classes.values():
            if c.kind not in ("class", "interface"):
                continue
            body = c.node.child_by_field_name("body")
            for member in (body.children if body is not None else []):
                if member.type in ("public_field_definition", "property_signature") and member.child_by_field_name("name") is not None:
                    name = text(member.child_by_field_name("name"))
                    t = self.annotation_class(c.mod, member.child_by_field_name("type"))
                    if t and t != c.fqn:
                        c.attrs.setdefault(name, (t, HIGH, line(member)))
                        continue
                    value = member.child_by_field_name("value")
                    e = self.entity(c.mod, value, {}, c) if value is not None else None
                    if e and e[0] == "inst" and e[1] != c.fqn:
                        c.attrs.setdefault(name, (e[1], MEDIUM, line(member)))
            ctor = c.methods.get("constructor")
            if ctor is not None:
                fn = ctor[0]
                env = self.local_env(c.mod, c, fn)
                for p in self.params_of(fn):  # constructor(private repo: Repo)
                    if any(k.type in ("accessibility_modifier", "readonly") for k in p.children):
                        pat = p.child_by_field_name("pattern")
                        t = self.annotation_class(c.mod, p.child_by_field_name("type"))
                        if pat is not None and t and t != c.fqn:
                            c.attrs.setdefault(text(pat), (t, HIGH, line(p)))
                for n in self.walk(fn):
                    if n.type == "assignment_expression":
                        left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
                        if left is not None and left.type == "member_expression" and text(left.child_by_field_name("object")) == "this":
                            e = self.entity(c.mod, right, env, c)
                            prop = text(left.child_by_field_name("property"))
                            if e and e[0] == "inst" and e[1] != c.fqn:
                                c.attrs.setdefault(prop, (e[1], MEDIUM, line(n)))

    # ---- emit
    def run(self) -> list[Claim]:
        self.collect()
        self.collect_attrs()
        for name in sorted(self.mods):
            self.emit_module(self.mods[name])
        return self.claims

    def tid(self, fqn):
        return f"type:{fqn}"

    def fn_params(self, fn):
        out = []
        for p in self.params_of(fn):
            pat = p.child_by_field_name("pattern") if p.type != "identifier" else p
            ann = p.child_by_field_name("type") if p.type != "identifier" else None
            out.append(text(pat).split("\n")[0][:40] + (text(ann) if ann is not None else ""))
        return out

    def method_claim(self, m, owner, name, node, decorators=(), basis="declaration"):
        fn = node
        if node.type == "public_field_definition":
            fn = function_of(node.child_by_field_name("value")) or node
        elif node.type == "variable_declarator":
            fn = function_of(node.child_by_field_name("value")) or node
        ret = fn.child_by_field_name("return_type")
        mods = [k.type if k.type in ("async", "static") else text(k) for k in node.children if k.type in ("async", "static", "accessibility_modifier")]
        if fn is not node and fn.type in ("arrow_function", "function_expression") and any(k.type == "async" for k in fn.children):
            mods.append("async")
        self.add(owner, "has_method", f"method:{owner.split(':', 1)[1]}#{name}", HIGH, basis, m, node, name=name, params=self.fn_params(fn) if fn.type != "method_signature" else self.fn_params(node),
                 returns=text(ret).lstrip(": ").strip() if ret is not None else "", annotations=[self.deco_name(d) for d in decorators], modifiers=sorted(set(mods)))

    def deco_name(self, d):
        e = d.named_children[0] if d.named_children else None
        if e is not None and e.type == "call_expression":
            e = e.child_by_field_name("function")
        return text(e).split(".")[-1]

    def emit_module(self, m: Mod):
        t = self.tid(m.name)
        self.add(t, "is_a", "module", HIGH, "declaration", m, 1, stereotypes=[], annotations=[], modifiers=[], role="test" if m.is_test else None, simple=m.name.rsplit(".", 1)[-1], package=m.pkg, end_line=len(self.lines[m.path]) or 1)
        self.add(f"package:{m.pkg}", "declares_type", t, HIGH, "declaration", m, 1)
        for dep, ln in sorted(m.imported.items()):
            if dep != m.name:
                syms = sorted({v[2] for v in m.names.values() if v[0] == "from" and v[1] == dep} | ({"*"} if any(v[0] == "mod" and v[1] == dep for v in m.names.values()) else set()))
                self.add(t, "depends_on", self.tid(dep), HIGH, "import statement", m, ln, via="import", symbols=syms)
        for (mod, fname), fn in sorted(self.funcs.items()):
            if mod != m.name:
                continue
            node = fn
            parent = fn.parent
            if parent is not None and parent.type == "variable_declarator":
                node = parent
            self.method_claim(m, t, fname, node)
            self.emit_body(m, None, fn, f"method:{m.name}#{fname}", t)
        for c in sorted((c for c in self.classes.values() if c.mod is m), key=lambda c: c.fqn):
            self.emit_class(m, c)
        self.module_level(m, t)
        if m.is_test:
            self.emit_tests(m, t)
        self.next_routes(m)

    def emit_class(self, m, c: Cls):
        t = self.tid(c.fqn)
        decos = [self.deco_name(d) for d in c.decorators]
        stereo = sorted({STEREOTYPES[d] for d in decos if d in STEREOTYPES})
        if c.kind == "component":
            stereo = sorted(set(stereo) | {"react-component"})
        elif any(self.is_react_base(b) for b in self.heritage_names(c)):
            stereo = sorted(set(stereo) | {"react-component"})
        self.add(t, "is_a", c.kind, HIGH, "declaration", m, c.node, stereotypes=stereo, annotations=decos, modifiers=["abstract"] if c.node.type == "abstract_class_declaration" else [],
                 role="test" if m.is_test else None, simple=c.simple, package=m.pkg, end_line=c.node.end_point[0] + 1)
        self.add(f"package:{m.pkg}", "declares_type", t, HIGH, "declaration", m, c.node)
        for h in self.heritage(c):
            e = self.entity(m, h, {}, None) if h.type in ("identifier", "member_expression") else None
            if e is None and h.type in ("type_identifier", "nested_type_identifier"):
                f = self.annotation_class(m, h)
                e = ("cls", f) if f else None
            label = text(h).split("<")[0]
            if e and e[0] == "cls" and e[1] != c.fqn:
                self.add(t, "implements" if h.parent.type == "implements_clause" and c.kind == "class" else "extends", self.tid(e[1]), HIGH, "declaration", m, h)
            else:
                root = label.split(".")[0]
                src = m.names.get(root)
                if src and src[0] == "ext":
                    label = f"{src[1]}:{label}"
                self.add(t, "implements" if h.parent.type == "implements_clause" else "extends", f"external:{label}", HIGH, "declaration (type not in this repository)", m, h, external=True)
        for name, (target, conf, ln) in sorted(c.attrs.items()):
            self.add(t, "depends_on", self.tid(target), conf, "annotated property type" if conf == HIGH else "property assigned from a constructor call", m, ln, via="property")
        if c.kind == "component" and c.fn is not None:
            self.add(t, "has_method", f"method:{c.fqn}#render", HIGH, "the component function is its render method", m, c.fn, name="render", params=self.fn_params(c.fn), returns="", annotations=["function-component"], modifiers=[])
            self.emit_body(m, None, c.fn, f"method:{c.fqn}#render", t)
            return
        for mname, (node, pending) in sorted(c.methods.items()):
            self.method_claim(m, t, mname, node, pending)
            mid = f"method:{c.fqn}#{mname}"
            fn = function_of(node.child_by_field_name("value")) if node.type == "public_field_definition" else node
            if fn is not None and fn.child_by_field_name("body") is not None:
                self.emit_body(m, c, fn, mid, t)
            for d in pending:
                self.nest_route(m, c, mid, mname, d)

    def heritage(self, c):
        out = []
        for k in c.node.children:
            if k.type == "class_heritage":
                for h in k.children:
                    if h.type == "extends_clause":
                        out += [x for x in h.named_children if x.type in ("identifier", "member_expression", "type_identifier", "nested_type_identifier")][:1]
                    elif h.type == "implements_clause":
                        out += [x for x in h.named_children if x.type in ("type_identifier", "nested_type_identifier", "identifier")]
            elif k.type == "extends_type_clause":
                out += [x for x in k.named_children if x.type in ("type_identifier", "nested_type_identifier", "identifier")]
        return out

    def heritage_names(self, c):
        return [text(h).split("<")[0] for h in self.heritage(c) if h.parent.type == "extends_clause"]

    def is_react_base(self, name):
        return name in ("React.Component", "React.PureComponent", "Component", "PureComponent")

    def nest_route(self, m, c, mid, mname, d):
        name = self.deco_name(d)
        if name not in NEST_HTTP:
            return
        prefix = ""
        for cd in c.decorators:
            if self.deco_name(cd) == "Controller":
                args = cd.named_children[0].child_by_field_name("arguments") if cd.named_children and cd.named_children[0].type == "call_expression" else None
                prefix = (string_value(args.named_children[0]) if args is not None and args.named_child_count else "") or ""
        args = d.named_children[0].child_by_field_name("arguments") if d.named_children and d.named_children[0].type == "call_expression" else None
        sub = (string_value(args.named_children[0]) if args is not None and args.named_child_count else "") or ""
        path = "/" + "/".join(x for x in (prefix.strip("/"), sub.strip("/")) if x)
        self.add(f"route:{NEST_HTTP[name]} {path}", "handled_by", mid, HIGH, f"@{name} on a method of a @Controller class", m, d, http=NEST_HTTP[name], path=path)

    def emit_body(self, m, c, fn, mid, subject_type):
        env = self.local_env(m, c, fn)
        seen = set()
        body = fn.child_by_field_name("body") or fn
        for n in sorted((x for x in self.walk(body) if x.type in ("call_expression", "new_expression", "jsx_self_closing_element", "jsx_opening_element")), key=lambda x: x.start_byte):
            self.use(m, c, n, env, mid, subject_type, seen)

    def use(self, m, c, n, env, mid, subject_type, seen):
        if n.type.startswith("jsx"):
            name = n.child_by_field_name("name")
            if name is None or name.type != "identifier" or not is_component_name(text(name)):
                return
            e = self.lookup_local(m, text(name))
            if e and e[0] == "cls" and self.classes[e[1]].kind in ("component", "class"):
                target = self.tid(e[1])
                if ("r", target) not in seen and target != subject_type:
                    seen.add(("r", target))
                    self.add(subject_type, "renders", target, MEDIUM, "JSX element resolved through an import or local definition", m, n)
            return
        if n.type == "new_expression":
            e = self.entity(m, n.child_by_field_name("constructor"), env, c)
            if e and e[0] == "cls" and ("i", e[1]) not in seen:
                seen.add(("i", e[1]))
                self.add(mid, "instantiates", self.tid(e[1]), MEDIUM, "new expression resolved through an import or local definition", m, n)
            return
        fn = n.child_by_field_name("function")
        if fn is None or fn.type == "super":
            return
        e = self.entity(m, fn, env, c)
        if not e:
            return
        if e[0] == "func":
            key, target, basis = ("c", e[1], e[2]), f"method:{e[1]}#{e[2]}", "call of a function resolved through imports or local definitions"
        elif e[0] == "meth":
            key, target, basis = ("c", e[1], e[2]), f"method:{e[1]}#{e[2]}", "method called on a receiver whose class is known from a type annotation, this or a constructor call"
        else:
            return
        if target == mid or key in seen:
            return
        seen.add(key)
        self.add(mid, "calls", target, MEDIUM, basis, m, n)

    # ---- module-level routes (Express, Fastify, Koa routers, React Router)
    def module_level(self, m, t):
        env = {}
        for n in sorted((x for x in self.walk(m.root) if x.type in ("call_expression", "jsx_self_closing_element", "jsx_opening_element")), key=lambda x: x.start_byte):
            if n.type == "call_expression":
                self.http_route(m, n, env)
            else:
                self.router_route(m, n)
        # statements outside any function (entry points, scripts) call things too; they are attributed to `<module>`
        mid, seen, before = f"method:{m.name}#<module>", set(), len(self.claims)
        for n in sorted((x for x in self.walk_outside_functions(m.root) if x.type in ("call_expression", "new_expression")), key=lambda x: x.start_byte):
            self.use(m, None, n, env, mid, t, seen)
        if len(self.claims) > before:
            self.add(t, "has_method", mid, HIGH, "statements at the top level of the module", m, 1, name="<module>", params=[], returns="", annotations=[], modifiers=[])

    def walk_outside_functions(self, root):
        stack = [root]
        while stack:
            x = stack.pop()
            yield x
            for c in x.children:
                if c.type in ("function_declaration", "function_expression", "arrow_function", "method_definition", "class_declaration", "class_body", "generator_function_declaration"):
                    continue
                stack.append(c)

    def http_route(self, m, n, env):
        fn = n.child_by_field_name("function")
        if fn is None or fn.type != "member_expression":
            return
        verb = text(fn.child_by_field_name("property"))
        if verb not in HTTP:
            return
        args = n.child_by_field_name("arguments")
        if args is None or not args.named_children:
            return
        path = string_value(args.named_children[0])
        if path is None or not path.startswith("/") or len(args.named_children) < 2:
            return
        http = "ANY" if verb == "all" else verb.upper()
        recv = text(fn.child_by_field_name("object"))
        direct = recv in ("app", "server", "fastify")
        handler = args.named_children[-1]
        if handler.type == "identifier" or handler.type == "member_expression":
            e = self.entity(m, handler, env, None)
            if not e or e[0] not in ("func", "meth"):
                return
            mid = f"method:{e[1]}#{e[2]}"
        else:
            fnode = function_of(handler)
            if fnode is None:
                return
            name = f"{http} {path}"
            mid = f"method:{m.name}#{name}"
            self.method_claim(m, self.tid(m.name), name, fnode, basis="inline route handler")
            self.emit_body(m, None, fnode, mid, self.tid(m.name))
        self.add(f"route:{http} {path}", "handled_by", mid, HIGH if direct else MEDIUM,
                 "route registered on the application object" if direct else "route registered on a router; the path is relative to where the router is mounted", m, n, http=http, path=path)

    def router_route(self, m, n):
        name = n.child_by_field_name("name")
        if name is None or text(name) != "Route":
            return
        attrs = {}
        for a in n.children_by_field_name("attribute"):
            if a.type != "jsx_attribute" or not a.named_children:
                continue
            key = text(a.named_children[0])
            val = a.named_children[1] if len(a.named_children) > 1 else None
            attrs[key] = val
        path = string_value(attrs.get("path")) if attrs.get("path") is not None and attrs["path"].type == "string" else None
        if path is None:
            return
        target = None
        for key in ("element", "Component", "component"):
            val = attrs.get(key)
            if val is None or val.type != "jsx_expression" or not val.named_children:
                continue
            inner = val.named_children[0]
            if inner.type in ("jsx_self_closing_element", "jsx_element"):
                tag = inner if inner.type == "jsx_self_closing_element" else inner.child_by_field_name("open_tag")
                nm = tag.child_by_field_name("name") if tag is not None else None
                e = self.lookup_local(m, text(nm)) if nm is not None and nm.type == "identifier" else None
            elif inner.type == "identifier":
                e = self.lookup_local(m, text(inner))
            else:
                e = None
            if e and e[0] == "cls":
                target = e[1]
                break
        if target:
            c = self.classes[target]
            mid = f"method:{target}#render" if c.kind == "component" else None
            if mid:
                self.add(f"route:GET {path}", "handled_by", mid, MEDIUM, "React Router <Route path> with an element or component prop; nested routes are relative to their parent", m, n, http="GET", path=path)

    def next_routes(self, m):
        p = m.path
        mt = re.match(r"^(?:src/)?app/(?:(.*)/)?(page|route)\.[cm]?[jt]sx?$", p)
        if mt:
            segs = [s for s in (mt.group(1) or "").split("/") if s and not (s.startswith("(") and s.endswith(")")) and not s.startswith("@")]
            path = "/" + "/".join(dyn(s) for s in segs)
            if mt.group(2) == "page":
                tgt = m.exports.get("default")
                comp = self.lookup_local(m, tgt[1]) if tgt and tgt[0] == "local" else None
                if comp and comp[0] == "cls":
                    self.add(f"route:GET {path}", "handled_by", f"method:{comp[1]}#render", MEDIUM, "Next.js app router file convention (page file)", m, 1, http="GET", path=path)
            else:
                for verb in NEXT_VERBS:
                    e = m.exports.get(verb)
                    if e and e[0] == "local" and (m.name, e[1]) in self.funcs:
                        self.add(f"route:{verb} {path}", "handled_by", f"method:{m.name}#{e[1]}", MEDIUM, "Next.js app router file convention (route file exporting an HTTP verb)", m, 1, http=verb, path=path)
            return
        mt = re.match(r"^(?:src/)?pages/(.+)\.[cm]?[jt]sx?$", p)
        if mt and not m.is_test:
            parts = mt.group(1).split("/")
            if parts[-1] == "index":
                parts = parts[:-1]
            if parts and parts[-1].startswith("_"):  # _app, _document and friends are not routes
                return
            path = "/" + "/".join(dyn(s) for s in parts)
            tgt = m.exports.get("default")
            ent = self.lookup_local(m, tgt[1]) if tgt and tgt[0] == "local" else None
            if ent and ent[0] == "func":
                self.add(f"route:ANY {path}", "handled_by", f"method:{ent[1]}#{ent[2]}", MEDIUM, "Next.js pages router file convention (API route)", m, 1, http="ANY", path=path)
            elif ent and ent[0] == "cls" and self.classes[ent[1]].kind == "component":
                self.add(f"route:GET {path}", "handled_by", f"method:{ent[1]}#render", MEDIUM, "Next.js pages router file convention (page)", m, 1, http="GET", path=path)

    # ---- tests
    def emit_tests(self, m, t):
        n = 0
        for x in self.walk(m.root):
            if x.type == "call_expression":
                fn = x.child_by_field_name("function")
                if fn is not None and fn.type == "identifier" and text(fn) in ("it", "test"):
                    args = x.child_by_field_name("arguments")
                    if args is not None and args.named_children and string_value(args.named_children[0]) is not None:
                        n += 1
        if n:
            self.add(t, "has_test_methods", str(n), HIGH, "count of it(...) and test(...) calls", m, 1)
        stem = re.sub(r"\.(test|spec)$", "", m.name)
        simple = stem.rsplit(".", 1)[-1]
        cand = stem if stem in self.mods and not self.mods[stem].is_test else None
        if cand is None:
            hits = self.by_simple.get(simple, [])
            cand = hits[0] if len(hits) == 1 else None
        if cand:
            self.add(t, "tests", self.tid(cand), MEDIUM, "test file name matches module name", m, 1)
