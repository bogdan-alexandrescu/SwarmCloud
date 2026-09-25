"""The issue forms are forms GitHub will render, and they offer the screens that exist.

Adopted from saga-prompt-lab on 2026-09-24 at the owner's request: bugs, fixes
and improvements are filed through `.github/ISSUE_TEMPLATE/`, and CLAUDE.md's
"Issues" section holds the rules around them.

WHY THIS IS A TEST. Those YAML files are read by exactly one program, GitHub's
issue-form renderer, and it reports a broken form in exactly one place: the
"New issue" page, where a form that fails GitHub's schema drops out of the
template chooser. The reporter files a blank issue instead, or gives up, and
nothing in CI ever notices. So the parts of GitHub's schema these forms depend
on are asserted here -- required keys, element types, unique ids and labels,
non-empty unique string options -- and so are the rules that are this
repository's own: every label a form applies is declared, every repo path a
form names exists, and an epic is filed with no task item in its body.

THE "WHERE" LISTS FOLLOW THE NAV; THEY ARE NOT A SECOND COPY OF IT. The bug and
feature forms ask where the reporter was, and for the web UI the honest answer
is a tab in `SECTIONS` (apps/swarm-ui/src/App.tsx). A hand-kept list of those
tabs is a mirrored copy, and every mirrored copy this repository has kept has
drifted (docs/mirrored-values.md). The nav has already renamed "Runner
profiles" to "Profile headroom" and taken a section from "Pools" back to
"Capacity"; a form still offering the old names sends a reporter looking for a
screen that is not there, and sends the fixer to the wrong file. So the parser
that reads `SECTIONS` for test_nav_headings_agree.py is IMPORTED, not
restated, and a tab added, renamed or removed without the forms following
fails here, naming the option to add or change.

WHAT IT CANNOT SEE. GitHub itself. Labels are checked against the checked-in
declaration `.github/labels.yml`, not against the repository's live labels --
a unit test here runs with no credentials -- so a label deleted on GitHub is
not caught; the form would still file, without the label. And this checks
that a form will render and points at real things, not that it asks good
questions.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

# THE NAV PARSER, IMPORTED. test_nav_headings_agree.py already reads SECTIONS
# out of App.tsx; a second regex over the same array would be the mirrored copy
# this file exists to prevent. If that helper is renamed, this import fails
# loudly at collection, which is the right way for this file to break.
from control_plane.test_nav_headings_agree import APP_TSX, SRC, _read, _sections

REPO = Path(__file__).resolve().parents[3]
TEMPLATES = REPO / ".github" / "ISSUE_TEMPLATE"
CONFIG = TEMPLATES / "config.yml"
LABELS = REPO / ".github" / "labels.yml"
CONTRACT = REPO / "CONTRACT.md"
HELP_TS = SRC / "help.ts"

#: The forms CLAUDE.md's "Issues" section sends people to. Named here so that a
#: form deleted or renamed fails loudly: the parametrised cases below iterate
#: whatever files exist, an empty parameter set is a SKIP, and a skip reads as a
#: pass in CI output.
REQUIRED_FORMS = ("bug_report.yml", "feature_request.yml", "epic.yml")

#: The forms that ask where the reporter was, and the id of that dropdown.
SURFACE_FORMS = ("bug_report.yml", "feature_request.yml")
SURFACE_ID = "surface"

#: GitHub's issue-form schema: the top-level keys it accepts and the element
#: types a body may hold. An unknown key or type is a form GitHub refuses.
TOP_LEVEL_KEYS = {"name", "description", "title", "labels", "assignees", "projects", "type", "body"}
ELEMENT_TYPES = {"markdown", "textarea", "input", "dropdown", "checkboxes"}
#: GitHub's rule for `id`: alphanumerics, `-` and `_`, unique within the form.
ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
#: GitHub caps a label's description at 100 characters and rejects longer.
LABEL_DESCRIPTION_MAX = 100

#: How a web-UI option is spelled: `Web UI · <what the reader saw>`, then the
#: route in parentheses when the thing has one of its own.
UI = "Web UI · "
ROUTE_SUFFIX = re.compile(r" \(#([\w/-]+)\)$")
#: Every other surface ends by saying where its code lives: `(apps/scheduler)`.
PATH_SUFFIX = re.compile(r" \(([^()]+)\)$")
NOT_SURE = "Not sure"

#: The label on a form whose issue must be filed with no task item in its body.
#: An epic closes on its COMMENTS (CLAUDE.md, "Issues"); a box in its body is one
#: that no comment matches.
INDEX_FREE_LABEL = "epic"
#: A Markdown list item that GitHub renders as a task: `- [ ]`, `* [x]`, `1. [X]`.
TASK_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\[[ xX]\]")

#: UI surfaces that are NOT a tab in SECTIONS, each with the file and component
#: that draws it. This is not a restatement of the nav: it is exactly the list
#: of things the nav does not reach directly. The component is still required
#: to exist, so a surface removed from the app cannot outlive it in the forms.
BEYOND_THE_NAV = {
    "Agent inspector": ("AgentDetail.tsx", "AgentDetailScreen"),
    "Checkpoint browser": ("CheckpointBrowser.tsx", "CheckpointBrowser"),
    "Navigation, layout or theme": ("App.tsx", "App"),
}


def _forms() -> list[Path]:
    return sorted(
        p
        for pattern in ("*.yml", "*.yaml")
        for p in TEMPLATES.glob(pattern)
        if p.stem != "config"
    )


FORMS = _forms()


def _load(path: Path) -> Any:
    assert path.is_file(), f"{path.relative_to(REPO)} is not present"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{path.relative_to(REPO)} is empty"
    return yaml.safe_load(text)


def _strings(node: Any) -> Iterator[str]:
    """Every string anywhere in a parsed YAML document."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _const(text: str, name: str) -> str:
    """A module-level string constant from TypeScript source."""
    m = re.search(rf"^(?:export )?const {re.escape(name)} = '([^']*)'", text, re.M)
    assert m is not None, f"`const {name} = '...'` is not declared where this test reads it"
    return m.group(1)


