"""The language servers §3.5 names, and how each is started.

docs/repo-index.md §3.5 (revised 2026-10-04): pyright for Python, tsserver
(through typescript-language-server) for TypeScript and JavaScript, gopls
for Go and terraform-ls for HCL. The image installs all four into
DEFAULT_BIN_DIR (images/agent-runtime-indexer/Dockerfile, versions pinned
there and in lsp/package-lock.json), and the driver resolves a command ONLY
there: never from PATH, so nothing a checkout or an agent put on a PATH can
stand in for a server.

What each spec says:

  command          argv; argv[0] is a name in the bin directory, or an
                   absolute path (the tests' fake server)
  languages        the extractor's language names it serves. tsserver
                   serves two, in ONE process: the workspace load is most
                   of a server's cost, so it is paid once
  language_ids     file extension -> the LSP languageId for didOpen. A file
                   whose extension is not here is never opened, and its
                   sites stay `ast`
  call_hierarchy   ask `callHierarchy/incomingCalls` for exported functions
                   and methods. terraform-ls has none
  references       ask `textDocument/references` for the symbols of
                   `reference_kinds` instead -- terraform-ls's only way to
                   say who uses a variable, a resource or a module
  settings         answers to `workspace/configuration`
  env              added to the minimal environment the driver builds

The per-language adapters (RI10a-d: lsp/python.py, typescript.py, go.py,
terraform.py) refine routes and test discovery on top of these; the
protocol, the budget and the fallbacks are the driver's, for all four.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

# Where the image links every server command (Dockerfile, "language servers").
DEFAULT_BIN_DIR = Path("/opt/repo-index/servers/bin")


@dataclass(frozen=True)
class ServerSpec:
    name: str
    command: tuple[str, ...]
    languages: tuple[str, ...]
    language_ids: Mapping[str, str]
    call_hierarchy: bool = True
    references: bool = False
    reference_kinds: tuple[str, ...] = ()
    initialization_options: Mapping[str, Any] | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)


SERVERS: dict[str, ServerSpec] = {
    "pyright": ServerSpec(
        name="pyright",
        command=("pyright-langserver", "--stdio"),
        languages=("python",),
        language_ids={".py": "python", ".pyi": "python"},
        # openFilesOnly: diagnostics for the file being asked about, not a
        # full-workspace check whose output the index never reads. No
        # library code indexing: dependencies are not installed (§3.5).
        settings={
            "python": {"analysis": {"diagnosticMode": "openFilesOnly",
                                    "indexing": False,
                                    "useLibraryCodeForTypes": False,
                                    "autoSearchPaths": True}},
            "pyright": {"disableLanguageServices": False},
        },
    ),
    "tsserver": ServerSpec(
        name="tsserver",
        command=("typescript-language-server", "--stdio"),
        languages=("typescript", "javascript"),
        language_ids={".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
                      ".tsx": "typescriptreact", ".js": "javascript", ".mjs": "javascript",
                      ".cjs": "javascript", ".jsx": "javascriptreact"},
        # No automatic type acquisition: it downloads @types packages.
        #
        # useSyntaxServer "never": typescript-language-server's default
        # ("auto") starts a second, partial-semantic tsserver and routes
        # definition, references and the like to it for as long as the
        # semantic server is still loading the project (its first-start
        # state, until a projectLoadingFinish or diagnostics event). This
        # driver asks for a definition right after didOpen, so every answer
        # came from the syntax server, which cannot follow an import to
        # another file: the build's LSP self-test got "typescript: ok, 0 lsp
        # edge(s)" (main, 2026-10-05). "never" runs one full server.
        initialization_options={"disableAutomaticTypingAcquisition": True,
                                "tsserver": {"useSyntaxServer": "never"},
                                "preferences": {"includeCompletionsForModuleExports": False}},
    ),
    "gopls": ServerSpec(
        name="gopls",
        command=("gopls", "serve"),
        languages=("go",),
        language_ids={".go": "go"},
        # §3.5 "no dependencies are installed": no module download, no
        # toolchain download, no telemetry upload. A module that needs a
        # missing dependency still resolves its in-repository calls. GOFLAGS
        # is left alone on purpose: `-mod=mod` would let `go list` rewrite
        # the checkout's go.mod and go.sum, and the index never writes to
        # the repository it reads.
        env={"GOPROXY": "off", "GOTOOLCHAIN": "local", "GOTELEMETRY": "off", "CGO_ENABLED": "0"},
    ),
    "terraform-ls": ServerSpec(
        name="terraform-ls",
        command=("terraform-ls", "serve"),
        languages=("hcl",),
        language_ids={".tf": "terraform", ".tfvars": "terraform-vars"},
        call_hierarchy=False,
        references=True,
        reference_kinds=("variable", "resource", "data", "module", "output", "local"),
        # No `terraform init` and no provider schema download.
        initialization_options={"experimentalFeatures": {"prefillRequiredFields": False},
                                "validation": {"enableEnhancedValidation": False}},
    ),
}

# terraform-ls is NOT in the image for now (owner decision 2026-10-05). The
# vendor zip carried HIGH CVEs (release 37289476429), and a source build with
# the golang.org/x modules raised answered 0 references in the image's LSP
# self-test (main a3621325, "hcl: ok, 0 lsp edge(s)"). So HCL is indexed by
# tree-sitter alone and reports `unsupported` in the LSP pass. The spec is kept
# here so lane IDX can re-enable it in agent-runtime-indexer once it resolves.
DISABLED_SERVERS: dict[str, ServerSpec] = {"terraform-ls": SERVERS.pop("terraform-ls")}
