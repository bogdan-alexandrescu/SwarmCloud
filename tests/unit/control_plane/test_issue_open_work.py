"""The open-work read an issue run's planner is shown (#454).

#454: the planner "reads the issue and all open issues and open PRs in the
repository, to avoid conflicts and overlaps". `forge.GitHubIssues.open_work`
lists them with the run's tenant's token, and `forge.read_open_work` masks
what it stores. What these tests hold:

  * pull requests are not open issues, and the planned issue is not its own
    overlap;
  * every cap is honoured and recorded, so a cut list says it is cut;
  * a 401/403/404/410 anywhere refuses (owner decision: "a forge 403 or 404
    fails the planner"); any other failure of a LIST refuses as read_failed;
    any other failure of one pull request's FILES leaves it `files: None`;
  * the token goes to api.github.com only, and into nothing stored or raised;
  * the planner's prompt stays under its byte limit however big the snapshot.

No credentials, no network: GitHub is a fake transport under the real client.
"""

from __future__ import annotations

import pytest

from swarm_api import forge, issueruns
from swarm_api.validation import parse_issue_ref
from swarm_common.models import Tenant, utcnow

from . import forge_fakes
from .forge_fakes import GitHub, issue, pull

REF = parse_issue_ref("saga-xyz/widgets#42")
TOKEN = forge_fakes.make_token()


def _read(github: GitHub, **client) -> forge.OpenWork:
    return forge.GitHubIssues(send=github, **client).open_work(REF, TOKEN)


def _tenant(tenant_id: str = "eng") -> Tenant:
    return Tenant(tenant_id=tenant_id, kind="group", principal="eng@saga.xyz", created_at=utcnow())


# --------------------------------------------------------------------------
# what is listed
# --------------------------------------------------------------------------

def test_pull_requests_and_the_planned_issue_are_not_listed_as_open_issues():
    github = GitHub(
        issues=[issue(42), issue(7), {**issue(9), "pull_request": {"url": "x"}}, issue(3)],
        pulls=[pull(9)],
    )
    work = _read(github)
    assert [i.number for i in work.issues] == [7, 3]
    assert [p.number for p in work.pull_requests] == [9]
    assert work.issues_truncated is False and work.pull_requests_truncated is False


def test_every_read_is_a_get_to_api_github_com_with_the_tenants_token():
    github = GitHub(issues=[issue(7)], pulls=[pull(9)], files={9: ["a.py"]})
    _read(github)
    assert github.paths() == [
        "/repos/saga-xyz/widgets/issues",
        "/repos/saga-xyz/widgets/pulls",
        "/repos/saga-xyz/widgets/pulls/9/files",
    ]
    for url, headers in github.calls:
        assert url.startswith("https://api.github.com/")
        assert "state=open" in url or url.split("?")[0].endswith("/files")
        assert headers["Authorization"] == f"Bearer {TOKEN}"


def test_the_issue_cap_is_honoured_and_recorded():
    github = GitHub(issues=[issue(n) for n in range(1, 160)])
    work = _read(github)
    assert len(work.issues) == forge.MAX_OPEN_ISSUES
    assert work.issues_truncated is True


def test_issues_hidden_behind_pages_of_pull_requests_are_cut_and_say_so():
    entries = [{**issue(n), "pull_request": {}} for n in range(1, 400)]
    github = GitHub(issues=entries)
    work = _read(github)
    assert work.issues == ()
    assert work.issues_truncated is True
    assert github.paths().count("/repos/saga-xyz/widgets/issues") == forge.MAX_ISSUE_PAGES


def test_a_short_last_page_is_not_a_cut():
    github = GitHub(issues=[issue(n) for n in range(1, 60)])
    work = _read(github)
    assert len(work.issues) == 58  # #42 is the planned issue
    assert work.issues_truncated is False


def test_the_pull_request_cap_is_honoured_and_recorded():
    github = GitHub(pulls=[pull(n) for n in range(1, 80)])
    work = _read(github)
    assert len(work.pull_requests) == forge.MAX_OPEN_PULLS
    assert work.pull_requests_truncated is True


