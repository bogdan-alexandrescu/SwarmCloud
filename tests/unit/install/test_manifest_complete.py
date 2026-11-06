"""The test that fails on an unlisted dependency (design §3.3).

Each collector reads the tree (offline: no credentials, no emulator, no
network) and returns the references it finds, each mapped to where it was found.
Two directions:

* (a) `test_every_reference_is_listed`: every collected reference is matched by
  some manifest entry's `refs:` (`env:` for environment variables), or is in
  the manifest's `ignored:` section with a reason;
* (b) `test_every_entry_ref_still_matches`: every entry's `refs:` pattern, every
  `ignored:` match and every `env:` name still matches something collected, so
  a stale entry fails too.

`test_fragments_merged_and_ids_unique` holds the per-lane fragment format
(docs/design/day-one-plan.md §1 conventions).
"""

from __future__ import annotations

import ast
import fnmatch
import json
import re
import subprocess
from pathlib import Path

import pytest

from swarm_install import manifest as m

ROOT = m.REPO_ROOT
TF = ROOT / "terraform"

_TF_HEADER = re.compile(r'^\s*(resource|data)\s+"([a-z0-9_]+)"\s+"([A-Za-z0-9_-]+)"')
_TF_HEADER_START = re.compile(r"^\s*(resource|data)\s*(\"|$)")
_API = re.compile(r'"([a-z0-9]+\.googleapis\.com)"')
_SECRET_ID = re.compile(r'\bsecret_id\s*=\s*"(swarm-[^"]+)"')
_VARIABLE = re.compile(r'^variable\s+"([A-Za-z0-9_]+)"\s*\{')
_BOOL_TYPE = re.compile(r"^\s*type\s*=\s*bool\s*$")
_VITE = re.compile(r"import\.meta\.env\.(VITE_[A-Z0-9_]+)")
_MAY_READ_ENV = re.compile(r"environ|getenv|\benv(\.get\(|\[)")
_SCRIPT_FLAG = re.compile(r"^\s*(--[a-z0-9-]+)\)")


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _tf_files() -> list[tuple[str, Path]]:
    """(root key, file) for terraform/bootstrap, terraform/infra and each module."""
    out: list[tuple[str, Path]] = []
    for root in ("bootstrap", "infra"):
        out += [(root, p) for p in sorted((TF / root).glob("*.tf"))]
    for p in sorted(TF.glob("modules/*/*.tf")):
        out.append((f"modules/{p.parent.name}", p))
    return out


# ---- the collectors (design §3.3, numbered as there) ----


def collect_terraform() -> dict[str, str]:
    """1. Every `resource` and `data` block: `<root>/<type>.<name>`, and
    `<root>/data.<type>.<name>` for a data source, as Terraform addresses it."""
    found: dict[str, str] = {}
    multi_line: list[str] = []
    for root, path in _tf_files():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = _TF_HEADER.match(line)
            if match:
                data = "data." if match.group(1) == "data" else ""
                found[f"{root}/{data}{match.group(2)}.{match.group(3)}"] = f"{_rel(path)}:{n}"
            elif _TF_HEADER_START.match(line):
                multi_line.append(f"{_rel(path)}:{n}")
    assert not multi_line, (
        "a resource or data header is not on one line, so the collector cannot read it: "
        f"{multi_line}"
    )
    return found


