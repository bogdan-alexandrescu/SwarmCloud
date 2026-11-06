"""Every refusal of §2.3, each on a crafted archive built here."""

from __future__ import annotations

import gzip
import json
import secrets
import tarfile

import pytest
from intake_archives import device, fifo, file, folder, hardlink, symlink, tar_gz, zip_archive, zip_raw_name
from plugin_intake import limits
from plugin_intake.intake import run_intake
from plugin_intake.refusals import REFUSALS

MANIFEST = json.dumps({"name": "demo", "version": "1.0.0"})


def intake(packed: bytes, **kwargs):
    return run_intake(packed, intake_id=secrets.token_hex(16), scope="platform", tenant_id=None, **kwargs)


def refused(packed: bytes, **kwargs) -> dict:
    report, runnable = intake(packed, **kwargs)
    assert runnable is None, report
    assert report["outcome"] == "refused"
    return report["refusal"]


def plugin(*members):
    return [folder("demo-abc123/"), file("demo-abc123/.claude-plugin/plugin.json", MANIFEST), *members]


def test_the_caps_are_the_owners():
    assert limits.MAX_PACKED_BYTES == 100 * 1024 * 1024
    assert limits.MAX_UNPACKED_BYTES == 500 * 1024 * 1024
    assert limits.MAX_FILES == 20_000


def test_every_refusal_is_a_fixed_sentence():
    for code, sentence in REFUSALS.items():
        assert code.isidentifier() and sentence.endswith(".")
        assert "{" not in sentence and "%" not in sentence


def test_over_the_packed_cap(monkeypatch):
    packed = tar_gz(plugin(file("demo-abc123/a.txt", secrets.token_bytes(4096))))
    monkeypatch.setattr(limits, "MAX_PACKED_BYTES", len(packed) - 1)
    assert refused(packed)["code"] == "archive_too_large"


@pytest.mark.parametrize("build", [tar_gz, zip_archive])
def test_over_the_unpacked_cap(monkeypatch, build):
    monkeypatch.setattr(limits, "MAX_UNPACKED_BYTES", 10_000)
    packed = build(plugin(file("demo-abc123/a.bin", secrets.token_bytes(6_000)), file("demo-abc123/b.bin", secrets.token_bytes(6_000))))
    assert refused(packed)["code"] == "unpacked_too_large"


@pytest.mark.parametrize("build", [tar_gz, zip_archive])
def test_over_the_file_count(monkeypatch, build):
    monkeypatch.setattr(limits, "MAX_FILES", 5)
    members = plugin(*[file(f"demo-abc123/f{i}.txt", "x") for i in range(6)])
    assert refused(build(members))["code"] == "too_many_files"


def test_hard_links_count_against_the_unpacked_cap(monkeypatch):
    monkeypatch.setattr(limits, "MAX_UNPACKED_BYTES", 10_000)
    members = plugin(file("demo-abc123/big.bin", secrets.token_bytes(4_000)))
    members += [hardlink(f"demo-abc123/copy{i}", "demo-abc123/big.bin") for i in range(3)]
    assert refused(tar_gz(members))["code"] == "unpacked_too_large"


@pytest.mark.parametrize("build", [tar_gz, zip_archive])
def test_an_expansion_past_the_ratio(build):
    # 4 MiB of zeros packs to a few kilobytes: far past 100:1.
    packed = build(plugin(file("demo-abc123/zeros.bin", bytes(4 * 1024 * 1024))))
    assert refused(packed)["code"] == "expansion_ratio"


def test_a_gzip_bomb_is_stopped_while_decompressing(monkeypatch):
    # A stream that is not even a tar: the counter stops it before the tar parser sees the end.
    monkeypatch.setattr(limits, "MAX_UNPACKED_BYTES", 1024)
    monkeypatch.setattr(limits, "TAR_OVERHEAD_BYTES", 1024)
    monkeypatch.setattr(limits, "RATIO_FLOOR_BYTES", 10**12)
    packed = gzip.compress(bytes(1024 * 1024))
    assert refused(packed)["code"] == "unpacked_too_large"


