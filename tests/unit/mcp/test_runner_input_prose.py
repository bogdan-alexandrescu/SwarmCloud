"""What a caller is told a runner takes renders from the catalogue (archived pull request 213 review).

`RunnerProfile.inputs` is the one declaration, and swarm-api and the bridge
read it. `_table` renders a profile's inputs as a table -- each key,
`RunnerInput.describe()` and `means`, and for a `list` of `object`s or an
`object` one row per shape its `type` selects -- so no copy of a bound is
written by hand. The tests below hold the catalogue to declaring inputs and
the rendering to every shape.
"""

from __future__ import annotations

from swarm_common.profiles import RUNNER_PROFILES


#: The profiles that declare at least one input. `{}` (the prompt only) has no
#: table. Since contract request 32 (archived issue 218) no profile is `inputs=None`.
DECLARING = [name for name, profile in RUNNER_PROFILES.items() if profile.inputs]


def _cell(text: str) -> str:
    """`text` safe inside a Markdown table cell: `one of a | b` would split it."""
    return text.replace("|", "\\|")


def _table(name: str) -> str:
    """The table for `name`, exactly as the docs must carry it.

    A `list` of `object`s, or an `object` (request 32: `browser`'s `actions`),
    is followed by one row per shape its `type` selects, naming each field's
    kind, whether it is required, and what the runner does with it -- so a
    caller reads the eight action shapes from the same generated table.
    """
    rows = [
        f"| input | kind and bounds | what the {name} runner does with it |",
        "|---|---|---|",
    ]
    for key, spec in RUNNER_PROFILES[name].inputs.items():
        kind = spec.describe() + (", required" if spec.required else "")
        rows.append(f"| `{key}` | {_cell(kind)} | {_cell(spec.means)} |")
        shaped = spec.items if spec.kind == "list" and spec.items is not None else spec
        for shape, fields in (shaped.variants or {}).items():
            kinds = ", ".join(
                f"`{field}` {'(required) ' if f.required else ''}{f.describe()}"
                for field, f in fields.items()
            ) or "no field"
            means = "; ".join(f"`{field}`: {f.means}" for field, f in fields.items())
            rows.append(f"| `{key}` `{shape}` | {_cell(kinds)} | {_cell(means)} |")
    return "\n".join(rows) + "\n"


def test_some_profile_declares_inputs():
    """Guards every parametrisation below against reading nothing."""
    assert {"mock", "browser", "generic"} <= set(DECLARING), DECLARING


def test_the_browser_table_renders_every_action_shape():
    """The rendering for `list` and `object` request 32 asked for: one row per
    shape, so the table is not silent on what an action may carry."""
    table = _table("browser")
    for shape in RUNNER_PROFILES["browser"].inputs["actions"].items.variants:
        assert f"| `actions` `{shape}` |" in table, table
    assert "one of load \\| domcontentloaded" in table, "a `|` in a cell must be escaped"
