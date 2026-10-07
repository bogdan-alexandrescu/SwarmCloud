/**
 * EVERY KEYBOARD SHORTCUT THE CONSOLE ANSWERS, IN ONE TABLE (G1-21, QA pass
 * 2026-10-07).
 *
 * N, T, W, I, `/` and the rest existed, and only the Submit page's footnote
 * mentioned one of them: ninety-odd Help topics and not a word about the
 * keyboard. The Help topic `keyboard` is GENERATED from this table, and the
 * handlers that navigate -- N and `?` (SkyShell, Spine.tsx) and T, W and I
 * (SubmitChooser.tsx) -- read their keys from it, so a key cannot be added to
 * a handler and left out of Help without the handler changing here first.
 * The keys that act inside one view (the log, the diff, the agent list) are
 * held to their handlers by `src/__tests__/qa.g1.help.test.tsx`, which reads
 * each handler's source for the key this table says it answers.
 *
 * Pure data: no React, and nothing from `help.ts`, which imports this.
 */

/** The task form's and the workflow form's addresses, as `go` takes them. */
export const TASK_FORM = 'work/new'
export const WORKFLOW_FORM = 'work/new-workflow'
/** The issue form (intake-tenants.html 1A): /submit/issue, key I. */
export const ISSUE_FORM = 'work/new-issue'

/** Where a key is listened for. */
export type ShortcutScope = 'anywhere' | 'submit' | 'agent' | 'log' | 'diff'

export interface Shortcut {
  /** `KeyboardEvent.key` as the handler compares it: lowercase for a letter. */
  key: string
  /** The key as a `<kbd>` and the Help topic draw it. */
  label: string
  scope: ShortcutScope
  /** What it does, as one clause. */
  does: string
  /** For a key that navigates, the address `go` takes. */
  to?: string
}

/** The scope as the Help topic says it, after the clause. */
export const SCOPE_WORDS: Readonly<Record<ShortcutScope, string>> = {
  anywhere: 'from any page',
  submit: 'on Submit',
  agent: 'on an agent',
  log: 'in an agent’s log',
  diff: 'in a diff',
}

export const SHORTCUTS: readonly Shortcut[] = [
  { key: 'n', label: 'N', scope: 'anywhere', does: 'opens Submit', to: 'submit' },
  { key: '?', label: '?', scope: 'anywhere', does: 'opens this topic' },
  { key: 'Escape', label: 'Esc', scope: 'anywhere', does: 'closes the innermost thing that is open: a help card, a menu, a viewer, the agent' },
  { key: 't', label: 'T', scope: 'submit', does: 'starts a task', to: TASK_FORM },
  { key: 'w', label: 'W', scope: 'submit', does: 'starts a workflow', to: WORKFLOW_FORM },
  { key: 'i', label: 'I', scope: 'submit', does: 'starts from a GitHub issue', to: ISSUE_FORM },
  { key: '[', label: '[', scope: 'agent', does: 'folds the list beside it to a strip, and back' },
  { key: 'f', label: 'F', scope: 'log', does: 'follows the newest lines of a live agent, or stops' },
  { key: 'w', label: 'W', scope: 'log', does: 'wraps long lines, or stops' },
  { key: 'e', label: 'E', scope: 'log', does: 'jumps to the next error' },
  { key: '/', label: '/', scope: 'log', does: 'searches the log' },
  { key: 'n', label: 'N', scope: 'diff', does: 'opens the next file, instead of Submit' },
  { key: 'p', label: 'P', scope: 'diff', does: 'opens the previous file' },
  { key: 'j', label: 'J', scope: 'diff', does: 'jumps to the next change' },
  { key: 'k', label: 'K', scope: 'diff', does: 'jumps to the previous change' },
  { key: '/', label: '/', scope: 'diff', does: 'finds text in the diff' },
]

/** The shortcuts listened for in one scope. */
export function shortcutsIn(scope: ShortcutScope): readonly Shortcut[] {
  return SHORTCUTS.filter((s) => s.scope === scope)
}

/** The one shortcut with this key in this scope. Throws on a table that lost it. */
export function shortcut(scope: ShortcutScope, key: string): Shortcut {
  const s = SHORTCUTS.find((x) => x.scope === scope && x.key === key)
  if (s === undefined) throw new Error(`no ${scope} shortcut on ${key}`)
  return s
}

/**
 * Whether a key landed where it is typing, not a command: a field, a select
 * or anything contenteditable. `closest`, not `isContentEditable`: the target
 * is often a span inside the editable element, and jsdom does not implement
 * `isContentEditable` at all.
 */
export function typedIntoField(target: EventTarget | null): boolean {
  const el = target instanceof Element ? target : null
  if (el !== null && el.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"])') !== null) return true
  return el instanceof HTMLElement && el.isContentEditable
}