@pytest.mark.parametrize("build", [tar_gz, zip_archive])
def test_an_absolute_path(build):
    assert refused(build(plugin(file("/etc/cron.d/evil", "x"))))["code"] == "entry_path_absolute"


@pytest.mark.parametrize("build", [tar_gz, zip_archive])
def test_a_parent_segment(build):
    assert refused(build(plugin(file("demo-abc123/../../evil.sh", "x"))))["code"] == "entry_path_parent"


@pytest.mark.parametrize("name", ["../../../../tmp/evil.sh", "..\\..\\evil.bat", "/abs/evil", "C:/Windows/evil.dll"])
def test_zip_slip_is_refused(name):
    code = refused(zip_raw_name(name))["code"]
    assert code in {"entry_path_parent", "entry_path_invalid", "entry_path_absolute"}


def test_a_backslash_path():
    assert refused(tar_gz(plugin(file("demo-abc123\\evil", "x"))))["code"] == "entry_path_invalid"


def test_a_path_too_deep():
    deep = "demo-abc123/" + "/".join(["d"] * 33) + "/f"
    assert refused(tar_gz(plugin(file(deep, "x"))))["code"] == "entry_path_too_deep"


def test_a_segment_too_long():
    assert refused(tar_gz(plugin(file("demo-abc123/" + "a" * 256, "x"))))["code"] == "entry_path_segment_too_long"


def test_duplicates_after_case_folding_and_nfc():
    assert refused(tar_gz(plugin(file("demo-abc123/README.md", "a"), file("demo-abc123/readme.md", "b"))))["code"] == "entry_duplicate"
    nfc, nfd = "demo-abc123/caf\u00e9", "demo-abc123/cafe\u0301"
    assert refused(tar_gz(plugin(file(nfc, "a"), file(nfd, "b"))))["code"] == "entry_duplicate"


@pytest.mark.parametrize("build", [tar_gz, zip_archive])
def test_a_symlink_that_leaves_the_root(build):
    assert refused(build(plugin(symlink("demo-abc123/passwd", "../../../etc/passwd"))))["code"] == "link_escapes_root"


@pytest.mark.parametrize("build", [tar_gz, zip_archive])
def test_an_absolute_symlink(build):
    assert refused(build(plugin(symlink("demo-abc123/passwd", "/etc/passwd"))))["code"] == "link_escapes_root"


def test_a_symlink_that_leaves_the_requested_subdir():
    members = plugin(file("demo-abc123/secret.txt", "x"), file("demo-abc123/plugins/one/.claude-plugin/plugin.json", MANIFEST))
    members.append(symlink("demo-abc123/plugins/one/leak", "../../secret.txt"))
    assert refused(tar_gz(members), subdir="plugins/one")["code"] == "link_escapes_root"


def test_a_symlink_to_a_symlink_and_a_dangling_one():
    members = plugin(file("demo-abc123/a.txt", "x"), symlink("demo-abc123/b", "a.txt"), symlink("demo-abc123/c", "b"))
    assert refused(tar_gz(members))["code"] == "link_to_link"
    assert refused(tar_gz(plugin(symlink("demo-abc123/d", "nothing"))))["code"] == "link_target_missing"


def test_an_entry_through_a_symlink():
    members = plugin(folder("demo-abc123/real/"), symlink("demo-abc123/alias", "real"), file("demo-abc123/alias/x", "x"))
    assert refused(tar_gz(members))["code"] == "entry_under_link"


def test_a_hard_link_that_leaves_the_root():
    members = plugin(file("demo-abc123/outside.txt", "x"), file("demo-abc123/plugins/one/.claude-plugin/plugin.json", MANIFEST))
    members.append(hardlink("demo-abc123/plugins/one/leak", "demo-abc123/outside.txt"))
    assert refused(tar_gz(members), subdir="plugins/one")["code"] == "hardlink_escapes_root"
    assert refused(tar_gz(plugin(hardlink("demo-abc123/x", "../../etc/shadow"))))["code"] == "entry_path_parent"
    assert refused(tar_gz(plugin(hardlink("demo-abc123/x", "demo-abc123/missing"))))["code"] == "hardlink_escapes_root"