def _api_literals(path: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        code = line.split("#", 1)[0]
        for api in _API.findall(code):
            found.setdefault(api, f"{_rel(path)}:{n}")
    return found


def _bash_array(path: Path, name: str) -> dict[str, str]:
    found: dict[str, str] = {}
    inside = False
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith(f"{name}=("):
            inside = True
            rest = line[len(name) + 2 :]
        elif inside:
            rest = line
        else:
            continue
        body, closed = (rest.split(")", 1)[0], True) if ")" in rest else (rest, False)
        for word in body.split("#", 1)[0].split():
            found.setdefault(word, f"{_rel(path)}:{n}")
        if closed:
            break
    assert found, f"{_rel(path)}: no {name}=( ... ) array found"
    return found


def collect_apis() -> dict[str, str]:
    """2. The `google_project_service` lists and prerequisites.sh's REQUIRED_APIS."""
    found: dict[str, str] = {}
    found.update(_api_literals(TF / "infra" / "main.tf"))
    found.update(_api_literals(TF / "bootstrap" / "variables.tf"))
    for api, where in _bash_array(ROOT / "scripts" / "prerequisites.sh", "REQUIRED_APIS").items():
        found.setdefault(api, where)
    return found


def _git_files(*patterns: str) -> list[str]:
    """Tracked files matching the pathspecs: the tree, never a build or cache."""
    return subprocess.run(
        ["git", "ls-files", "--", *patterns],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()


def _python_service_files() -> list[Path]:
    """Every tracked Python file under apps/, tests excluded."""
    files = []
    for name in _git_files("apps/*.py"):
        parts = Path(name).parts
        if any(p in ("tests", "test") for p in parts) or parts[-1].startswith("test_"):
            continue
        files.append(ROOT / name)
    return files


_ENVIRON_NAMES = ("environ", "env")


def _is_environ(node: ast.AST) -> bool:
    """`os.environ`, or a mapping passed in as `environ` or `env`."""
    return (isinstance(node, ast.Attribute) and node.attr == "environ") or (
        isinstance(node, ast.Name) and node.id in _ENVIRON_NAMES
    )


def _env_key(node: ast.AST) -> ast.AST | None:
    """The key expression of an environment read, or None if `node` is not one."""
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, ast.Load)
        and _is_environ(node.value)
    ):
        return node.slice
    if isinstance(node, ast.Call) and node.args:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "get" and _is_environ(func.value):
            return node.args[0]
        if (isinstance(func, ast.Attribute) and func.attr == "getenv") or (
            isinstance(func, ast.Name) and func.id == "getenv"
        ):
            return node.args[0]
    return None


def _env_helpers(tree: ast.AST) -> dict[str, int]:
    """Functions that read the environment under one of their parameters, e.g.
    settings.py's `_csv(name, default)`: name -> that parameter's position."""
    helpers: dict[str, int] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = [a.arg for a in fn.args.posonlyargs + fn.args.args]
        for node in ast.walk(fn):
            key = _env_key(node)
            if isinstance(key, ast.Name) and key.id in params:
                helpers[fn.name] = params.index(key.id)
                break
    return helpers


def _env_names(tree: ast.AST) -> list[tuple[str, int]]:
    """Every literal environment variable name the module reads, directly or
    through one of its own helpers."""
    helpers = _env_helpers(tree)
    names: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        key = _env_key(node)
        if key is None and isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            position = helpers.get(node.func.id)
            if position is not None and len(node.args) > position:
                key = node.args[position]
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            names.append((key.value, node.lineno))
    return names


def collect_env() -> dict[str, str]:
    """3. Environment variables services read: Python `ast`, and `VITE_*` in the UI."""
    found: dict[str, str] = {}
    for path in _python_service_files():
        text = path.read_text(encoding="utf-8")
        if not _MAY_READ_ENV.search(text):
            continue  # cannot read the environment: skip the parse, the slow part
        tree = ast.parse(text, filename=str(path))
        for name, line in _env_names(tree):
            found.setdefault(name, f"{_rel(path)}:{line}")
    for name in _git_files("apps/swarm-ui/src/*.ts", "apps/swarm-ui/src/*.tsx"):
        path = ROOT / name
        if "__tests__" in path.parts:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for name in _VITE.findall(line):
                found.setdefault(name, f"{_rel(path)}:{n}")
    return found


