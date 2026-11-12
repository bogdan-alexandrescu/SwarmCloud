"""The label-key prefix is the setting SWARM_LABEL_PREFIX, never a value of the repository.

The owner answered "configurable" on 8 November 2026
(docs/design/generic-repository.md section 3.3): every label and annotation
key the platform writes is `<prefix>/<name>`, the prefix is SWARM_LABEL_PREFIX,
else `swarm.<SWARM_ORG_DOMAIN>`, and with neither the render refuses with one
fixed sentence instead of guessing a key (kubernetes/label_prefix.py).

The keys the live cluster's admission policies match must render exactly as
they do today when the setting carries today's prefix. fixtures/
label_prefix_keys.json is every key name under the prefix, in order, as each
template and each render held them before the prefix became a setting
(written with the placeholder prefix in place of the live one); the tests
below hold the templates' `__LABEL_PREFIX__` keys and every render's keys to
it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
KUBERNETES = REPO / "kubernetes"
RENDER = KUBERNETES / "render.py"
LABEL_PREFIX_PY = KUBERNETES / "label_prefix.py"
APPLY = KUBERNETES / "apply.sh"
FIXTURE = json.loads((Path(__file__).resolve().parent / "fixtures" / "label_prefix_keys.json").read_text())

_spec = importlib.util.spec_from_file_location("placeholders", REPO / "tests" / "placeholders.py")
_placeholders = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_placeholders)
ORG_DOMAIN = _placeholders.ORG_DOMAIN
#: The placeholder deployment's prefix: swarm.<placeholder organisation domain>.
PREFIX = f"swarm.{ORG_DOMAIN}"
TOKEN = "__LABEL_PREFIX__"
UNSET = (
    "the label-key prefix is not configured: set SWARM_LABEL_PREFIX, or SWARM_ORG_DOMAIN "
    "for the default swarm.<org_domain>; no key is guessed"
)

#: The six swarm-system manifests terraform/infra renders and kubernetes/apply.sh
#: applies: (template, terraform output, the file apply.sh reads it into).
TERRAFORM_RENDERS = (
    ("swarm-system/swarm-live.yaml", "swarm_live_manifest", "relay.yaml"),
    ("swarm-system/swarm-egress.yaml", "swarm_egress_manifest", "egress.yaml"),
    ("swarm-system/tailscale-egress.yaml", "tailscale_egress_manifest", "tailscale.yaml"),
    ("swarm-system/swarm-graph.yaml", "swarm_graph_manifest", "graph.yaml"),
    ("swarm-system/swarm-typesafe-fwd.yaml", "swarm_typesafe_fwd_manifest", "fwd.yaml"),
    ("swarm-system/swarm-voice.yaml", "swarm_voice_manifest", "voice.yaml"),
)

#: The renders, as apply.sh and the installer ask for them (offline network values).
RENDERS = {
    "policies": ["policies", "--project", _placeholders.PROJECT],
    "tenant": ["tenant", "--tenant", _placeholders.TEAM_TENANT, "--project", _placeholders.PROJECT,
               "--workspace-id", "w-1a2b3c"],
    "swarm-system": ["swarm-system"],
    "job": ["job", "--tenant", _placeholders.TEAM_TENANT, "--project", _placeholders.PROJECT,
            "--profile", "browser", "--task", "t1", "--attempt", "a1", "--lease", "l1", "--generation", "3"],
}


def _env(**setting: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("SWARM_LABEL_PREFIX", "SWARM_ORG_DOMAIN")}
    env.update(setting)
    return env


def _render(name: str, **setting: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(RENDER), *RENDERS[name]], capture_output=True, text=True,
                          env=_env(**setting), timeout=120, check=False)


def _keys(text: str, prefix: str) -> list[str]:
    return re.findall(re.escape(prefix) + r"/([a-z0-9-]+)", text)


# --------------------------------------------------------------------------
# the rule
# --------------------------------------------------------------------------


def _module():
    spec = importlib.util.spec_from_file_location("swarm_label_prefix_test", LABEL_PREFIX_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_setting_is_the_prefix_and_the_org_domain_its_default() -> None:
    module = _module()
    assert module.label_prefix({"SWARM_LABEL_PREFIX": "labels.example.net"}) == "labels.example.net"
    assert module.label_prefix({"SWARM_ORG_DOMAIN": ORG_DOMAIN}) == PREFIX
    # The setting wins over the domain.
    assert module.label_prefix({"SWARM_LABEL_PREFIX": "labels.example.net",
                                "SWARM_ORG_DOMAIN": ORG_DOMAIN}) == "labels.example.net"
    assert module.label_key("workspace", {"SWARM_ORG_DOMAIN": ORG_DOMAIN}) == f"{PREFIX}/workspace"


def test_neither_setting_is_a_refusal_with_the_fixed_sentence() -> None:
    module = _module()
    for env in ({}, {"SWARM_LABEL_PREFIX": "", "SWARM_ORG_DOMAIN": "  "}):
        with pytest.raises(module.LabelPrefixError) as refused:
            module.label_prefix(env)
        assert str(refused.value) == UNSET
    assert module.UNSET == UNSET


@pytest.mark.parametrize("value", ["swarm", "Swarm.Example.com", "swarm.example.com/x", "swarm.example.com\nkey: v",
                                   "-swarm.example.com", "swarm..example.com", "a" * 250 + ".com"])
def test_a_prefix_that_is_not_a_key_prefix_is_refused(value: str) -> None:
    module = _module()
    with pytest.raises(module.LabelPrefixError):
        module.label_prefix({"SWARM_LABEL_PREFIX": value})


@pytest.mark.parametrize("value", ["example", "example.com/x", "exa mple.com"])
def test_an_org_domain_that_is_not_a_domain_is_refused(value: str) -> None:
    module = _module()
    with pytest.raises(module.LabelPrefixError):
        module.label_prefix({"SWARM_ORG_DOMAIN": value})


def test_the_command_line_prints_the_prefix_and_refuses_without_one() -> None:
    run = [sys.executable, str(LABEL_PREFIX_PY)]
    done = subprocess.run(run, capture_output=True, text=True, env=_env(SWARM_ORG_DOMAIN=ORG_DOMAIN), check=False)
    assert (done.returncode, done.stdout) == (0, f"{PREFIX}\n"), done.stderr
    done = subprocess.run([*run, "--key", "workspace"], capture_output=True, text=True,
                          env=_env(SWARM_ORG_DOMAIN=ORG_DOMAIN), check=False)
    assert (done.returncode, done.stdout) == (0, f"{PREFIX}/workspace\n"), done.stderr
    done = subprocess.run(run, capture_output=True, text=True, env=_env(), check=False)
    assert done.returncode != 0 and done.stdout == ""
    assert UNSET in done.stderr


# --------------------------------------------------------------------------
# the manifests: the token, and the keys of today
# --------------------------------------------------------------------------


def _templates() -> list[Path]:
    return sorted(KUBERNETES.rglob("*.yaml"))


def test_no_manifest_spells_a_prefix_only_the_token() -> None:
    """Every `swarm.<domain>/<name>` key in kubernetes/ is `__LABEL_PREFIX__/<name>`."""
    literal = re.compile(r"\bswarm\.[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,63}/[a-z0-9-]+")
    found = [f"{p.relative_to(REPO)}: {m.group(0)}" for p in _templates() for m in literal.finditer(p.read_text())]
    for path in (RENDER, KUBERNETES / "network_parity.py", APPLY):
        found += [f"{path.relative_to(REPO)}: {m.group(0)}" for m in literal.finditer(path.read_text())]
    assert not found, "\n".join(found)


def test_every_template_carries_todays_keys_under_the_token() -> None:
    held = {str(p.relative_to(REPO)): _keys(p.read_text(), TOKEN) for p in _templates()}
    held = {path: names for path, names in held.items() if names}
    assert held == FIXTURE["templates"]


@pytest.mark.parametrize("name", sorted(RENDERS))
def test_a_render_with_the_prefix_carries_todays_keys_exactly(name: str) -> None:
    done = _render(name, SWARM_LABEL_PREFIX=PREFIX)
    assert done.returncode == 0, done.stderr[-2000:]
    assert TOKEN not in done.stdout
    assert _keys(done.stdout, PREFIX) == FIXTURE["renders"][name]


@pytest.mark.parametrize("name", sorted(RENDERS))
def test_the_org_domain_alone_renders_the_same_bytes(name: str) -> None:
    by_prefix = _render(name, SWARM_LABEL_PREFIX=PREFIX)
    by_domain = _render(name, SWARM_ORG_DOMAIN=ORG_DOMAIN)
    assert by_domain.returncode == 0, by_domain.stderr[-2000:]
    assert by_domain.stdout == by_prefix.stdout


def test_another_prefix_moves_every_key_and_nothing_else() -> None:
    ours = _render("policies", SWARM_LABEL_PREFIX=PREFIX).stdout
    theirs = _render("policies", SWARM_LABEL_PREFIX="labels.example.net").stdout
    assert f"{PREFIX}/" not in theirs
    assert theirs.replace("labels.example.net/", f"{PREFIX}/") == ours


@pytest.mark.parametrize("name", sorted(RENDERS))
def test_a_render_with_no_prefix_refuses_and_writes_nothing(name: str) -> None:
    done = _render(name)
    assert done.returncode != 0
    assert done.stdout == ""
    assert UNSET in done.stderr


#: The lint renders (`make lint`, security.yml, the worker's pre-publish
#: checkov): no project, so offline (render.py OFFLINE_LABEL_PREFIX).
OFFLINE_RENDERS = {
    "policies": ["policies"],
    "tenant": ["tenant", "--tenant", "lint"],
    "job": ["job", "--tenant", "lint", "--profile", "browser", "--task", "t1", "--attempt", "a1",
            "--lease", "l1", "--generation", "1"],
}


@pytest.mark.parametrize("name", sorted(OFFLINE_RENDERS))
def test_an_offline_render_with_no_prefix_writes_the_placeholder_and_says_so(name: str) -> None:
    done = subprocess.run([sys.executable, str(RENDER), *OFFLINE_RENDERS[name]], capture_output=True,
                          text=True, env=_env(), timeout=120, check=False)
    assert done.returncode == 0, done.stderr
    assert TOKEN not in done.stdout
    assert "swarm.example.com/" in done.stdout
    warning = [line for line in done.stderr.splitlines() if "no label-key prefix is configured" in line]
    assert len(warning) == 1 and warning[0].startswith("# render: WARNING:"), done.stderr
    # A configured prefix wins offline too, and nothing is said.
    configured = subprocess.run([sys.executable, str(RENDER), *OFFLINE_RENDERS[name]], capture_output=True,
                                text=True, env=_env(SWARM_LABEL_PREFIX="labels.example.net"), timeout=120,
                                check=False)
    assert configured.returncode == 0, configured.stderr
    assert "swarm.example.com/" not in configured.stdout
    assert "labels.example.net/" in configured.stdout
    assert "no label-key prefix is configured" not in configured.stderr


def test_the_admission_policy_matches_the_workspace_label_the_render_writes() -> None:
    """The scope policy's CEL and the tenant render's label are one key."""
    policies = _render("policies", SWARM_LABEL_PREFIX=PREFIX).stdout
    tenant = _render("tenant", SWARM_LABEL_PREFIX=PREFIX).stdout
    assert f"'{PREFIX}/workspace' in namespaceObject.metadata.labels" in policies
    assert f"    {PREFIX}/workspace: w-1a2b3c\n" in tenant


def test_the_spec_verify_keys_configmap_carries_the_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = importlib.util.spec_from_file_location("swarm_k8s_render_label_prefix", RENDER)
    render = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(render)
    values = {"NAMESPACE": "swarm-tenant-team-a", "TENANT_ID": "team-a"}
    monkeypatch.setenv("SWARM_LABEL_PREFIX", PREFIX)
    assert f"    {PREFIX}/rationale: >-\n" in render.render_spec_verify_keys({}, values)
    monkeypatch.delenv("SWARM_LABEL_PREFIX")
    monkeypatch.delenv("SWARM_ORG_DOMAIN", raising=False)
    with pytest.raises(SystemExit):
        render.render_spec_verify_keys({}, values)


# --------------------------------------------------------------------------
# kubernetes/apply.sh fills terraform's six swarm-system renders
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("template", "output", "file"), TERRAFORM_RENDERS)
def test_apply_fills_the_token_in_every_terraform_render(template: str, output: str, file: str) -> None:
    """Read, then filled, then checked for an unrendered placeholder, in that order."""
    text = APPLY.read_text()
    read = text.index(f"output -raw {output} >")
    filled = text.index('fill_label_prefix "${', read)
    assert text[filled:text.index("\n", filled)].rstrip().endswith(f'/{file}"'), text[filled:filled + 120]
    checked = text.index("grep -Eo '__[A-Z0-9_]+__'", read)
    assert read < filled < checked


@pytest.mark.parametrize(("template", "output", "file"), TERRAFORM_RENDERS)
def test_the_fill_apply_runs_leaves_no_token(tmp_path: Path, template: str, output: str, file: str) -> None:
    target = tmp_path / file
    target.write_text((KUBERNETES / template).read_text())
    done = subprocess.run([sys.executable, str(LABEL_PREFIX_PY), "--fill", str(target)], capture_output=True,
                          text=True, env=_env(SWARM_ORG_DOMAIN=ORG_DOMAIN), check=False)
    assert done.returncode == 0, done.stderr
    filled = target.read_text()
    assert TOKEN not in filled
    assert _keys(filled, PREFIX) == FIXTURE["templates"].get(f"kubernetes/{template}", [])
    # Without a setting the file is refused and left as it was.
    target.write_text((KUBERNETES / template).read_text())
    done = subprocess.run([sys.executable, str(LABEL_PREFIX_PY), "--fill", str(target)], capture_output=True,
                          text=True, env=_env(), check=False)
    if TOKEN in target.read_text():
        assert done.returncode != 0 and UNSET in done.stderr
    assert target.read_text() == (KUBERNETES / template).read_text()


def test_apply_resolves_the_prefix_through_the_one_rule() -> None:
    text = APPLY.read_text()
    assert re.search(r'fill_label_prefix\(\) \{\n(.*\n)*?.*label_prefix\.py" --fill', text)


# --------------------------------------------------------------------------
# the readers: network parity, tenant registration, the workspace guard
# --------------------------------------------------------------------------


def test_network_parity_reads_the_annotations_under_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = importlib.util.spec_from_file_location("swarm_k8s_parity_label_prefix", KUBERNETES / "network_parity.py")
    parity = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(parity)
    monkeypatch.setenv("SWARM_LABEL_PREFIX", "labels.example.net")
    assert parity.source_annotation() == "labels.example.net/network-source"
    assert parity.api_cidr_annotation() == "labels.example.net/swarm-api-egress-cidr"
    assert [key for _, key in parity.network_annotations()] == [
        f"labels.example.net/{name}" for name in ("pod-cidr", "service-cidr", "cluster-dns-ip", "node-local-dns-ip")]


def test_register_tenant_reads_the_workspace_label_from_the_expectation() -> None:
    """A9 reads the key the guard wrote into the expectation from the deployment's
    tfvars (namespace_label_key), and A7 renders under that key's prefix."""
    text = (REPO / "scripts" / "register-tenant.sh").read_text()
    assert 'WS_NAMESPACE_LABEL="$(ws_expect namespace_label_key)"' in text
    assert 'export SWARM_LABEL_PREFIX="${WS_NAMESPACE_LABEL%/workspace}"' in text
    assert not re.search(r'WS_NAMESPACE_LABEL="swarm\.', text)