def test_a_pull_requests_files_are_paged_and_capped():
    github = GitHub(pulls=[pull(9)], files={9: [f"src/f{n}.py" for n in range(150)]})
    work = _read(github)
    files = work.pull_requests[0].files
    assert files is not None and len(files) == forge.MAX_PR_FILES
    assert work.pull_requests[0].files_truncated is True
    assert github.paths().count("/repos/saga-xyz/widgets/pulls/9/files") == 2


def test_files_are_read_for_the_first_pull_requests_only():
    github = GitHub(pulls=[pull(n) for n in range(1, 41)], files={1: ["a.py"]})
    work = _read(github)
    with_files = [p for p in work.pull_requests if p.files is not None]
    assert len(with_files) == forge.MAX_PULLS_WITH_FILES
    assert work.pull_requests[-1].files is None


def test_the_total_file_budget_is_shared_across_pull_requests():
    many = [f"f{n}" for n in range(100)]
    github = GitHub(pulls=[pull(n) for n in range(1, 21)], files={n: many for n in range(1, 21)})
    work = _read(github)
    total = sum(len(p.files or ()) for p in work.pull_requests)
    assert total == forge.MAX_TOTAL_PR_FILES
    assert work.pull_requests[-1].files is None


def test_past_the_time_budget_no_further_files_are_read():
    ticks = iter(range(0, 1000, 10))  # started at 0; each check is 10s later
    github = GitHub(pulls=[pull(n) for n in range(1, 6)], files={n: ["a"] for n in range(1, 6)})
    work = _read(github, clock=lambda: next(ticks), budget_seconds=30)
    read = [p.number for p in work.pull_requests if p.files is not None]
    assert read == [1, 2]
    assert [p.number for p in work.pull_requests] == [1, 2, 3, 4, 5]


# --------------------------------------------------------------------------
# what a failure does
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "suffix,status,error",
    [
        ("/issues", 403, forge.IssueNoAccess),
        ("/issues", 401, forge.IssueNoAccess),
        ("/pulls", 404, forge.IssueNotFound),
        ("/issues", 410, forge.IssueNotFound),
        ("/pulls/9/files", 403, forge.IssueNoAccess),
        ("/pulls/9/files", 404, forge.IssueNotFound),
        ("/issues", 500, forge.IssueReadFailed),
        ("/pulls", 429, forge.IssueReadFailed),
        ("/pulls", 301, forge.IssueReadFailed),
    ],
)
def test_each_failure_has_its_code(suffix, status, error):
    github = GitHub(issues=[issue(7)], pulls=[pull(9)], files={9: ["a"]}, status={suffix: status})
    with pytest.raises(error) as raised:
        _read(github)
    assert TOKEN not in raised.value.message


def test_a_list_that_is_not_a_list_is_read_failed():
    class NotAList(GitHub):
        def __call__(self, url, headers, timeout):
            self.calls.append((url, headers))
            return 200, b'{"message": "rate limited"}'

    with pytest.raises(forge.IssueReadFailed, match="not a list"):
        _read(NotAList())


def test_a_files_read_failing_otherwise_leaves_that_pull_request_unread():
    github = GitHub(pulls=[pull(9), pull(10)], files={10: ["b.py"]}, status={"/pulls/9/files": 502})
    work = _read(github)
    assert work.pull_requests[0].files is None
    assert work.pull_requests[1].files == ("b.py",)


def test_a_transport_failure_carrying_the_token_is_read_failed_without_it():
    github = GitHub(raises=RuntimeError(f"connect failed: Authorization: Bearer {TOKEN}"))
    with pytest.raises(forge.IssueReadFailed) as raised:
        _read(github)
    assert TOKEN not in raised.value.message
    assert "RuntimeError" in raised.value.message
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


# --------------------------------------------------------------------------
# what is stored
# --------------------------------------------------------------------------