def collect_secrets() -> dict[str, str]:
    """4. Secret ids: Terraform `secret_id`, swarm-api's PLATFORM_SECRETS.

    An interpolated id is collected with each `${...}` replaced by `*`."""
    found: dict[str, str] = {}
    for _, path in _tf_files():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for secret in _SECRET_ID.findall(line):
                found.setdefault(re.sub(r"\$\{[^}]*\}", "*", secret), f"{_rel(path)}:{n}")
    from swarm_api.platform_secrets import PLATFORM_SECRETS

    source = "apps/swarm-api/swarm_api/platform_secrets.py"
    for secret in PLATFORM_SECRETS:
        found.setdefault(secret.secret_id, source)
    return found


def collect_secret_flags() -> dict[str, str]:
    """4. The `--<name>)` cases of scripts/create-secrets.sh's argument loop."""
    path = ROOT / "scripts" / "create-secrets.sh"
    found: dict[str, str] = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = _SCRIPT_FLAG.match(line)
        if match:
            found.setdefault(match.group(1), f"{_rel(path)}:{n}")
    assert found, "scripts/create-secrets.sh: no --<name>) case found"
    return found


def collect_scheduler_jobs() -> dict[str, str]:
    """5. `google_cloud_scheduler_job` resources in the scheduler module."""
    path = TF / "modules" / "scheduler" / "jobs.tf"
    found: dict[str, str] = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = _TF_HEADER.match(line)
        if match and match.group(2) == "google_cloud_scheduler_job":
            found[match.group(3)] = f"{_rel(path)}:{n}"
    return found


def collect_images() -> dict[str, str]:
    """6. images.json's names, build-images.sh's ALL_TARGETS, every tracked Dockerfile."""
    found: dict[str, str] = {}
    images = json.loads((TF / "infra" / "images.json").read_text(encoding="utf-8"))
    for group, names in images.items():
        for name in names:
            found.setdefault(name, f"terraform/infra/images.json ({group})")
    found.update(
        {
            k: v
            for k, v in _bash_array(ROOT / "scripts" / "build-images.sh", "ALL_TARGETS").items()
            if k not in found
        }
    )
    for path in _git_files("*Dockerfile", "Dockerfile"):
        found[f"dockerfile:{path}"] = path
    return found


def collect_kubernetes() -> dict[str, str]:
    """7. render.py's POLICY_FILES and TENANT_FILES, and kubernetes/swarm-system/."""
    tree = ast.parse((ROOT / "kubernetes" / "render.py").read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in ("POLICY_FILES", "TENANT_FILES")
            for t in node.targets
        ):
            assert isinstance(node.value, ast.Tuple)
            for elt in node.value.elts:
                assert isinstance(elt, ast.Constant)
                found[elt.value] = f"kubernetes/render.py:{elt.lineno}"
    for path in sorted((ROOT / "kubernetes" / "swarm-system").glob("*")):
        if path.is_file():
            found[path.relative_to(ROOT / "kubernetes").as_posix()] = _rel(path)
    return found


def collect_variables() -> dict[str, str]:
    """8. Every `enable_*` and every `bool` variable in both roots."""
    found: dict[str, str] = {}
    for root in ("bootstrap", "infra"):
        for path in sorted((TF / root).glob("*.tf")):
            current: tuple[str, int] | None = None
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                match = _VARIABLE.match(line)
                if match:
                    current = (match.group(1), n)
                    if current[0].startswith("enable_"):
                        found[f"{root}/{current[0]}"] = f"{_rel(path)}:{n}"
                elif current and _BOOL_TYPE.match(line):
                    found[f"{root}/{current[0]}"] = f"{_rel(path)}:{current[1]}"
                elif line.startswith("}"):
                    current = None
    return found


COLLECTORS = {
    "terraform": collect_terraform,
    "api": collect_apis,
    "env": collect_env,
    "secret": collect_secrets,
    "secret_flag": collect_secret_flags,
    "scheduler_job": collect_scheduler_jobs,
    "image": collect_images,
    "kubernetes": collect_kubernetes,
    "variable": collect_variables,
}
assert tuple(COLLECTORS) == m.REF_KINDS


@pytest.fixture(scope="module")
def collected() -> dict[str, dict[str, str]]:
    return {kind: collect() for kind, collect in COLLECTORS.items()}


