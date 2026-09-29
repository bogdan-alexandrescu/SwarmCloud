# The diff viewer (#310)

`apps/swarm-ui/src/diff/` holds a self-contained viewer for the patch an agent
produces: `<DiffView patch={string} getFile? />`. This is step 1 of #310. The
component exists and is tested, but no screen mounts it yet. It goes into
`AgentDetail.tsx` in a later step, once the lane that currently owns that file
has merged.

| file | what it is |
|---|---|
| `parse.ts` | `parseUnifiedDiff(text)`: `git diff --binary` output read into files, hunks and numbered lines |
| `rows.ts` | the diff as a flat list of fixed-height rows, plus gaps, find and offsets. Pure functions. |
| `storage.ts` | the remembered unified/split choice |
| `DiffView.tsx` | the component |

Tests: `src/__tests__/diff/parse.test.ts` and `view.test.tsx`. The multi-file
case is a real `git diff --binary` in `src/__tests__/diff/fixture.ts`.

## Why each decision is the way it is

**The parser never throws.** The patch is text an agent wrote. A viewer that
crashes on it takes the whole page down with it. Every malformed shape returns
`{ ok: false, error: { kind, line, message } }` instead. `kind` is one of
`not-text`, `no-file-header`, `bad-file-header`, `bad-path`,
`bad-hunk-header`, `truncated-hunk` or `unexpected-line`, so a caller can
switch on it. The viewer shows it as one alert line: `patch not read: … ·
line N`. A fuzz test corrupts the real fixture 400 ways and checks that none
of them throws.

**Paths come from the least ambiguous source available.** Unquoted, the
`diff --git a/x b/y` line has more than one reading when a path contains
` b/`. So the parser takes paths from `rename from`/`copy from`, then from
`---`/`+++`, and uses the `diff --git` line only when neither exists. That
happens for a mode change, a pure rename, and a binary file. Quoted paths are
C-unescaped, and octal escapes are decoded as UTF-8 bytes, which is how git
writes `café` or a path with a tab.

**A binary patch is never parsed as text.** Lines in a `GIT binary patch`
body can begin with `-` or `+`. The parser skips the body up to the next
`diff --git` and marks the file `binary`.

**CRLF has two meanings, and the parser keeps them apart.** If every line of
the patch ends in CRLF, the patch was converted in transit, so the CR is
dropped. If only some lines end in CR, the CR belongs to the file content (a
CRLF file inside an LF patch). Those lines keep it, and the viewer draws it as
a visible `␍`. `\ No newline at end of file` is attached to the line it
follows, and the viewer draws it as a mark rather than dropping it. Both are
real differences, and a reader may be looking for exactly that one.

**No line is ever invented.** The unchanged lines between hunks are not in
the patch. Without a `getFile(path, side)` prop, a gap says `N unchanged
lines · context not available`. With `getFile`, the viewer fetches the file
and first checks it against every line the patch says the new side holds, at
that line number and character for character. If any line disagrees (a
different revision, or a truncated read), it shows `context not shown: the
file does not match this patch` and no context lines. A failed read says
`context not read: <error>` and offers a retry. This is the console's
absence rule (design-system.md §8.1) applied to source lines: an unread line
is shown as unread, never filled in.

**A big patch costs only what is on screen.** Rows have fixed heights (22px,
36px for a file header), and each row is drawn with its height inline. The
viewer draws only the rows within 1200px of the scroller's visible window;
two spacers stand in for everything else. A test renders a 50,000-line patch
and asserts fewer than 300 rows in the DOM. Lines never wrap. They scroll
sideways, so the text stays exactly as given and a row cannot grow away from
its offset. The widest line sets `--diff-ch`, which gives every text column
the same width and keeps the two halves of the split view aligned.

**Split is a preference, not a guarantee.** The choice is stored in
`localStorage` under `swarm.diff.view`. Every read and write is in
try/catch, because storage throws in private windows and embedded previews.
When the viewer is narrower than 700px (its own box, or the window if it has
not been laid out yet), it is unified regardless of the stored choice: two
columns of code do not fit.

**Text reaches the DOM only as React text nodes.** There is no
`dangerouslySetInnerHTML` and no syntax highlighter. A test renders
`<img onerror>` and `<script>` inside a patch and asserts that neither
becomes an element.

**Colour is not the only channel.** Added and removed lines use tints of
`--ok` and `--bad`, declared once as `--diff-add-bg` and `--diff-del-bg`.
Because they are mixed from tokens that each theme already tunes, they work in
dark and light without a light-mode block. Each line also keeps its `+`/`-`
sign in its own column, so the two stay distinguishable in greyscale. Find
matches use `--diff-match-bg`. The current match adds an outline rather than
a stronger tint, because a stronger tint would fall below AA for `--text`
(`tests/unit/control_plane/test_ui_contrast.py` measures that pair).

## Keys and controls

Keys work while focus is anywhere in the viewer except a text field, and are
ignored if a modifier is held: `n`/`p` for the next and previous file, `j`/`k`
for the next and previous hunk, `/` to focus find. In the find box, Enter goes
to the next match and Shift+Enter to the previous one. Find is
case-insensitive across every hunk line of every file. It opens any collapsed
file that holds a match, and it scrolls the current match into view. Every
control is a labelled `<button>` or `<input>`. The scroller is a labelled,
focusable region that carries its keys in `aria-keyshortcuts`, and the viewer
prints no how-to line (design-system.md §8.5).

## What it does not do yet

* It is not mounted anywhere (see above).
* Find searches the patch's lines, not context lines expanded from `getFile`,
  and not paths.
* Expanding context always asks for the `new` side. Added and deleted files
  have no gaps (their single hunk is the whole file), so the `old` side is
  never needed for a gap today. The `side` argument exists so that a caller
  wiring `getFile` to two revisions does not have to change its signature
  later.
