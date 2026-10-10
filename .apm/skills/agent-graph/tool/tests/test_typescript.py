import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
from kgen import generate as gen  # noqa: E402

FILES = {
    "package.json": json.dumps({"name": "demo", "version": "1.2.0", "scripts": {"build": "tsc -p .", "test": "vitest"}, "dependencies": {"react": "^18.0.0"}, "devDependencies": {"vitest": "^1.0.0"}}, indent=2),
    "tsconfig.json": '{\n  // comments are allowed\n  "compilerOptions": { "baseUrl": ".", "paths": { "@/*": ["src/*"] }, },\n}\n',
    "src/models/user.ts": "export interface User { id: string }\nexport class Base { ident() { return 1; } }\nexport class Admin extends Base {}\n",
    "src/models/index.ts": "export * from './user';\n",
    "src/services/repo.ts": "import type { User } from '@/models';\n\nexport interface UserRepo {\n  get(id: string): User;\n}\n\nexport class MemRepo implements UserRepo {\n  get(id: string): User { return { id }; }\n}\n",
    "src/services/service.ts": (
        "import { UserRepo, MemRepo } from './repo';\nimport { Admin } from '@/models';\n\n"
        "export class Service {\n  private audit = new MemRepo();\n  constructor(private readonly repo: UserRepo) {}\n"
        "  load(id: string) {\n    const u = this.repo.get(id);\n    this.log(u);\n    return new Admin().ident();\n  }\n"
        "  log(x: unknown) { return this.audit.get('a'); }\n}\n\nexport function helper(id: string) {\n  return new Service(new MemRepo()).load(id);\n}\n"
    ),
    "src/hooks/useThing.ts": "import { helper } from '../services/service';\nexport function useThing() { return helper('x'); }\n",
    "src/ui/Button.tsx": "export const Button = ({ label }: { label: string }) => <button>{label}</button>;\n",
    "src/ui/index.ts": "export { Button } from './Button';\n",
    "src/App.tsx": (
        "import React, { memo } from 'react';\nimport { Button } from '@/ui';\nimport { useThing } from './hooks/useThing';\nimport { Route } from 'react-router-dom';\n\n"
        "const Home = memo(function Home() {\n  const t = useThing();\n  return <Button label={String(t)} />;\n});\n\n"
        "export default function App() {\n  useThing();\n  return <div><Home /><Route path=\"/home\" element={<Home />} /></div>;\n}\n"
    ),
    "src/server.js": (
        "const express = require('express');\nconst { helper } = require('./services/service');\nconst app = express();\nconst router = express.Router();\n\n"
        "function list(req, res) { res.json(helper(1)); }\napp.get('/things', list);\napp.post('/things', (req, res) => { helper(2); res.end(); });\nrouter.delete('/things/:id', list);\n"
    ),
    "src/users.controller.ts": "import { Service } from './services/service';\n\n@Controller('users')\nexport class UsersController {\n  constructor(private service: Service) {}\n\n  @Get(':id')\n  find(id: string) { return this.service.load(id); }\n}\n",
    "app/api/items/[id]/route.ts": "export async function GET() { return Response.json({}); }\n",
    "app/blog/page.tsx": "export default function Blog() { return <main />; }\n",
    "src/ui/Button.test.tsx": "import { Button } from './Button';\n\ntest('renders', () => { Button; });\nit('x', () => {});\n",
    "lib/app.js": "var app = exports = module.exports = {};\napp.init = function init() { this.boot(); };\napp.boot = function () { return 1; };\n",
    "lib/index.js": "const app = require('./app');\nfunction start() { app.init(); }\nfunction Old() {}\nOld.prototype.run = function () { return this.stop(); };\nOld.prototype.stop = function () {};\nmodule.exports = { start, Old };\n",
    "dist/bundle.js": "function x(){}\n",
    "types.d.ts": "declare const y: number;\n",
}