@pytest.mark.parametrize("member", [device("demo-abc123/null"), fifo("demo-abc123/pipe")])
def test_device_files_and_fifos(member):
    assert refused(tar_gz(plugin(member)))["code"] == "entry_special_file"


def test_a_zip_device_file():
    assert refused(zip_archive(plugin(device("demo-abc123/null"))))["code"] == "entry_special_file"


@pytest.mark.parametrize("build", [tar_gz, zip_archive])
@pytest.mark.parametrize("mode", [0o4755, 0o2755])
def test_setuid_and_setgid(build, mode):
    assert refused(build(plugin(file("demo-abc123/run", "x", mode=mode))))["code"] == "entry_setuid"


def test_dependencies_without_node_modules():
    package = json.dumps({"name": "x", "dependencies": {"left-pad": "1.3.0"}})
    assert refused(tar_gz(plugin(file("demo-abc123/package.json", package))))["code"] == "dependencies_not_shipped"
    nested = plugin(file("demo-abc123/server/package.json", package))
    assert refused(tar_gz(nested))["code"] == "dependencies_not_shipped"


def test_dependencies_with_node_modules_pass():
    package = json.dumps({"name": "x", "dependencies": {"left-pad": "1.3.0"}})
    members = plugin(file("demo-abc123/package.json", package), file("demo-abc123/node_modules/left-pad/index.js", "module.exports = 1;"))
    report, runnable = intake(tar_gz(members))
    assert report["outcome"] == "pinned" and runnable


def test_a_package_json_that_is_not_an_object():
    assert refused(tar_gz(plugin(file("demo-abc123/package.json", "[1, 2]"))))["code"] == "package_json_invalid"


def test_not_an_archive_and_a_corrupt_one():
    assert refused(b"#!/bin/sh\necho hi\n")["code"] == "archive_format_unknown"
    good = tar_gz(plugin(file("demo-abc123/a", "x")))
    assert refused(good[: len(good) // 2])["code"] == "archive_corrupt"


def test_no_manifest_and_no_overlay():
    assert refused(tar_gz([file("demo-abc123/belay.mjs", "x")]))["code"] == "manifest_missing"


def test_a_manifest_with_an_unknown_component():
    manifest = json.dumps({"name": "demo", "lspServers": {"x": {"command": "x"}}})
    members = [file("demo-abc123/.claude-plugin/plugin.json", manifest)]
    assert refused(tar_gz(members))["code"] == "manifest_component_unknown"


def test_a_manifest_path_outside_the_root():
    manifest = json.dumps({"name": "demo", "commands": "../../etc"})
    assert refused(tar_gz([file("demo-abc123/.claude-plugin/plugin.json", manifest)]))["code"] == "manifest_path_outside_root"


def test_a_hook_naming_a_missing_file_and_an_unknown_event():
    hooks = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "node ${CLAUDE_PLUGIN_ROOT}/gone.mjs"}]}]}}
    assert refused(tar_gz(plugin(file("demo-abc123/hooks/hooks.json", json.dumps(hooks)))))["code"] == "hook_path_missing"
    hooks = {"hooks": {"BeforeEverything": [{"hooks": [{"type": "command", "command": "true"}]}]}}
    assert refused(tar_gz(plugin(file("demo-abc123/hooks/hooks.json", json.dumps(hooks)))))["code"] == "hook_unknown"


def test_a_refusal_never_carries_the_archives_text():
    marker = "MARK" + secrets.token_hex(8)
    report, _ = intake(tar_gz(plugin(file(f"demo-abc123/{marker}/../../../x", "x"))))
    assert report["refusal"]["code"] == "entry_path_parent"
    assert isinstance(report["refusal"]["entry_index"], int)
    assert marker not in json.dumps(report)


def test_a_contiguous_file_is_a_regular_file():
    members = plugin(file("demo-abc123/cont", "x"))
    members[-1].kind = tarfile.CONTTYPE
    assert intake(tar_gz(members))[0]["outcome"] == "pinned"