# --------------------------------------------------------------------------
# The forms exist, and GitHub will render them


def test_the_forms_this_repository_relies_on_exist() -> None:
    names = {p.name for p in FORMS}
    missing = [n for n in REQUIRED_FORMS if n not in names]
    assert not missing, (
        f"{missing} are not in .github/ISSUE_TEMPLATE/. CLAUDE.md's Issues section "
        "sends people to them, and every per-form check below iterates the files "
        "that exist -- so a missing form is a check that silently stopped running."
    )
    assert CONFIG.is_file(), ".github/ISSUE_TEMPLATE/config.yml is not present"
    # GitHub also offers Markdown templates. This repository uses forms only: a
    # .md template beside them is a second way to file that nothing here checks.
    markdown = sorted(p.name for p in TEMPLATES.glob("*.md"))
    assert not markdown, f"Markdown issue templates are not checked by this test: {markdown}"


def _element_problems(i: int, el: Any) -> list[str]:
    where = f"body[{i}]"
    if not isinstance(el, dict):
        return [f"{where} is not a mapping"]
    kind = el.get("type")
    if kind not in ELEMENT_TYPES:
        return [f"{where}: type {kind!r} is not one of {sorted(ELEMENT_TYPES)}"]
    attrs = el.get("attributes")
    if not isinstance(attrs, dict):
        return [f"{where} ({kind}): has no `attributes` mapping"]

    out: list[str] = []
    if kind == "markdown":
        if not (isinstance(attrs.get("value"), str) and attrs["value"].strip()):
            out.append(f"{where} (markdown): `attributes.value` must be non-empty text")
        if "validations" in el:
            out.append(f"{where} (markdown): markdown cannot carry `validations`")
        return out

    # `id` is optional to GitHub; it is required HERE, so that every answer on
    # a filed issue can be named -- and so the uniqueness rule has something
    # to check.
    ident = el.get("id")
    if not (isinstance(ident, str) and ID_RE.match(ident)):
        out.append(f"{where} ({kind}): `id` {ident!r} must match {ID_RE.pattern}")
    if not (isinstance(attrs.get("label"), str) and attrs["label"].strip()):
        out.append(f"{where} ({kind}): `attributes.label` is required")
    for key in ("description", "placeholder", "value", "render"):
        if key in attrs and not isinstance(attrs[key], str):
            out.append(f"{where} ({kind}): `attributes.{key}` must be text")
    validations = el.get("validations")
    if validations is not None and (
        not isinstance(validations, dict)
        or set(validations) != {"required"}
        or not isinstance(validations["required"], bool)
    ):
        out.append(f"{where} ({kind}): `validations` may only be {{required: true|false}}")

    if kind == "dropdown":
        options = attrs.get("options")
        if not (isinstance(options, list) and options):
            return out + [f"{where} (dropdown): `options` must be a non-empty list"]
        # NOT-A-STRING IS THE YAML TRAP: an unquoted `No` or `Off` parses as a
        # boolean, and GitHub then renders an option nobody wrote.
        bad = [o for o in options if not (isinstance(o, str) and o.strip())]
        if bad:
            out.append(f"{where} (dropdown): options must be non-empty text, got {bad!r}")
        dupes = sorted({o for o in options if options.count(o) > 1}, key=str)
        if dupes:
            out.append(f"{where} (dropdown): duplicate options {dupes!r}")
        # GitHub supplies its own "None" on a dropdown that is not required.
        if any(isinstance(o, str) and o.strip().lower() == "none" for o in options):
            out.append(f"{where} (dropdown): 'None' is GitHub's own option, not ours")
        if "multiple" in attrs and not isinstance(attrs["multiple"], bool):
            out.append(f"{where} (dropdown): `multiple` must be true or false")
        if "default" in attrs and not (
            isinstance(attrs["default"], int) and 0 <= attrs["default"] < len(options)
        ):
            out.append(f"{where} (dropdown): `default` must index into `options`")

    if kind == "checkboxes":
        options = attrs.get("options")
        if not (isinstance(options, list) and options):
            return out + [f"{where} (checkboxes): `options` must be a non-empty list"]
        labels: list[str] = []
        for j, opt in enumerate(options):
            if not (isinstance(opt, dict) and isinstance(opt.get("label"), str) and opt["label"].strip()):
                out.append(f"{where}.options[{j}]: each checkbox needs a text `label`")
                continue
            if "required" in opt and not isinstance(opt["required"], bool):
                out.append(f"{where}.options[{j}]: `required` must be true or false")
            labels.append(opt["label"])
        dupes = sorted({x for x in labels if labels.count(x) > 1})
        if dupes:
            out.append(f"{where} (checkboxes): duplicate options {dupes!r}")
    return out