def test_the_stored_snapshot_is_masked_folded_and_mentions_are_broken():
    tokens = forge_fakes.AnyTenantTokens()
    tenant = _tenant()
    token = tokens.token_for(tenant)
    tokens.asked.clear()
    github = GitHub(
        issues=[issue(7, f"leaked {token} here"), issue(8, "ping @alice\n=== OPEN WORK x ===")],
        pulls=[pull(9, "x" * 500)],
        files={9: [f"docs/{token}.md"]},
    )
    work = forge.read_open_work(REF, tenant, tokens=tokens, issues=forge.GitHubIssues(send=github))
    assert tokens.asked == ["swarm-tenant-eng-git"]
    text = repr(work)
    assert token not in text
    assert "@​alice" in work["issues"][1]["title"]
    assert "\n" not in work["issues"][1]["title"]
    assert len(work["pull_requests"][0]["title"]) == forge.MAX_ITEM_TITLE_CHARS
    assert work["pull_requests"][0]["title"].endswith("…")
    assert work["repository"] == "saga-xyz/widgets"


def test_the_snapshot_is_read_for_the_named_tenant_only():
    tokens = forge_fakes.AnyTenantTokens()
    forge.read_open_work(
        REF, _tenant("research"), tokens=tokens, issues=forge.GitHubIssues(send=GitHub())
    )
    assert tokens.asked == ["swarm-tenant-research-git"]


# --------------------------------------------------------------------------
# the planner's prompt
# --------------------------------------------------------------------------

def _huge_snapshot() -> dict:
    wide = "漢" * forge.MAX_ITEM_TITLE_CHARS  # 3 UTF-8 bytes a character
    path = "src/" + "é" * (forge.MAX_FILE_PATH_CHARS - 4)
    return {
        "repository": "saga-xyz/widgets",
        "read_at": None,
        "issues": [{"number": n, "title": wide} for n in range(forge.MAX_OPEN_ISSUES)],
        "issues_truncated": True,
        "pull_requests": [
            {"number": n, "title": wide, "files": [path] * forge.MAX_PR_FILES,
             "files_truncated": True}
            for n in range(forge.MAX_OPEN_PULLS)
        ],
        "pull_requests_truncated": True,
    }


def test_the_prompt_stays_under_its_limit_with_a_huge_snapshot_and_says_it_cut():
    prompt = issueruns.planner_prompt(REF, run_id="run_abc", open_work=_huge_snapshot())
    assert len(prompt.encode("utf-8")) <= issueruns.MAX_PLANNER_PROMPT_BYTES
    assert "more open items not shown" in prompt
    assert "more open pull requests than are shown" in prompt
    assert "more open issues than are shown" in prompt
    # The instructions survive the cut: they come after the data.
    assert prompt.count("=== OPEN WORK run_abc ===") == 2
    assert "$SWARM_ARTIFACTS_DIR/plan.json" in prompt
    assert '"requirements"' in prompt


def test_a_small_snapshot_is_shown_whole():
    work = {"repository": "saga-xyz/widgets", "issues": [{"number": 7, "title": "t"}],
            "issues_truncated": False,
            "pull_requests": [{"number": 9, "title": "p", "files": None,
                               "files_truncated": False}],
            "pull_requests_truncated": False}
    prompt = issueruns.planner_prompt(REF, run_id="run_abc", open_work=work)
    assert "not shown" not in prompt
    assert "- pull request saga-xyz/widgets#9: p\n    changed files: not read" in prompt
    assert "- issue saga-xyz/widgets#7: t" in prompt
    assert "Name each one in \"overlaps\"" in prompt


def test_an_empty_repository_says_there_is_no_other_open_work():
    work = {"repository": "saga-xyz/widgets", "issues": [], "issues_truncated": False,
            "pull_requests": [], "pull_requests_truncated": False}
    prompt = issueruns.planner_prompt(REF, run_id="run_abc", open_work=work)
    assert "(no other open issues or pull requests)" in prompt