def test_the_guard_reads_the_workspace_label_from_the_expectation() -> None:
    text = (REPO / "scripts" / "lib" / "workspace-guard.sh").read_text()
    assert not re.search(r'^LABEL = "swarm\.', text, re.M)
    assert '.get("namespace_label_key")' in text
    assert 'label_key="$(deployment_label_prefix)/workspace"' in text


def _guard_manifest_reader() -> str:
    """The guard's manifest reader (GUARD_MANIFEST_PY), as the guard runs it."""
    text = (REPO / "scripts" / "lib" / "workspace-guard.sh").read_text()
    start = text.index("GUARD_MANIFEST_PY='\n") + len("GUARD_MANIFEST_PY='\n")
    return text[start:text.index("\n'\n", start)]


def _read_manifest(tmp_path: Path, manifest: str, key: str | None) -> object:
    """The reader's objects, with an expectation naming KEY (none: no key)."""
    expectation = tmp_path / "expect.json"
    expectation.write_text(json.dumps({} if key is None else {"namespace_label_key": key}))
    env = {**_env(), "GUARD_EXPECTATION": str(expectation)}
    done = subprocess.run([sys.executable, "-c", _guard_manifest_reader()], input=manifest,
                          capture_output=True, text=True, env=env, timeout=60, check=True)
    return json.loads(done.stdout)