@pytest.mark.parametrize("path", FORMS, ids=[p.name for p in FORMS])
def test_the_form_is_one_github_will_render(path: Path) -> None:
    form = _load(path)
    assert isinstance(form, dict), f"{path.name} is not a mapping at the top level"

    problems: list[str] = []
    unknown = sorted(set(form) - TOP_LEVEL_KEYS)
    if unknown:
        problems.append(f"unknown top-level keys {unknown}: GitHub rejects the form")
    for key in ("name", "description"):
        if not (isinstance(form.get(key), str) and form[key].strip()):
            problems.append(f"`{key}` is required and must be text")
    if "title" in form and not isinstance(form["title"], str):
        problems.append("`title` must be text")
    labels = form.get("labels", [])
    if not (isinstance(labels, list) and all(isinstance(x, str) and x for x in labels)):
        problems.append("`labels` must be a list of label names")

    body = form.get("body")
    if not (isinstance(body, list) and body):
        problems.append("`body` must be a non-empty list")
        body = []
    for i, el in enumerate(body):
        problems.extend(_element_problems(i, el))

    fields = [el for el in body if isinstance(el, dict) and el.get("type") != "markdown"]
    if not fields:
        problems.append("GitHub requires at least one field that is not markdown")
    ids = [el.get("id") for el in fields]
    dup_ids = sorted({x for x in ids if ids.count(x) > 1}, key=str)
    if dup_ids:
        problems.append(f"duplicate ids {dup_ids}: GitHub requires them unique")
    field_labels = [(el.get("attributes") or {}).get("label") for el in fields]
    dup_labels = sorted({x for x in field_labels if field_labels.count(x) > 1}, key=str)
    if dup_labels:
        problems.append(f"duplicate labels {dup_labels}: GitHub requires them unique")

    assert not problems, f"{path.name}:\n  " + "\n  ".join(problems)