def build(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    for rel, body in FILES.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-f", "."], check=True)
    gen.generate(repo)
    return repo


def claims(repo: Path) -> list[dict]:
    return [json.loads(line) for line in (repo / gen.OUT / "claims.jsonl").read_text(encoding="utf-8").splitlines()]


def edges(repo: Path, predicate: str) -> set[tuple[str, str, str]]:
    return {(c["subject"], c["object"], c["confidence"]) for c in claims(repo) if c["predicate"] == predicate}


def test_modules_classes_components_and_inheritance(tmp_path: Path):
    repo = build(tmp_path)
    c = claims(repo)
    kinds = {x["subject"]: x["object"] for x in c if x["predicate"] == "is_a"}
    assert kinds["type:src.services.service"] == "module" and kinds["type:src.models.user.Admin"] == "class" and kinds["type:src.models.user.User"] == "interface"
    assert kinds["type:src.App.Home"] == "component" and kinds["type:src.App.App"] == "component"
    assert ("type:src.models.user.Admin", "type:src.models.user.Base", "high") in edges(repo, "extends")
    assert ("type:src.services.repo.MemRepo", "type:src.services.repo.UserRepo", "high") in edges(repo, "implements")
    assert not any(x["subject"].startswith("type:types") or "dist" in x["source"]["file"] for x in c)
    stereo = next(x for x in c if x["predicate"] == "is_a" and x["subject"] == "type:src.ui.Button.Button")["attrs"]["stereotypes"]
    assert stereo == ["react-component"]


def test_calls_resolve_through_types_this_barrels_and_aliases(tmp_path: Path):
    repo = build(tmp_path)
    calls = edges(repo, "calls")
    has = lambda a, b: (a, b, "medium") in calls  # noqa: E731
    assert has("method:src.services.service.Service#load", "method:src.services.repo.UserRepo#get")   # constructor parameter property typed by an interface
    assert has("method:src.services.service.Service#load", "method:src.services.service.Service#log")  # this call
    assert has("method:src.services.service.Service#load", "method:src.models.user.Base#ident")       # inherited method, reached through the @/models barrel
    assert has("method:src.services.service.Service#log", "method:src.services.repo.MemRepo#get")    # field initialised with new
    assert has("method:src.services.service#helper", "method:src.services.service.Service#load")      # chained call on a constructor result
    assert has("method:src.hooks.useThing#useThing", "method:src.services.service#helper")           # relative import
    assert ("type:src.services.service.Service", "type:src.services.repo.UserRepo", "high") in edges(repo, "depends_on")
    assert ("type:src.services.service", "type:src.models", "high") in edges(repo, "depends_on")  # the @/models alias lands on the barrel file


def test_jsx_renders_and_hooks(tmp_path: Path):
    repo = build(tmp_path)
    renders = edges(repo, "renders")
    assert ("type:src.App.Home", "type:src.ui.Button.Button", "medium") in renders
    assert any(s.startswith("type:src.App.") and o == "type:src.App.Home" for s, o, _ in renders)
    assert ("method:src.App.Home#render", "method:src.hooks.useThing#useThing", "medium") in edges(repo, "calls")


def test_routes_from_express_nest_next_and_react_router(tmp_path: Path):
    repo = build(tmp_path)
    routes = {(x["attrs"]["http"], x["attrs"]["path"], x["confidence"], x["object"]) for x in claims(repo) if x["predicate"] == "handled_by"}
    assert ("GET", "/things", "high", "method:src.server#list") in routes
    assert ("POST", "/things", "high", "method:src.server#POST /things") in routes
    assert ("DELETE", "/things/:id", "medium", "method:src.server#list") in routes
    assert ("GET", "/users/:id", "high", "method:src.users.controller.UsersController#find") in routes
    assert ("GET", "/api/items/{id}", "medium", "method:app.api.items.[id].route#GET") in routes
    assert ("GET", "/blog", "medium", "method:app.blog.page.Blog#render") in routes
    assert ("GET", "/home", "medium", "method:src.App.Home#render") in routes
    assert ("method:src.server#POST /things", "method:src.services.service#helper", "medium") in edges(repo, "calls")


def test_tests_build_page_and_idempotence(tmp_path: Path):
    repo = build(tmp_path)
    assert ("type:src.ui.Button.test", "type:src.ui.Button", "medium") in edges(repo, "tests")
    assert any(x["predicate"] == "has_test_methods" and x["object"] == "2" for x in claims(repo))
    build_page = (repo / gen.OUT / "okf/build.md").read_text(encoding="utf-8")
    assert "demo@1.2.0" in build_page and "script build: tsc -p ." in build_page and "react@^18.0.0" in build_page
    again = gen.generate(repo)
    assert again["written"] == [] and again["removed"] == []


def test_commonjs_objects_and_prototype_methods(tmp_path: Path):
    repo = build(tmp_path)
    calls = edges(repo, "calls")
    assert ("method:lib.app.app#init", "method:lib.app.app#boot", "medium") in calls         # this.boot() inside a method added with app.init = function
    assert ("method:lib#start", "method:lib.app.app#init", "medium") in calls          # require() of a module whose module.exports is the object
    assert ("method:lib.Old#run", "method:lib.Old#stop", "medium") in calls      # Foo.prototype.x = function
    kinds = {x["subject"]: x["object"] for x in claims(repo) if x["predicate"] == "is_a"}
    assert kinds["type:lib.app.app"] == "object" and kinds["type:lib.Old"] == "class"


def test_import_edges_name_the_imported_symbols(tmp_path: Path):
    repo = build(tmp_path)
    imp = next(x for x in claims(repo) if x["predicate"] == "depends_on" and x["subject"] == "type:src.services.service" and x["object"] == "type:src.services.repo")
    assert imp["attrs"]["symbols"] == ["MemRepo", "UserRepo"]


def test_class_expression_in_a_static_field_is_collected(tmp_path: Path):
    files = {"src/u.ts": "export const strip = (p: string) => p;\n",
             "src/b.ts": "import { strip } from './u';\nexport class B {\n  static readonly Proxy = class {\n    run(x: string) { return strip(x); }\n  };\n}\n"}
    repo = tmp_path / "r"
    for rel, body in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    gen.generate(repo)
    assert ("method:src.b.B.Proxy#run", "method:src.u#strip", "medium") in edges(repo, "calls")