def test_the_guard_reads_the_namespace_label_under_the_expectations_key(tmp_path: Path) -> None:
    namespace = "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: swarm-tenant-research\n  labels:\n" \
                "    {key}: w-1a2b3c\n"
    ours = namespace.format(key=f"{PREFIX}/workspace")
    (obj,) = _read_manifest(tmp_path, ours, f"{PREFIX}/workspace")
    assert obj["workspace_label"] == "w-1a2b3c"
    # Another deployment's prefix: today's key is then a stranger's label.
    (obj,) = _read_manifest(tmp_path, ours, "labels.example.net/workspace")
    assert obj["workspace_label"] is None
    (obj,) = _read_manifest(tmp_path, namespace.format(key="labels.example.net/workspace"),
                            "labels.example.net/workspace")
    assert obj["workspace_label"] == "w-1a2b3c"


def test_the_guard_reads_no_label_when_the_expectation_names_no_key(tmp_path: Path) -> None:
    """No key is guessed: the label is not read, so C7 admits no group or service Namespace."""
    namespace = f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: swarm-tenant-research\n  labels:\n" \
                f"    {PREFIX}/workspace: w-1a2b3c\n"
    (obj,) = _read_manifest(tmp_path, namespace, None)
    assert obj["workspace_label"] is None