def test_the_template_chooser_config() -> None:
    cfg = _load(CONFIG)
    assert isinstance(cfg, dict), "config.yml is not a mapping"
    unknown = sorted(set(cfg) - {"blank_issues_enabled", "contact_links"})
    assert not unknown, f"config.yml: unknown keys {unknown}"
    assert isinstance(cfg.get("blank_issues_enabled"), bool), (
        "config.yml: `blank_issues_enabled` must be true or false"
    )

    links = cfg.get("contact_links")
    assert isinstance(links, list) and links, "config.yml: `contact_links` must be a non-empty list"
    url_re = re.compile(r"^https://github\.com/([^/]+/[^/]+)/(blob|tree)/main/(.+)$")
    repos: set[str] = set()
    problems: list[str] = []
    for i, link in enumerate(links):
        if not isinstance(link, dict) or set(link) != {"name", "url", "about"}:
            problems.append(f"contact_links[{i}] must have exactly name, url and about")
            continue
        if not all(isinstance(link[k], str) and link[k].strip() for k in link):
            problems.append(f"contact_links[{i}]: name, url and about must be non-empty text")
            continue
        m = url_re.match(link["url"])
        if m is None:
            problems.append(
                f"{link['name']!r}: {link['url']} is not a blob/ or tree/ link on main, "
                "so this test cannot check that what it points at exists"
            )
            continue
        repos.add(m.group(1))
        target = REPO / m.group(3).split("#", 1)[0]
        exists = target.is_file() if m.group(2) == "blob" else target.is_dir()
        if not exists:
            problems.append(f"{link['name']!r} points at {m.group(3)}, which is not in this repository")
    if len(repos) > 1:
        problems.append(f"contact links point at more than one repository: {sorted(repos)}")
    assert not problems, "config.yml:\n  " + "\n  ".join(problems)


# --------------------------------------------------------------------------
# Labels


def _declared_labels() -> list[dict[str, Any]]:
    data = _load(LABELS)
    assert isinstance(data, list) and data, ".github/labels.yml must be a non-empty list"
    return data


def test_the_label_declaration_is_one_gh_label_create_accepts() -> None:
    problems: list[str] = []
    names: list[str] = []
    for i, entry in enumerate(_declared_labels()):
        if not isinstance(entry, dict) or set(entry) != {"name", "color", "description"}:
            problems.append(f"labels.yml[{i}] must have exactly name, color and description")
            continue
        name, color, description = entry["name"], entry["color"], entry["description"]
        names.append(name)
        if not (isinstance(name, str) and name.strip()):
            problems.append(f"labels.yml[{i}]: name must be text")
        # Quoted in the file, and checked as TEXT: an unquoted colour made only of
        # digits (`008672`) parses as the integer 8672 and loses its leading zeros.
        if not (isinstance(color, str) and re.fullmatch(r"[0-9a-f]{6}", color)):
            problems.append(f"{name!r}: color {color!r} must be six lower-case hex digits, quoted, no '#'")
        if not (isinstance(description, str) and 0 < len(description) <= LABEL_DESCRIPTION_MAX):
            problems.append(
                f"{name!r}: description must be text of 1-{LABEL_DESCRIPTION_MAX} characters "
                f"(GitHub's limit), got {len(description) if isinstance(description, str) else description!r}"
            )
    dupes = sorted({n for n in names if names.count(n) > 1}, key=str)
    if dupes:
        problems.append(f"declared twice: {dupes}")

    # Every declared label is applied by some form. The file declares what the
    # forms rely on; it is not a copy of every label on the repository, which
    # would be a mirror of GitHub state that nothing here can check.
    applied = {label for p in FORMS for label in (_load(p).get("labels") or [])}
    unused = sorted(set(names) - applied, key=str)
    if unused:
        problems.append(f"declared but applied by no form: {unused}")
    assert not problems, ".github/labels.yml:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("path", FORMS, ids=[p.name for p in FORMS])
