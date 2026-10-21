"""swarm-images' KEEP prefixes are the channel names and the promotion mark's.

Artifact Registry cleanup policies match tags by PREFIX (#1886,
REGISTRY-CLEANUP). `keep-channel-tags` keeps every tag that starts with a
channel name, with no age limit, because `:dev` and `:prod` are the live
images. `keep-promoted-30d` and `keep-owner-pinned` keep the promotion marks
push-images.sh writes, `promotion_mark_prefix <channel>` + `<tag>`
(scripts/lib/common.sh). Each prefix is a string in
terraform/modules/artifact_registry/main.tf and again in the shell, and the two
drift silently: a renamed mark would match no KEEP and every promoted image
would fall to `delete-tagged-stale` after 14 days, and a mark that started with
a channel name again would be kept forever by `keep-channel-tags`.

The channels are the environments that have a tfvars file
(terraform/environments/<env>/<env>.tfvars): push-images.sh promotes to
`--channel`, which defaults to ENVIRONMENT.

A small regex rather than python-hcl2, which is not a dependency. Offline:
source text and one `bash -c` that sources common.sh.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
MODULE_TF = REPO / "terraform" / "modules" / "artifact_registry" / "main.tf"
COMMON = REPO / "scripts" / "lib" / "common.sh"
ENVIRONMENTS = REPO / "terraform" / "environments"


def _policies() -> dict[str, str]:
    """Each `cleanup_policies { ... }` block's body, keyed by its id."""
    text = MODULE_TF.read_text()
    policies: dict[str, str] = {}
    for start in re.finditer(r"^\s*cleanup_policies\s*\{", text, re.MULTILINE):
        depth, i = 1, start.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        body = text[start.end() : i - 1]
        ident = re.search(r'^\s*id\s*=\s*"([^"]+)"', body, re.MULTILINE)
        assert ident, f"a cleanup_policies block in {MODULE_TF} has no id"
        policies[ident.group(1)] = body
    return policies


def _list(body: str, attribute: str) -> list[str]:
    match = re.search(rf"^\s*{attribute}\s*=\s*\[(?P<items>[^\]]*)\]", body, re.MULTILINE)
    assert match, f"the policy sets no {attribute}:\n{body}"
    return re.findall(r'"([^"]*)"', match.group("items"))


def _channels() -> list[str]:
    return sorted(d.name for d in ENVIRONMENTS.iterdir() if (d / f"{d.name}.tfvars").is_file())


def _mark_prefixes(channels: list[str]) -> list[str]:
    assert shutil.which("bash"), "common.sh needs bash"
    body = "".join(f'printf "%s\\n" "$(promotion_mark_prefix {c})"\n' for c in channels)
    proc = subprocess.run(
        ["bash", "-c", f'source "{COMMON}"\n{body}', "registry-cleanup-prefixes"],
        cwd=REPO,
        env={**os.environ, "SWARM_ENV_FILE": str(REPO / "no-such.env"), "NO_COLOR": "1"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc.stdout.splitlines()


def test_the_channels_are_dev_and_prod() -> None:
    # MUTATION: add terraform/environments/staging/staging.tfvars and this
    # fails, which is the point: a new channel needs its KEEP prefix first.
    assert _channels() == ["dev", "prod"]


def test_keep_channel_tags_keeps_exactly_the_channel_names() -> None:
    # MUTATION: drop "prod" from keep-channel-tags' tag_prefixes and this fails.
    assert sorted(_list(_policies()["keep-channel-tags"], "tag_prefixes")) == _channels()


def test_the_promoted_keep_prefix_is_what_every_channel_mark_starts_with() -> None:
    channels = _channels()
    marks = _mark_prefixes(channels)
    assert len(marks) == len(channels), marks
    # The part of `promoted-<channel>-` every channel shares: "promoted-".
    shared = os.path.commonprefix(marks)
    assert shared and all(m == f"{shared}{c}-" for m, c in zip(marks, channels, strict=True)), marks
    for policy in ("keep-promoted-30d", "keep-owner-pinned"):
        # MUTATION: change promotion_mark_prefix in common.sh to print
        # `release-<channel>-` (or a policy's tag_prefixes to ["promoted"]) and
        # this fails.
        assert _list(_policies()[policy], "tag_prefixes") == [shared], policy


def test_no_mark_is_kept_forever_by_the_channel_prefix() -> None:
    channels = _channels()
    # MUTATION: make promotion_mark_prefix print `<channel>-promoted-` again
    # and this fails: keep-channel-tags would keep every mark with no age.
    for mark in _mark_prefixes(channels):
        assert not any(mark.startswith(c) for c in _list(_policies()["keep-channel-tags"], "tag_prefixes")), mark
