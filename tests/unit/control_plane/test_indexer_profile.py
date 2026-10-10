"""The `indexer` runner profile runs agent-runtime-indexer (contract request 48,
accepted by the owner 2026-10-05, #625).

#625 moved the repository index's toolchain out of agent-runtime-base into
agent-runtime-indexer. Until a profile named that image, index runs were
submitted as `claude-code`, ran on the base, recorded the extractor as "not
installed in this image" and wrote no graph. The owner's decision: ONE
profile, `indexer`, that is claude-code in every field but its name and its
image. These hold that:

  * the frozen catalogue's `indexer` is claude-code with only `name` and
    `image` changed, and it is the only profile that runs the indexer image;
  * swarm-api submits index runs as `indexer`, by name, and a task carries no
    image a caller could set (invariant 10);
  * the Terraform mirror and the model table carry it as they carry
    claude-code (tests/terraform/catalogue.tftest.hcl compares the rest);
  * it runs the claude_code runner, so the worker and the API name its agent
    streams as claude-code's;
  * request 48 records the acceptance with the phrase
    scripts/lib/check-frozen-contract.sh looks for.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from swarm_common.profiles import RUNNER_PROFILES

from swarm_api import repoindex
from swarm_api.schemas import TaskCreate

REPO = Path(__file__).resolve().parents[3]
LOCALS = REPO / "terraform" / "infra" / "locals.tf"
REQUESTS = REPO / "docs" / "contract-change-requests.md"
SHA = "b" * 40


def test_the_indexer_profile_is_claude_code_on_the_indexer_image():
    assert "indexer" in RUNNER_PROFILES, "contract request 48's profile is missing"
    indexer = RUNNER_PROFILES["indexer"]
    claude = RUNNER_PROFILES["claude-code"]
    assert indexer.name == "indexer"
    assert indexer.image == "agent-runtime-indexer"
    differ = sorted(
        f.name for f in dataclasses.fields(indexer)
        if getattr(indexer, f.name) != getattr(claude, f.name)
    )
    # Contract request 53 (applied 2026-10-08) moved claude-code to GKE
    # Autopilot, and request 63 (owner, 2026-10-10, #939's canary) moved
    # indexer after it: the two share a backend again.
    assert differ == ["image", "name"], (
        f"the owner's decision was claude-code with only its name and image changed; {differ} differ"
    )
    assert indexer.backend.value == "GKE_AUTOPILOT" and claude.backend.value == "GKE_AUTOPILOT"


def test_only_the_indexer_profile_runs_the_indexer_image():
    runs = {name for name, p in RUNNER_PROFILES.items() if p.image == "agent-runtime-indexer"}
    assert runs == {"indexer"}
    for name, profile in RUNNER_PROFILES.items():
        assert (REPO / "images" / profile.image / "Dockerfile").is_file(), (name, profile.image)


def test_index_runs_are_submitted_as_the_indexer_profile_by_name():
    assert repoindex.INDEXER_PROFILE == "indexer"
    record = {
        "tenant_id": "eng",
        "repo_id": "gh-saga-xyz-widgets",
        "owner": "saga-xyz",
        "repo": "widgets",
        "default_branch": "main",
        "repository_url": "https://github.com/saga-xyz/widgets",
    }
    task = repoindex.indexer_task(record, SHA)
    assert task.runner_profile == "indexer"
    # Invariant 10: the task names a profile; the image is the catalogue's.
    assert not {"image", "command", "resources"} & set(TaskCreate.model_fields)
    assert set(task.input) == {"prompt"}


def test_the_terraform_mirror_carries_the_indexer_as_it_carries_claude_code():
    text = LOCALS.read_text(encoding="utf-8")
    block = re.search(r'^    "indexer" = \{\n(.*?)^    \}\n', text, re.M | re.S)
    assert block, 'terraform/infra/locals.tf runner_profiles has no "indexer" entry'
    body = block.group(1)
    assert re.search(r'^\s*image\s*=\s*"agent-runtime-indexer"$', body, re.M), body
    assert re.search(r'^\s*provider\s*=\s*"anthropic"$', body, re.M), body
    assert re.search(r"^\s*timeout_seconds\s*=\s*7200$", body, re.M), body
    # The model table is Terraform-only, and an index run ran claude-code's.
    models = dict(re.findall(r'^\s*"([a-z-]+)"\s*=\s*"(claude-[a-z0-9-]+)"$', text, re.M))
    assert models.get("indexer") == models["claude-code"], models


def test_the_indexer_writes_claude_codes_agent_streams():
    from agent_worker.runners.streams import cli_agent_spec
    from swarm_api.agent_streams import AGENT_STREAM_FILES

    assert cli_agent_spec("indexer") is cli_agent_spec("claude-code")
    assert AGENT_STREAM_FILES["indexer"] == AGENT_STREAM_FILES["claude-code"]


def test_request_48_records_the_owners_acceptance():
    text = REQUESTS.read_text(encoding="utf-8")
    start = text.index("## 48. ")
    end = text.find("\n## 49. ", start)
    section = text[start:end if end != -1 else len(text)]
    # check-frozen-contract.sh passes a diff under apps/common/swarm_common/
    # only beside an added line holding exactly this phrase (grep -iF).
    assert "accepted by the owner" in section.lower()
    assert "**Status:** open" not in section