def test_every_label_the_form_applies_is_declared(path: Path) -> None:
    declared = {e.get("name") for e in _declared_labels() if isinstance(e, dict)}
    applied = _load(path).get("labels") or []
    undeclared = [x for x in applied if x not in declared]
    assert not undeclared, (
        f"{path.name} applies {undeclared}, which .github/labels.yml does not declare. "
        "GitHub files the issue anyway and silently drops a label the repository "
        "does not have. Declare it, and create it: "
        "gh label create <name> --color <color> --description <description>"
    )


# --------------------------------------------------------------------------
# "Where did you see it" follows the nav


def _nav_options() -> dict[str, str]:
    """Every route the rail and its two utility buttons reach -> the option a form must carry."""
    app = _read(APP_TSX)
    out: dict[str, str] = {}
    for sid, slabel, tabs in _sections(app):
        for tid, tlabel in tabs:
            # A one-tab section draws no tab strip (Rail renders one only when
            # tabs.length > 1), so its reader sees the section's name and nothing else.
            name = slabel if len(tabs) == 1 else f"{slabel} › {tlabel}"
            out[f"{sid}/{tid}"] = f"{UI}{name} (#{sid}/{tid})"

    # The two utility buttons beside the sections. Read from the same constants
    # the router and the button use.
    ref = _const(app, "REFERENCE")
    out[ref] = f"{UI}{_const(app, 'REFERENCE_LABEL')} (#{ref})"
    help_route = _const(_read(HELP_TS), "HELP_ROUTE")
    # The Help screen's name is what the head's crumb prints for it. Anchored on
    # the crumb's own `??` fallback: the same `at.sectionId === HELP ? '...'`
    # shape also picks the rail button's CSS class, and a bare search for it
    # reads the name as "is-on".
    m = re.search(r"\?\? \(at\.sectionId === HELP \? '([^']+)'", app)
    assert m is not None, "could not read the name the head prints for the Help screen"
    out[help_route] = f"{UI}{m.group(1)} (#{help_route})"
    return out


def _surface_options(name: str) -> list[str]:
    form = _load(TEMPLATES / name)
    for el in form.get("body") or []:
        if isinstance(el, dict) and el.get("id") == SURFACE_ID:
            assert el.get("type") == "dropdown", f"{name}: `{SURFACE_ID}` must be a dropdown"
            options = el["attributes"]["options"]
            assert options, f"{name}: `{SURFACE_ID}` has no options"
            return options
    raise AssertionError(f"{name} has no `{SURFACE_ID}` dropdown, so nothing checks it against the nav")


@pytest.mark.parametrize("name", SURFACE_FORMS)
def test_the_web_ui_options_are_the_screens_the_nav_reaches(name: str) -> None:
    want = _nav_options()
    assert len(want) >= 3, f"the nav parsed to {len(want)} routes; this test would check almost nothing"

    routed: dict[str, list[str]] = {}
    for option in _surface_options(name):
        m = ROUTE_SUFFIX.search(option)
        if option.startswith(UI) and m:
            routed.setdefault(m.group(1), []).append(option)

    missing = [text for route, text in want.items() if route not in routed]
    stale = [o for route, opts in routed.items() if route not in want for o in opts]
    renamed = [
        f"{o!r} -> {want[route]!r}"
        for route, opts in routed.items()
        if route in want
        for o in opts
        if o != want[route]
    ]
    doubled = [route for route, opts in routed.items() if len(opts) > 1]
    assert not (missing or stale or renamed or doubled), (
        f"{name}'s `{SURFACE_ID}` dropdown has drifted from SECTIONS in apps/swarm-ui/src/App.tsx.\n"
        f"  add (a screen the nav reaches that a reporter cannot name): {missing}\n"
        f"  remove (no such route any more): {stale}\n"
        f"  rename (the nav calls it something else now): {renamed}\n"
        f"  offered twice: {doubled}"
    )


