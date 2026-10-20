"""Every language the index lists gets a working language-server pass (lane INDEX-LANGS).

Part of #1693. Owner decision, 2026-10-20: the LSP pass must work for every
language the extractor lists -- Python, TypeScript, JavaScript and Go, and
Terraform (HCL), whose server, terraform-ls, had been specified but left out
of the indexer image since 2026-10-05.

Two kinds of test, kept apart on purpose:

* REAL-SERVER TESTS. One small fixture repository per language, each with a
  call (for Terraform, a reference) from one file into another. The driver's
  pass runs against the real server binary in the directory the driver
  resolves servers from (lsp.DEFAULT_BIN_DIR, the image's
  /opt/repo-index/servers/bin; SWARM_LSP_BIN_DIR names another, for a machine
  that built the servers elsewhere) and the test asserts the edge came back
  as `lsp` evidence -- the tree-sitter pass alone writes `ast`, so only an
  answering server can turn it. A unit run without the binaries skips them,
  with a reason naming the missing file; the fake-server tests in
  test_repo_index_lsp.py cover the driver's protocol paths there.
  The image build proves the same five languages with `--lsp-self-test`,
  whose table is held to the extractor's LANGUAGE_SERVERS below.

* WIRING TESTS, which run everywhere: the server is enabled for its
  language, the image installs terraform-ls pinned by version and sha256,
  the build's self-test covers every listed language, and
  docs/repo-index.md's language table says which server serves which
  language and how each is budgeted.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest

import repo_index_fixtures as fx

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL_DIR = REPO_ROOT / "images" / "agent-runtime-indexer" / "repo-index"
SCRIPT = TOOL_DIR / "repo_index_extract.py"
DOCKERFILE = REPO_ROOT / "images" / "agent-runtime-indexer" / "Dockerfile"
DOC = REPO_ROOT / "docs" / "repo-index.md"


@pytest.fixture(scope="module")
def tool() -> Any:
    spec = importlib.util.spec_from_file_location("repo_index_extract", SCRIPT)
    assert spec is not None and spec.loader is not None, f"no extractor at {SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["repo_index_extract"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def lsp(tool: Any) -> Any:
    return sys.modules["lsp"]


# ---------------------------------------------------------------------------
# The fixture repositories: one per language, a call across two files
# ---------------------------------------------------------------------------

#: language -> (files, the caller, the callee, the edge's kind). The caller
#: and callee are in different files, so the server has to follow an import
#: (or, for Terraform, a module's other file) to answer.
FIXTURES: dict[str, tuple[dict[str, str], str, str, str]] = {
    "python": (
        {
            "pyproject.toml": "[project]\nname = \"fixture\"\nversion = \"0\"\n",
            "pkg/__init__.py": "",
            "pkg/store.py": "def load_rows(path):\n    return [path]\n",
            "pkg/report.py": ("from pkg.store import load_rows\n\n\n"
                              "def build_report(path):\n    return len(load_rows(path))\n"),
        },
        "pkg/report.py#build_report", "pkg/store.py#load_rows", "call",
    ),
    "typescript": (
        {
            "tsconfig.json": "{\"compilerOptions\": {\"strict\": true}, \"include\": [\"src\"]}\n",
            "src/store.ts": "export function loadRows(path: string): string[] {\n  return [path];\n}\n",
            "src/report.ts": ("import { loadRows } from \"./store\";\n\n"
                              "export function buildReport(path: string): number {\n"
                              "  return loadRows(path).length;\n}\n"),
        },
        "src/report.ts#buildReport", "src/store.ts#loadRows", "call",
    ),
    "javascript": (
        {
            "package.json": "{\"name\": \"fixture\", \"private\": true, \"type\": \"module\"}\n",
            "src/store.js": "export function loadRows(path) {\n  return [path];\n}\n",
            "src/report.js": ("import { loadRows } from \"./store.js\";\n\n"
                              "export function buildReport(path) {\n"
                              "  return loadRows(path).length;\n}\n"),
        },
        "src/report.js#buildReport", "src/store.js#loadRows", "call",
    ),
    "go": (
        {
            "go.mod": "module example.com/fixture\n\ngo 1.22\n",
            "store/store.go": ("package store\n\n"
                               "func LoadRows(path string) []string {\n\treturn []string{path}\n}\n"),
            "report/report.go": ("package report\n\nimport \"example.com/fixture/store\"\n\n"
                                 "func BuildReport(path string) int {\n"
                                 "\treturn len(store.LoadRows(path))\n}\n"),
        },
        "report/report.go#BuildReport", "store/store.go#LoadRows", "call",
    ),
    # No resource and no provider: terraform-ls answers references from the
    # configuration alone (no `terraform init`, §3.5), and a fixture with a
    # resource would be one more thing the repository's IaC scan reads.
    "hcl": (
        {
            "variables.tf": "variable \"region\" {\n  type = string\n}\n",
            "locals.tf": "locals {\n  name = \"fixture-${var.region}\"\n}\n",
            "outputs.tf": "output \"name\" {\n  value = local.name\n}\n",
        },
        "locals.tf#local.name", "variables.tf#var.region", "reference",
    ),
}


def _bin_dir(lsp: Any) -> Path:
    return Path(os.environ.get("SWARM_LSP_BIN_DIR") or lsp.DEFAULT_BIN_DIR)


def _server_for(tool: Any, lsp: Any, language: str) -> Any:
    name = tool.LANGUAGE_SERVERS.get(language)
    assert name is not None, f"{language} has no language server in LANGUAGE_SERVERS"
    assert name in lsp.SERVERS, f"{name} serves {language} but is not an enabled server"
    return lsp.SERVERS[name]


@pytest.mark.parametrize("language", sorted(FIXTURES))
def test_the_real_server_resolves_a_call_across_two_files(tool: Any, lsp: Any, tmp_path: Path,
                                                          language: str) -> None:
    files, frm, to, kind = FIXTURES[language]
    spec = _server_for(tool, lsp, language)
    bin_dir = _bin_dir(lsp)
    needed = [spec.command[0]] + (["go"] if spec.name == "gopls" else [])
    missing = [name for name in needed if not os.access(bin_dir / name, os.X_OK)]
    if missing:
        pytest.skip(f"{', '.join(missing)} not installed in {bin_dir}: the real-server pass runs "
                    "in the agent-runtime-indexer image (or with SWARM_LSP_BIN_DIR set)")
    root = tmp_path / "repo"
    fx.write_files(root, files)
    options = lsp.LspOptions(bin_dir=bin_dir, server_budget_seconds=240.0,
                             total_budget_seconds=240.0, request_timeout_seconds=30.0)
    index = tool.extract(root, tool.Budget(file_timeout_seconds=30.0), lsp=options)
    row = next(r for r in index["languages"] if r["language"] == language)
    # MUTATION: drop `language` from its ServerSpec's `languages` (or its
    # extension from `language_ids`) and the row is `unsupported`, not `ok`.
    assert (row["status"], row["server"]) == ("ok", spec.name), row
    assert row["files_covered"] == sum(1 for path in files if tool.SUPPORTED_EXTENSIONS.get(
        os.path.splitext(path)[1], (None,))[0] == language), row
    edges = [e for e in index["call_edges"] if (e["from"], e["to"], e["kind"]) == (frm, to, kind)]
    # MUTATION: have `_definition` (or, for HCL, `_referenced`) keep no
    # target and the edge stays `ast` -- tree-sitter's guess, not the server's.
    assert [e["evidence"] for e in edges] == ["lsp"], index["call_edges"]
    assert edges[0]["confidence"] == lsp.LSP_DECLARED


# ---------------------------------------------------------------------------
# The wiring, which runs everywhere
# ---------------------------------------------------------------------------


def test_every_language_with_a_grammar_has_an_enabled_server(tool: Any, lsp: Any) -> None:
    # MUTATION: leave "hcl" out of LANGUAGE_SERVERS and this is red.
    assert set(tool.LANGUAGE_SERVERS) == set(tool.GRAMMAR_PACKAGES)
    for language, name in tool.LANGUAGE_SERVERS.items():
        # MUTATION: keep terraform-ls in DISABLED_SERVERS and this is red for hcl.
        assert name in lsp.SERVERS, (language, name)
        assert language in lsp.SERVERS[name].languages, (language, name)
    assert lsp.DISABLED_SERVERS == {}
    assert set(FIXTURES) == set(tool.LANGUAGE_SERVERS)


def test_the_extractor_version_is_bumped_so_the_first_run_asks_terraform_ls(tool: Any) -> None:
    # A version-4 graph's HCL row is `unsupported` and holds no `lsp` edge.
    # An incremental run carries an unchanged file's edges from its base and
    # takes the WORSE of its own status and the base's, so HCL would stay
    # `unsupported` until some unrelated full run. Version 5 is refused as a
    # base by a version-4 graph's carry check, so the first run is full.
    # MUTATION: leave EXTRACTOR_VERSION at "4" and this is red.
    assert tool.EXTRACTOR_VERSION == "5"


def test_terraform_ls_answers_references_for_every_terraform_extension(tool: Any, lsp: Any) -> None:
    spec = lsp.SERVERS["terraform-ls"]
    assert spec.command == ("terraform-ls", "serve")
    assert spec.languages == ("hcl",)
    # No call hierarchy in terraform-ls: references are its only way to say
    # who uses a variable, a local, a module or an output.
    assert spec.references and not spec.call_hierarchy
    hcl = {ext for ext, (language, _) in tool.SUPPORTED_EXTENSIONS.items() if language == "hcl"}
    # A `.hcl` file that is not Terraform (a `.tftest.hcl`, a Packer
    # template) is not opened in terraform-ls; `.tf` and `.tfvars` are.
    assert {".tf", ".tfvars"} <= set(spec.language_ids) <= hcl


def test_the_build_self_test_proves_every_listed_language(tool: Any) -> None:
    # MUTATION: drop the "hcl" (or "javascript") entry from
    # _LSP_SELF_TEST_EDGES and the image could ship a server that resolves nothing.
    assert set(tool._LSP_SELF_TEST_EDGES) == set(tool.LANGUAGE_SERVERS)
    for language, (frm, to) in tool._LSP_SELF_TEST_EDGES.items():
        top = frm.split("/", 1)[0]
        assert to.split("/", 1)[0] == top, language
        # Two files: the edge crosses a file, as a repository's would.
        assert frm.split("#", 1)[0] != to.split("#", 1)[0], language
        for path in (frm.split("#", 1)[0], to.split("#", 1)[0]):
            assert path in tool._LSP_SELF_TEST_FILES, (language, path)


def _code(path: Path) -> str:
    return "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#"))


def test_the_image_builds_terraform_ls_pinned_by_version_and_sha256(lsp: Any) -> None:
    code = _code(DOCKERFILE)
    args = dict(re.findall(r"^ARG (\w+)=(\S+)$", code, re.M))
    # MUTATION: drop the TERRAFORM_LS_* ARGs and this is red.
    assert re.fullmatch(r"\d+\.\d+\.\d+", args.get("TERRAFORM_LS_VERSION", "")), args
    assert re.fullmatch(r"[0-9a-f]{64}", args.get("TERRAFORM_LS_SHA256", "")), args
    # Compiled from its module source, like gopls -- HashiCorp's zip carried
    # HIGH CVEs (release 37289476429) -- with the module's bytes checked
    # against the pinned sha256 before anything is built from them.
    builds = dict(re.findall(r'build_server (\S+) "\$\{(\w+)\}" /usr/local/bin/', code))
    assert builds.get("github.com/hashicorp/terraform-ls") == "TERRAFORM_LS_VERSION", builds
    assert "releases.hashicorp.com" not in code
    assert re.search(r'check_module_zip github\.com/hashicorp/terraform-ls "\$\{TERRAFORM_LS_VERSION\}" '
                     r'"\$\{TERRAFORM_LS_SHA256\}"', code)
    assert re.search(r'check_module_zip\(\) \{.{0,600}?sha256sum -c -', code, re.S)
    # The flagged golang.org/x modules are raised for it too, and the build
    # proves each landed (`go version -m`).
    for arg in ("X_MOD_VERSION", "X_TEXT_VERSION", "X_CRYPTO_VERSION", "X_NET_VERSION"):
        assert re.fullmatch(r"\d+\.\d+\.\d+", args.get(arg, "")), arg
    # Linked into the one directory the driver resolves servers from, and
    # checked executable there with the others.
    assert "COPY --from=languageservers /usr/local/bin/terraform-ls /opt/repo-index/servers/bin/" in code
    check = re.search(r"for s in ([^;]+); do \\\n\s*test -x", code)
    assert check and "terraform-ls" in check.group(1).split()


def _language_table(text: str) -> dict[str, list[str]]:
    """docs/repo-index.md's language table: extractor language -> its row's cells."""
    lines = text.splitlines()
    for at, line in enumerate(lines):
        if re.match(r"^\| language \| files \| server \|", line):
            rows = {}
            for row in lines[at + 2:]:
                if not row.startswith("|"):
                    break
                cells = [cell.strip() for cell in row.strip("|").split("|")]
                rows[cells[0].strip("`")] = cells
            return rows
    return {}


def test_the_doc_says_which_server_serves_each_language_and_its_budget(tool: Any, lsp: Any) -> None:
    table = _language_table(DOC.read_text(encoding="utf-8"))
    # MUTATION: delete the table from docs/repo-index.md §3.5 and this is red.
    assert set(table) == set(tool.LANGUAGE_SERVERS), table
    for language, name in tool.LANGUAGE_SERVERS.items():
        cells = table[language]
        assert f"`{name}`" in cells[2], (language, cells)
        assert " ".join(lsp.SERVERS[name].command) in " ".join(cells), (language, cells)
        # The budget cell names how the server is granted its time.
        assert re.search(r"share|grant", cells[-1]), (language, cells)
