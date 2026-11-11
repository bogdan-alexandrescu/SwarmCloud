"""The managed plugins' bundle job holds exactly the grants the design names.

docs/design/managed-plugins.md §1.4.2 (the owner's change of 11 November 2026):
the bundle build is one execution of the Cloud Run job swarm-plugin-bundle,
under the builder account, and no Cloud Build. These hold, across every file of
terraform/infra:

* the builder (swarm-plugin-bundler) is the member of exactly five grants, all
  in plugin-bundles.tf: the sources prefix and the one Bun object by condition,
  the platform repository and the npm mirror read, and a write to the bundle
  repository alone; no project role, no Docker Hub mirror, no log writer;
* swarm-api's only grant in plugin-bundles.tf is swarmPluginBundleRunner on the
  job's own IAM policy;
* nothing in plugin-bundles.tf or terraform/bootstrap/plugin_bundle_runner.tf
  names Cloud Build.

These read the Terraform as text: no provider, no plan, no credentials.
tests/terraform/plugin_bundles.tftest.hcl and plugin_bundle_runner.tftest.hcl
plan the same resources.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
INFRA = ROOT / "terraform" / "infra"
BUNDLES_TF = INFRA / "plugin-bundles.tf"
RUNNER_TF = ROOT / "terraform" / "bootstrap" / "plugin_bundle_runner.tf"

RESOURCE = re.compile(r'^resource\s+"([^"]+)"\s+"([^"]+)"\s*\{(.*?)^\}', re.MULTILINE | re.DOTALL)


def _code(text: str) -> str:
    """The text with every `#` comment removed."""
    return "\n".join(line.split("#", 1)[0] if not line.lstrip().startswith("#") else "" for line in text.splitlines())


def _resources(path: Path) -> list[tuple[str, str, str]]:
    return [(m.group(1), m.group(2), m.group(3)) for m in RESOURCE.finditer(_code(path.read_text()))]


def test_the_builder_holds_exactly_five_grants() -> None:
    grants = []
    for path in sorted(INFRA.glob("*.tf")):
        for kind, name, body in _resources(path):
            if kind.endswith(("_iam_member", "_iam_binding")) and re.search(r"plugin_bundler_(member|email)|plugin-bundler", body):
                grants.append((path.name, kind, name, body))
    assert {(path, kind, name) for path, kind, name, _ in grants} == {
        ("plugin-bundles.tf", "google_storage_bucket_iam_member", "plugin_bundler_sources"),
        ("plugin-bundles.tf", "google_storage_bucket_iam_member", "plugin_bundler_tool_mirror"),
        ("plugin-bundles.tf", "google_artifact_registry_repository_iam_member", "plugin_bundler_release_images"),
        ("plugin-bundles.tf", "google_artifact_registry_repository_iam_member", "plugin_bundler_npm"),
        ("plugin-bundles.tf", "google_artifact_registry_repository_iam_member", "plugin_bundler_push"),
    }
    by_name = {name: body for _, _, name, body in grants}
    for bucket_grant in ("plugin_bundler_sources", "plugin_bundler_tool_mirror"):
        assert "condition {" in by_name[bucket_grant], f"{bucket_grant} is held by condition"
        assert 'role   = "roles/storage.objectViewer"' in by_name[bucket_grant]
    assert "roles/artifactregistry.writer" in by_name["plugin_bundler_push"]
    assert "plugin_bundles[0]" in by_name["plugin_bundler_push"]
    for read in ("plugin_bundler_release_images", "plugin_bundler_npm"):
        assert "roles/artifactregistry.reader" in by_name[read]


def test_the_builder_holds_no_project_role_or_docker_hub_mirror() -> None:
    code = _code(BUNDLES_TF.read_text())
    assert "google_project_iam_member" not in code.replace("google_logging_project_sink", "")
    assert "roles/logging.logWriter" not in code
    assert "mirror_dockerhub" not in code and "docker-hub" not in code.lower()


def test_swarm_api_may_only_run_the_job() -> None:
    api_grants = [
        (kind, name, body)
        for kind, name, body in _resources(BUNDLES_TF)
        if kind.endswith(("_iam_member", "_iam_binding")) and '"swarm-api"' in body
    ]
    assert [(kind, name) for kind, name, _ in api_grants] == [("google_cloud_run_v2_job_iam_member", "plugin_bundle_api_runs")]
    body = api_grants[0][2]
    assert "module.plugins_role_ids.names.plugin_bundle_runner" in body
    assert "google_cloud_run_v2_job.plugin_bundle[0].name" in body


def test_nothing_names_cloud_build() -> None:
    for path in (BUNDLES_TF, RUNNER_TF):
        code = _code(path.read_text()).lower()
        assert "cloudbuild" not in code, path
        assert "cloud build" not in code, path