def test_register_tenant_stops_after_a1_when_no_prefix_is_configured() -> None:
    """ws_load_names resolves the prefix before any write past A1, through the one rule,
    and refuses a configured prefix that is not the expectation's."""
    text = (REPO / "scripts" / "register-tenant.sh").read_text()
    load = text.index("ws_load_names() {")
    end = text.index("\n}\n", load)
    mismatch = text.index('|| die "the configured label-key prefix is not the one this deployment', load)
    resolved = text.index('python3 "${REPO_ROOT}/kubernetes/label_prefix.py" >/dev/null', load)
    assert mismatch < resolved < end
    assert "|| die \"the label-key prefix is not configured (above); nothing past A1 can run\"" in \
        text[resolved:resolved + 200]


# --------------------------------------------------------------------------
# every entry point that renders the token resolves the prefix one way
# --------------------------------------------------------------------------
#
# A deploy step that renders a template holding the token, or reads a prefixed
# key back, with neither setting in its environment refuses -- release and
# hotfix ran scripts/apply-tenant-network-policies.sh that way. Every script
# that runs one of the renderers or readers therefore resolves the prefix
# through common.sh require_label_prefix (the deployment's tfvars, a differing
# configured prefix refused), or is listed below with why it needs none.

RENDERERS = re.compile(r"render\.py|tenant_netpol\.py|network_parity\.py|label_prefix\.py")
#: Scripts that run a renderer but need no deployment prefix, and why.
NEEDS_NO_PREFIX = {
    # Touches no project: renders under the offline placeholder when nothing is set.
    "scripts/lib/validate-manifests.sh": 'export SWARM_LABEL_PREFIX="swarm.example.com"',
    # Asks render.py which namespace a tenant has; renders no template.
    "scripts/workspace-migrate-record.sh": "kubernetes/render.py\" identity --tenant",
}
#: The cluster sweeps: resolved once, at top level, before kubectl is located.
AT_TOP = ("kubernetes/apply.sh", "scripts/apply-tenant-network-policies.sh",
          "scripts/lib/check-cluster-network-parity.sh")