@pytest.mark.parametrize("name", SURFACE_FORMS)
def test_the_web_ui_options_beyond_the_nav_still_exist(name: str) -> None:
    named: dict[str, str] = {}
    problems: list[str] = []
    for option in _surface_options(name):
        if not option.startswith(UI) or ROUTE_SUFFIX.search(option):
            continue
        head = re.split(r" — | \(", option[len(UI) :], maxsplit=1)[0]
        if head not in BEYOND_THE_NAV:
            problems.append(
                f"{option!r} has no route and is not one of {sorted(BEYOND_THE_NAV)}: "
                "either give it the (#route) the nav uses, or add it to BEYOND_THE_NAV "
                "with the component that draws it"
            )
        named[head] = option
    for head in sorted(set(BEYOND_THE_NAV) - set(named)):
        problems.append(f"no option for {head!r}")
    for head, (file, component) in BEYOND_THE_NAV.items():
        text = _read(SRC / file)
        if not re.search(rf"^export function {re.escape(component)}\(", text, re.M):
            problems.append(f"{head!r}: {file} no longer exports {component}")
    assert not problems, f"{name}:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("name", SURFACE_FORMS)
def test_every_other_surface_names_code_that_exists(name: str) -> None:
    """`Scheduler — admission and dispatch (apps/scheduler)` points the fixer at a real place."""
    problems: list[str] = []
    for option in _surface_options(name):
        if option.startswith(UI) or option.startswith(NOT_SURE):
            continue
        m = PATH_SUFFIX.search(option)
        if m is None:
            problems.append(f"{option!r} does not end by saying where its code lives: (path, path)")
            continue
        for token in m.group(1).split(", "):
            if not (REPO / token).exists():
                problems.append(f"{option!r} names {token}, which is not in this repository")
    assert not problems, f"{name}:\n  " + "\n  ".join(problems)


# --------------------------------------------------------------------------
# What the forms cite is real


def _top_level() -> set[str]:
    return {p.name for p in REPO.iterdir()}


@pytest.mark.parametrize("path", [*FORMS, CONFIG], ids=[p.name for p in [*FORMS, CONFIG]])
def test_every_repo_path_a_form_names_exists(path: Path) -> None:
    """A backticked `dir/file` in a form is an instruction to look there."""
    top = _top_level()
    cited = [
        token
        for text in _strings(_load(path))
        for token in re.findall(r"`([^`\s]+)`", text)
        if "/" in token and token.split("/", 1)[0] in top
    ]
    missing = sorted({t for t in cited if not (REPO / t.rstrip("/")).exists()})
    assert not missing, f"{path.name} cites paths that are not in this repository: {missing}"


def _invariant_count() -> int:
    text = _read(CONTRACT)
    heading = "## Invariants that must never be violated"
    assert heading in text, f"CONTRACT.md has no {heading!r} section to count"
    section = text.split(heading, 1)[1].split("\n## ", 1)[0]
    numbers = [int(n) for n in re.findall(r"^(\d+)\. ", section, re.M)]
    assert numbers == list(range(1, len(numbers) + 1)) and numbers, (
        f"CONTRACT.md's invariants are not numbered 1..N: {numbers}"
    )
    return len(numbers)