@pytest.fixture(scope="module")
def manifest() -> m.Manifest:
    return m.load()


def unmatched_references(
    manifest: m.Manifest, collected: dict[str, dict[str, str]]
) -> list[str]:
    """Every collected reference no entry, `env:` name or `ignored:` match covers."""
    missing: list[str] = []
    for kind, refs in collected.items():
        for ref, where in sorted(refs.items()):
            if kind == "env":
                listed = ref in manifest.env
            else:
                listed = bool(manifest.matches(kind, ref))
            if not listed and manifest.ignored_reason(kind, ref) is None:
                missing.append(f"{kind}: {ref} ({where})")
    return missing


def stale_references(manifest: m.Manifest, collected: dict[str, dict[str, str]]) -> list[str]:
    """Every `refs:` pattern, `ignored:` match and `env:` name that matches nothing."""

    def hits(kind: str, pattern: str) -> bool:
        return any(fnmatch.fnmatchcase(ref, pattern) for ref in collected[kind])

    stale = [
        f"{entry.id}: refs.{kind} {pattern!r}"
        for entry in manifest.entries
        for kind, pattern in entry.refs.items()
        if not hits(kind, pattern)
    ]
    stale += [
        f"ignored.{kind}: {item.match!r}"
        for kind, items in manifest.ignored.items()
        for item in items
        if not hits(kind, item.match)
    ]
    stale += [f"env: {name}" for name in sorted(manifest.env) if name not in collected["env"]]
    return stale


def test_collectors_find_the_tree(collected: dict[str, dict[str, str]]) -> None:
    """A collector that finds nothing has broken, not proved the tree clean."""
    empty = [kind for kind, refs in collected.items() if not refs]
    assert not empty, f"collectors that found nothing: {empty}"
    # Spot checks against design §3.3's own numbers and examples.
    assert "infra/google_project_service.this" not in collected["terraform"]
    assert "modules/project_services/google_project_service.this" in collected["terraform"]
    assert "run.googleapis.com" in collected["api"]
    assert "ALLOWED_DOMAINS" in collected["env"]
    assert {"safety_tick", "mcp_credentials_sweep"} <= set(collected["scheduler_job"])
    assert "infra/tenant_job_vpc_egress" in collected["variable"]


def test_every_reference_is_listed(
    manifest: m.Manifest, collected: dict[str, dict[str, str]]
) -> None:
    unmatched = unmatched_references(manifest, collected)
    assert not unmatched, (
        f"{len(unmatched)} references in the tree are in no manifest entry's refs:, "
        "env: or ignored: (add an entry, or ignore it with a reason):\n" + "\n".join(unmatched)
    )


def test_every_entry_ref_still_matches(
    manifest: m.Manifest, collected: dict[str, dict[str, str]]
) -> None:
    stale = stale_references(manifest, collected)
    assert not stale, "manifest refs that match nothing in the tree:\n" + "\n".join(stale)


def test_env_setters_point_at_the_tree(manifest: m.Manifest) -> None:
    """An `env:` setter is `internal: <reason>` or a `path:line` that exists."""
    bad = []
    for name, var in sorted(manifest.env.items()):
        if var.setter.startswith("internal: "):
            continue
        path, _, line = var.setter.partition(":")
        target = ROOT / path
        if not (target.is_file() and line.isdigit()):
            bad.append(f"{name}: {var.setter}")
            continue
        if int(line) > len(target.read_text(encoding="utf-8").splitlines()):
            bad.append(f"{name}: {var.setter} (past the end of the file)")
    assert not bad, "env: setters that point at nothing:\n" + "\n".join(bad)


def test_api_entries_are_in_a_list_or_new(
    manifest: m.Manifest, collected: dict[str, dict[str, str]]
) -> None:
    """Collector 2's other half: every `api.*` entry is in a list, or `new: true`."""
    orphans = [
        e.id
        for e in manifest.entries
        if e.kind == "gcp-api" and not e.new and not e.id.endswith("-NOT-enabled")
        and not any(k == "api" for k, _ in e.refs.items())
    ]
    assert not orphans, f"api entries in no list and not new: true: {orphans}"