def _code_lines(path: Path) -> list[str]:
    return [line for line in path.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]


def _entry_points() -> dict[str, Path]:
    found = {}
    for path in sorted([*REPO.glob("scripts/**/*.sh"), *KUBERNETES.glob("*.sh")]):
        if any(RENDERERS.search(line) for line in _code_lines(path)):
            found[str(path.relative_to(REPO))] = path
    return found


def test_every_script_that_renders_the_token_resolves_the_prefix_through_the_shared_helper() -> None:
    found = _entry_points()
    # The control: the scan finds the deploy's own entry points.
    assert {*AT_TOP, "scripts/register-tenant.sh"} <= set(found), sorted(found)
    # The rule's home: it defines both functions, and is not an entry point.
    common = found.pop("scripts/lib/common.sh")
    assert "require_label_prefix() {" in common.read_text()
    for name, path in found.items():
        lines = _code_lines(path)
        if name in NEEDS_NO_PREFIX:
            assert any(NEEDS_NO_PREFIX[name] in line for line in lines), name
            continue
        calls = [line for line in lines if re.search(r"\brequire_label_prefix\b", line)]
        assert calls, f"{name} renders the label-key token without common.sh require_label_prefix"
        assert not any("resolved_label_prefix" in line for line in lines), (
            f"{name} calls resolved_label_prefix itself: a second copy of the one rule")
        if name in AT_TOP:
            assert len(calls) == 1 and not calls[0].startswith((" ", "\t")), (name, calls)
            text = path.read_text()
            assert text.index(calls[0]) < text.index('KUBECTL="$(kubectl_bin)"'), name


