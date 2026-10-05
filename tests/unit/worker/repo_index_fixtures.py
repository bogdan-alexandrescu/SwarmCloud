"""Small fixture repositories for the repository-index extractor's tests.

They are written out by the tests rather than checked in as directories,
because a checked-in `tests/test_users.py` that imports a module only the
fixture has would be collected by pytest here and fail. Each fixture is a
dict of path -> text; `build_repo` writes it and, when asked, commits a
history with FIXED author and committer dates under an isolated git
configuration, so two builds of one fixture have the same commit shas and
the extractor's determinism can be held byte for byte.

Line numbers matter: the tests assert symbols' line ranges against these
texts, so an edit here moves an assertion there.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Iterable, Mapping

PYTHON_APP: dict[str, str] = {
    # 1-based line numbers are written beside the definitions the tests assert.
    "src/pkg/__init__.py": "",
    "src/pkg/users.py": (
        "from fastapi import APIRouter\n"  # 1
        "\n"  # 2
        "from .store import fetch, save\n"  # 3
        "\n"  # 4
        "router = APIRouter()\n"  # 5
        "\n"  # 6
        "\n"  # 7
        "def load_user(user_id):\n"  # 8
        "    row = fetch(user_id)\n"  # 9
        "    return normalise(row)\n"  # 10
        "\n"  # 11
        "\n"  # 12
        "def normalise(row):\n"  # 13
        "    return dict(row)\n"  # 14
        "\n"  # 15
        "\n"  # 16
        "@router.get(\"/users/{user_id}\")\n"  # 17
        "async def get_user(user_id):\n"  # 18
        "    return load_user(user_id)\n"  # 19
        "\n"  # 20
        "\n"  # 21
        "class UserService(BaseService):\n"  # 22
        "    def create(self, data):\n"  # 23
        "        save(data)\n"  # 24
        "        return self._audit(data)\n"  # 25
        "\n"  # 26
        "    def _audit(self, data):\n"  # 27
        "        return data\n"  # 28
        "\n"  # 29
        "\n"  # 30
        "class BaseService:\n"  # 31
        "    pass\n"  # 32
    ),
    "src/pkg/store.py": (
        "def fetch(key):\n"  # 1
        "    return {\"key\": key}\n"  # 2
        "\n"  # 3
        "\n"  # 4
        "def save(row):\n"  # 5
        "    return row\n"  # 6
    ),
    "src/pkg/web.py": (
        "from flask import Flask\n"  # 1
        "\n"  # 2
        "app = Flask(__name__)\n"  # 3
        "\n"  # 4
        "\n"  # 5
        "@app.route(\"/health\", methods=[\"GET\", \"HEAD\"])\n"  # 6
        "def health():\n"  # 7
        "    return \"ok\"\n"  # 8
        "\n"  # 9
        "\n"  # 10
        "@app.route(\"/ping\")\n"  # 11
        "def ping():\n"  # 12
        "    return \"pong\"\n"  # 13
    ),
    "tests/test_users.py": (
        "from pkg.users import load_user\n"  # 1
        "\n"  # 2
        "\n"  # 3
        "def test_load_user():\n"  # 4
        "    assert load_user(1)\n"  # 5
    ),
    # Named for store.py but imports nothing: the edge is naming alone.
    "tests/test_store.py": (
        "def test_store_shape():\n"  # 1
        "    assert True\n"  # 2
    ),
}

# Two modules define `render`; a caller importing both cannot be resolved
# to one, so each candidate gets an `ast` edge at the ambiguous confidence.
PYTHON_AMBIGUOUS: dict[str, str] = {
    "lib/a.py": "def render(x):\n    return x\n",
    "lib/b.py": "def render(x):\n    return x\n",
    "lib/c.py": "def only_here():\n    return 1\n",
    "lib/use.py": (
        "from lib.a import *\n"  # 1
        "from lib.b import *\n"  # 2
        "from lib.c import only_here\n"  # 3
        "\n"  # 4
        "\n"  # 5
        "def main():\n"  # 6
        "    render(1)\n"  # 7
        "    only_here()\n"  # 8
    ),
}

TS_APP: dict[str, str] = {
    "web/src/server.js": (
        "const express = require(\"express\");\n"  # 1
        "const { listUsers } = require(\"./users\");\n"  # 2
        "\n"  # 3
        "const app = express();\n"  # 4
        "\n"  # 5
        "app.get(\"/api/users\", listUsers);\n"  # 6
        "app.post(\"/api/users\", (req, res) => {\n"  # 7
        "  res.send(listUsers());\n"  # 8
        "});\n"  # 9
        "\n"  # 10
        "module.exports = app;\n"  # 11
    ),
    "web/src/users.js": (
        "function listUsers() {\n"  # 1
        "  return [];\n"  # 2
        "}\n"  # 3
        "\n"  # 4
        "module.exports = { listUsers };\n"  # 5
    ),
    "web/src/format.ts": (
        "export function formatName(first: string, last: string): string {\n"  # 1
        "  return `${first} ${last}`;\n"  # 2
        "}\n"  # 3
        "\n"  # 4
        "export const shout = (s: string): string => s.toUpperCase();\n"  # 5
        "\n"  # 6
        "function internalOnly(): number {\n"  # 7
        "  return 1;\n"  # 8
        "}\n"  # 9
        "\n"  # 10
        "export class Formatter extends Base {\n"  # 11
        "  run(): string {\n"  # 12
        "    return formatName(\"a\", \"b\");\n"  # 13
        "  }\n"  # 14
        "}\n"  # 15
        "\n"  # 16
        "class Base {}\n"  # 17
    ),
    "web/src/App.tsx": (
        "import { formatName } from \"./format\";\n"  # 1
        "\n"  # 2
        "export default function App(): JSX.Element {\n"  # 3
        "  return <div>{formatName(\"a\", \"b\")}</div>;\n"  # 4
        "}\n"  # 5
    ),
    "web/src/format.test.ts": (
        "import { formatName } from \"./format\";\n"  # 1
        "\n"  # 2
        "describe(\"formatName\", () => {\n"  # 3
        "  it(\"joins\", () => {\n"  # 4
        "    expect(formatName(\"a\", \"b\")).toBe(\"a b\");\n"  # 5
        "  });\n"  # 6
        "});\n"  # 7
    ),
}

GO_APP: dict[str, str] = {
    "go.mod": "module example.com/shop\n\ngo 1.22\n",
    "cmd/server/main.go": (
        "package main\n"  # 1
        "\n"  # 2
        "import (\n"  # 3
        "\t\"net/http\"\n"  # 4
        "\n"  # 5
        "\tcart \"example.com/shop/internal/cart\"\n"  # 6
        ")\n"  # 7
        "\n"  # 8
        "func main() {\n"  # 9
        "\thttp.HandleFunc(\"GET /cart\", cart.Show)\n"  # 10
        "\tr := newRouter()\n"  # 11
        "\tr.POST(\"/cart/items\", cart.Add)\n"  # 12
        "\thttp.ListenAndServe(\":8080\", nil)\n"  # 13
        "}\n"  # 14
    ),
    "cmd/server/router.go": (
        "package main\n"  # 1
        "\n"  # 2
        "func newRouter() *router {\n"  # 3
        "\treturn &router{}\n"  # 4
        "}\n"  # 5
    ),
    "internal/cart/cart.go": (
        "package cart\n"  # 1
        "\n"  # 2
        "import \"net/http\"\n"  # 3
        "\n"  # 4
        "type Cart struct {\n"  # 5
        "\tItems []string\n"  # 6
        "}\n"  # 7
        "\n"  # 8
        "func (c *Cart) Total() int {\n"  # 9
        "\treturn len(c.Items)\n"  # 10
        "}\n"  # 11
        "\n"  # 12
        "func Show(w http.ResponseWriter, r *http.Request) {\n"  # 13
        "\tcount(nil)\n"  # 14
        "}\n"  # 15
        "\n"  # 16
        "func Add(w http.ResponseWriter, r *http.Request) {}\n"  # 17
        "\n"  # 18
        "func count(c *Cart) int {\n"  # 19
        "\treturn 0\n"  # 20
        "}\n"  # 21
    ),
    "internal/cart/cart_test.go": (
        "package cart\n"  # 1
        "\n"  # 2
        "import \"testing\"\n"  # 3
        "\n"  # 4
        "func TestCount(t *testing.T) {\n"  # 5
        "\tcount(nil)\n"  # 6
        "}\n"  # 7
    ),
}

HCL_APP: dict[str, str] = {
    "infra/main.tf": (
        "variable \"region\" {\n"  # 1
        "  type = string\n"  # 2
        "}\n"  # 3
        "\n"  # 4
        "resource \"google_storage_bucket\" \"artifacts\" {\n"  # 5
        "  name     = \"artifacts\"\n"  # 6
        "  location = var.region\n"  # 7
        "  labels = {\n"  # 8
        "    managed-by = \"swarm-terraform\"\n"  # 9
        "  }\n"  # 10
        "}\n"  # 11
        "\n"  # 12
        "module \"network\" {\n"  # 13
        "  source = \"./modules/network\"\n"  # 14
        "  bucket = google_storage_bucket.artifacts.name\n"  # 15
        "}\n"  # 16
        "\n"  # 17
        "output \"bucket\" {\n"  # 18
        "  value = module.network.id\n"  # 19
        "}\n"  # 20
    ),
    "infra/modules/network/main.tf": (
        "variable \"bucket\" {\n"  # 1
        "  type = string\n"  # 2
        "}\n"  # 3
        "\n"  # 4
        "output \"id\" {\n"  # 5
        "  value = var.bucket\n"  # 6
        "}\n"  # 7
    ),
}

MIXED_UNSUPPORTED: dict[str, str] = {
    "app/main.py": "def run():\n    return 1\n",
    "tools/release.rb": "def release\n  puts 'x'\nend\n",
    "README.md": "# fixture\n\ntext\n",
}


def git_env(home: Path) -> dict[str, str]:
    """An environment that reads no user or system git configuration.

    The tests must not depend on, or change, the configuration of the
    machine they run on, so commits carry their identity in variables.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    env.update(
        {
            "HOME": str(home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
    )
    return env


def _git(root: Path, env: Mapping[str, str], *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args],
        env=dict(env),
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout


def write_files(root: Path, files: Mapping[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def build_repo(
    root: Path,
    files: Mapping[str, str],
    *,
    history: Iterable[tuple[int, Mapping[str, str]]] = (),
    first_commit_at: int = 1_780_000_000,
) -> Path:
    """Write `files`, commit them, then commit each `history` step.

    `history` is a sequence of (unix time, {path: new text}); every step is
    one commit with that author and committer date. The first commit is
    made at `first_commit_at`, so a history step may be dated before or
    after it as the test needs. The HEAD commit's date anchors the
    extractor's 90-day window, which keeps the window deterministic.
    """
    root.mkdir(parents=True, exist_ok=True)
    home = root.parent / (root.name + "-home")
    home.mkdir(exist_ok=True)
    env = git_env(home)
    write_files(root, files)
    _git(root, env, "init", "-q", "-b", "main")
    _git(root, env, "add", "-A")
    stamp = f"@{first_commit_at} +0000"
    _git(
        root,
        {**env, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp},
        "commit", "-q", "-m", "fixture",
    )
    for when, changes in history:
        write_files(root, changes)
        _git(root, env, "add", "-A")
        stamp = f"@{when} +0000"
        _git(
            root,
            {**env, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp},
            "commit", "-q", "-m", f"change at {when}",
        )
    return root