def _write(path: Path, data: dict) -> Path:
    import yaml

    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _entry(entry_id: str, **extra: object) -> dict:
    return {
        "id": entry_id,
        "kind": "other",
        "scope": "core",
        "stage": 7,
        "provider": "engine",
        **extra,
    }


def test_fragments_merged_and_ids_unique(tmp_path: Path) -> None:
    base = _write(
        tmp_path / "manifest.yaml",
        {"entries": [_entry("other.base"), _entry("other.kept")]},
    )
    frags = tmp_path / "manifest.d"
    frags.mkdir()
    _write(
        frags / "lane-a.yaml",
        {
            "entries": [
                _entry("switch.new_one", refs={"variable": ["infra/enable_new_one"]}),
                _entry("other.base", replace=True, notes="rewritten by lane-a"),
            ],
            "env": {"NEW_VAR": {"setter": "internal: lane-a's own default"}},
        },
    )
    merged = m.load(base, frags)
    by_id = merged.by_id()
    # A fragment's new entry is collected, and its refs match.
    assert "switch.new_one" in by_id
    assert merged.matches("variable", "infra/enable_new_one") == ["switch.new_one"]
    assert merged.sources["switch.new_one"] == "lane-a.yaml"
    assert by_id["other.base"].notes == "rewritten by lane-a"
    assert "NEW_VAR" in merged.env

    # Two fragments restating one id fail.
    _write(frags / "lane-b.yaml", {"entries": [_entry("other.base", replace=True)]})
    with pytest.raises(m.ManifestError, match="also restated by lane-a.yaml"):
        m.load(base, frags)
    (frags / "lane-b.yaml").unlink()

    # A fragment restating a base id without replace: true fails.
    _write(frags / "lane-c.yaml", {"entries": [_entry("other.kept")]})
    with pytest.raises(m.ManifestError, match="restate it with replace: true"):
        m.load(base, frags)
    (frags / "lane-c.yaml").unlink()

    # Two fragments adding one new id fail.
    _write(frags / "lane-d.yaml", {"entries": [_entry("switch.new_one")]})
    with pytest.raises(m.ManifestError, match="also restated"):
        m.load(base, frags)
    (frags / "lane-d.yaml").unlink()

    # replace: true with nothing to replace, and one env name set twice, fail.
    _write(frags / "lane-e.yaml", {"entries": [_entry("other.ghost", replace=True)]})
    with pytest.raises(m.ManifestError, match="no such entry"):
        m.load(base, frags)
    (frags / "lane-e.yaml").unlink()
    _write(frags / "lane-f.yaml", {"env": {"NEW_VAR": {"setter": "internal: again here"}}})
    with pytest.raises(m.ManifestError, match="NEW_VAR is already set"):
        m.load(base, frags)


def test_stale_entry_and_unlisted_reference_are_reported(tmp_path: Path) -> None:
    """The two directions on a hand-built tree of references."""
    base = _write(
        tmp_path / "manifest.yaml",
        {
            "entries": [_entry("other.stale", refs={"terraform": ["infra/google_x.gone"]})],
            "ignored": {"terraform": [{"match": "*/terraform_data.*", "reason": "creates nothing"}]},
            "env": {"GONE_VAR": {"setter": "internal: was read once"}},
        },
    )
    merged = m.load(base, tmp_path / "absent")
    collected = {kind: {} for kind in m.REF_KINDS}
    collected["terraform"] = {
        "infra/google_y.new": "a.tf:1",
        "infra/terraform_data.ok": "a.tf:9",
    }
    collected["env"] = {"NEW_VAR": "x.py:3"}
    assert unmatched_references(merged, collected) == [
        "terraform: infra/google_y.new (a.tf:1)",
        "env: NEW_VAR (x.py:3)",
    ]
    assert stale_references(merged, collected) == [
        "other.stale: refs.terraform 'infra/google_x.gone'",
        "env: GONE_VAR",
    ]
