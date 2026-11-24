"""docs/install.md and the day-one plan say what the installer does (K1 to K9).

docs/runbooks/day-one-proof-install.md section 7 listed nine places where the
docs, the plugin or the console said something the product does not do. The
owner's decision of 2026-11-24 was to fix them before the first proof install.
Each case here holds one fixed disagreement to the code it was checked
against, so the docs cannot drift back without a red test:

  K1  the OAuth brand and the Desktop client are made in the console's
      Admin › Setup step Sign-in client (steps/edge.py, BrandStep,
      DesktopClientStep), never at a hidden prompt in stage 2;
  K2  Admin › Setup's checklist has eight lines plus an optional ninth
      (consolefinish/ChecklistFinish.tsx, `finishLines`);
  K3  no plugin command `/sc:submit` exists; the console and the docs name
      `/sc run --issue` (plugin/commands/sc.md, swarm_mcp/sc.py);
  K4  a release candidate is installed from `channels/rc.install.sh`
      (release.md, scripts/release/publish.sh);
  K5  the proposed budget is `sizing.BUDGET_FLOOR_USD` plus run time;
  K6  a deleted project's id is never reusable (uninstall.py);
  K7  the uninstall has as many steps as `uninstall.STEPS`;
  K8  stage 13's message names the hand-over button and its bound
      (steps/handoff.py, HAND_OVER, CONSOLE);
  K9  a teammate is told how to get `sc` before `sc login`.
"""

from __future__ import annotations

import re
from pathlib import Path

from swarm_install import sizing, uninstall
from swarm_install.steps import handoff

ROOT = Path(__file__).resolve().parents[3]
INSTALL = (ROOT / "docs" / "install.md").read_text(encoding="utf-8")
PLAN = (ROOT / "docs" / "design" / "day-one-plan.md").read_text(encoding="utf-8")


def section(text: str, heading: str) -> str:
    """The body under `heading` (a whole heading line), up to the next heading of any level."""
    start = text.index(heading + "\n") + len(heading) + 1
    following = re.search(r"^#{1,6} ", text[start:], flags=re.M)
    return text[start:] if following is None else text[start:start + following.start()]


def test_k1_stage_2_makes_no_brand_or_client_at_a_prompt() -> None:
    stage2 = section(INSTALL, "### Stage 2: the front door")
    assert "input hidden" not in stage2
    assert "Sign-in client" in stage2
    outside = section(INSTALL, "## Steps outside the terminal and the console")
    row = next(line for line in outside.splitlines() if line.startswith("| The OAuth brand"))
    assert "stage 2" not in row and "Sign-in client" in row


def test_k2_the_checklist_is_eight_lines_and_an_optional_ninth() -> None:
    step3 = section(INSTALL, "## Step 3: Admin › Setup in the console")
    assert "six steps" not in step3 and "seven rows" not in step3
    assert "eight lines" in step3 and "optional ninth" in step3
    for name in ("Cloud", "Sign-in client", "GitHub App", "First team", "Claude accounts", "Smoke agent",
                 "Verify", "Send to your team", "Add a plugin"):
        assert f"**{name}" in step3, name


def test_k3_no_doc_or_plugin_names_a_command_that_does_not_exist() -> None:
    texts = [INSTALL, PLAN, *(p.read_text(encoding="utf-8") for p in (ROOT / "plugin").rglob("*.md"))]
    for text in texts:
        assert "/sc:submit" not in text
    assert "/sc run --issue" in section(INSTALL, "## Step 3: Admin › Setup in the console")


def test_k4_a_candidate_is_installed_from_its_channel_script() -> None:
    p4 = PLAN[PLAN.index("**The commands the owner runs in P4**"):]
    p4 = p4[:p4.index("---")]
    assert "channels/rc.install.sh | SWARM_CHANNEL=rc sh" in p4
    assert "swarmcloud/install.sh | sh" not in p4


def test_k5_the_proposed_budget_is_the_sizing_floor_plus_run_time() -> None:
    costs = section(INSTALL, "### What it costs")
    assert f"{sizing.BUDGET_FLOOR_USD} USD plus" in costs
    assert "500 USD" not in costs and "300 USD plus" not in costs
    one_person = sizing.propose("individual", 1)["budget_usd"]
    assert f"{one_person} USD" in costs


def test_k6_a_deleted_projects_id_is_never_reusable() -> None:
    stage1 = section(INSTALL, "### Stage 1: foundations")
    assert "keeps its id" not in stage1
    assert "never reusable" in stage1


def test_k7_the_uninstall_lists_every_step() -> None:
    body = section(INSTALL, "### Uninstall")
    numbered = re.findall(r"^(\d+)\. ", body, flags=re.M)
    assert [int(n) for n in numbered] == [s.number for s in uninstall.STEPS]


def test_k8_stage_13_names_the_hand_over_button_and_its_bound() -> None:
    stage13 = section(INSTALL, "### Stage 13: the hand-off to the console")
    assert handoff.HAND_OVER in stage13
    assert f"up to {handoff._hours(handoff.CONSOLE.bound_s)}" in stage13


def test_k9_a_teammate_is_told_how_to_get_sc_before_sc_login() -> None:
    step3 = section(INSTALL, "## Step 3: Admin › Setup in the console")
    send = step3[step3.index("**Send to your team.**"):]
    assert "sc: command not found" in send and "install.sh" in send