@pytest.mark.parametrize("path", FORMS, ids=[p.name for p in FORMS])
def test_every_invariant_a_form_cites_exists(path: Path) -> None:
    """`invariant 9` in a form has to be an invariant CONTRACT.md numbers.

    Out-of-range only. It cannot tell that invariant 9 still MEANS tenant
    isolation if CONTRACT.md is ever renumbered; the forms say what each one is
    in words beside the number for that reason.
    """
    count = _invariant_count()
    cited = {
        int(n)
        for text in _strings(_load(path))
        for ref in re.findall(r"\binvariants? ((?:\d+(?:\s*(?:–|-|,|and)\s*)?)+)", text, re.I)
        for n in re.findall(r"\d+", ref)
    }
    wrong = sorted(n for n in cited if not 1 <= n <= count)
    assert not wrong, f"{path.name} cites invariants {wrong}; CONTRACT.md numbers 1-{count}"


# --------------------------------------------------------------------------
# An epic's body is not an index


def _task_items_written_into_the_body(form: dict[str, Any]) -> list[str]:
    """Every task item GitHub writes into a filed issue's body before anyone types a word.

    What GitHub SUBMITS from a form is not what the form shows. A `markdown`
    element is shown and never submitted, so the box shape an epic's preamble
    illustrates is not in the filed body. A `checkboxes` element is always
    submitted, as `### <label>` followed by one `- [X]` or `- [ ]` task item per
    option -- a required acknowledgement is therefore a box that arrives ticked.
    A prefilled `value` is submitted as it stands unless the reporter clears it
    (inside a fenced block when `render` is set, where it is not a task). A
    dropdown writes the option picked, as plain text.
    """
    out: list[str] = []
    for i, el in enumerate(form.get("body") or []):
        if not isinstance(el, dict):
            continue
        kind = el.get("type")
        attrs = el.get("attributes") or {}
        where = f"body[{i}] ({kind}) {attrs.get('label')!r}"
        if kind == "checkboxes":
            for opt in attrs.get("options") or []:
                text = opt.get("label") if isinstance(opt, dict) else opt
                out.append(f"{where}: checkbox {text!r}")
            continue
        written: list[str] = []
        if kind in ("textarea", "input") and isinstance(attrs.get("value"), str) and not attrs.get("render"):
            written = attrs["value"].splitlines()
        elif kind == "dropdown":
            written = [o for o in attrs.get("options") or [] if isinstance(o, str)]
        out.extend(f"{where}: {line.strip()!r}" for line in written if TASK_ITEM.match(line))
    return out


def test_an_epic_is_filed_with_no_task_item_in_its_body() -> None:
    """The comments are an epic's record; a box in its body is one no comment matches.

    CLAUDE.md closes an epic by enumerating the boxes in its COMMENTS and
    reconciling them against anything the body lists -- if the two disagree,
    the epic is not closeable. A task item the form writes into every epic's
    body therefore makes every epic unclosable by that procedure. Ticked on
    arrival, it also reads as finished work to anyone who trusts the body over
    the comments -- the failure CLAUDE.md cites from saga-prompt-lab's #871,
    which closed on its body's index over two live defects the index never
    listed. Prompt Lab's own epics (#1399) carry no task item in the body.

    This holds the PROPERTY -- nothing the form submits is a task item -- not
    the shape "has no checkboxes element", so a ticked box smuggled in as a
    textarea's prefilled value fails here too.
    """
    epics = [p for p in FORMS if INDEX_FREE_LABEL in (_load(p).get("labels") or [])]
    assert epics, (
        f"no form applies the {INDEX_FREE_LABEL!r} label, so this check reads nothing; "
        "CLAUDE.md's Issues section sends every wave's findings to one"
    )
    problems = {p.name: _task_items_written_into_the_body(_load(p)) for p in epics}
    problems = {name: items for name, items in problems.items() if items}
    assert not problems, (
        "These forms write task items into every issue they file. An epic's boxes "
        "belong in its comments; say the rule in a `markdown` element, which GitHub "
        "shows and never submits.\n  "
        + "\n  ".join(f"{name}: {item}" for name, items in problems.items() for item in items)
    )