def test_an_unreadable_config_folder_is_reported_before_the_refusal(tmp_path: Path) -> None:
    """resolved_label_prefix prints the tfvars fetch's own error, then the refusal."""
    config = tmp_path / "config"
    (config / "dev").mkdir(parents=True, mode=0o700)
    config.chmod(0o700)
    tfvars = config / "dev" / "dev.tfvars"
    tfvars.write_text(f'allowed_domains = ["{ORG_DOMAIN}"]\n')
    tfvars.chmod(0o666)  # world-writable: config_fetch refuses to read it
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    env = {k: v for k, v in os.environ.items() if k not in ("SWARM_LABEL_PREFIX", "SWARM_ORG_DOMAIN")}
    env.update(SWARM_NO_PROJECT="1", PROJECT_ID="example-project", ENVIRONMENT="dev", NO_COLOR="1",
               SWARM_DEPLOY_CONFIG_DIR=str(config), XDG_CACHE_HOME=str(cache),
               SWARM_ENV_FILE=str(tmp_path / "no-such.env"), PYTHON_BIN=sys.executable)
    done = subprocess.run(
        ["bash", "-c", 'source scripts/lib/common.sh; rc=0; resolved_label_prefix || rc=$?; echo "rc=${rc}"'],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    assert "rc=1" in done.stdout, done.stdout + done.stderr
    assert "group- or world-writable" in done.stderr, done.stderr
    assert UNSET in done.stderr
    assert done.stderr.index("world-writable") < done.stderr.index(UNSET)
    assert "repository variable" not in done.stderr.lower()
