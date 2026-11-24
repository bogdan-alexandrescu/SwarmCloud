// The approvals inbox's addresses and kinds (ApprovalDecision.tsx), apart from
// the page so the screens that link to an item reach them without loading it.

/**
 * The page's address, and one item's (`/automate/approvals/<id>`, paths.ts). The
 * inbox's kind filter rides on both as `kind=` (`/automate/approvals?kind=merge`), so
 * opening an item keeps the list it was opened from (AUT-15).
 */
export const APPROVALS_ADDRESS = 'automate/approvals'
export function approvalAddress(id: string, kind: string | null = null, board: string | null = null): string {
  const q = new URLSearchParams({ approval: id })
  if (kind !== null) q.set('kind', kind)
  if (board !== null) {
    // Opened from the Pipeline board (plans-pipeline §2.1): the board's
    // filters ride on the item's address, so its Back is a link to the board
    // as it was, not a step back through history.
    q.set(FROM_KEY, FROM_BOARD)
    const b = new URLSearchParams(board)
    for (const k of BOARD_KEYS) {
      const v = b.get(k)
      if (v) q.set(k, v)
    }
  }
  return `${APPROVALS_ADDRESS}?${q.toString()}`
}

/** The Pipeline board's address (plans-pipeline §2.1), its filters kept. */
export const PIPELINE_ADDRESS = 'automate/pipeline'
/** The board's filters, the keys its address carries and an item opened from it carries back. */
export const BOARD_KEYS = ['source', 'state', 'waits'] as const
/** `from=pipeline` on an item's address: it was opened from the board. */
export const FROM_KEY = 'from'
export const FROM_BOARD = 'pipeline'

/** The board's own query out of any address's query: its filter keys only, in their order. */
export function boardQuery(view: string | null): string {
  const from = new URLSearchParams(view ?? '')
  const out = new URLSearchParams()
  for (const k of BOARD_KEYS) {
    const v = from.get(k)
    if (v) out.set(k, v)
  }
  return out.toString()
}

export function pipelineAddress(board: string | null = null): string {
  const q = boardQuery(board)
  return q === '' ? PIPELINE_ADDRESS : `${PIPELINE_ADDRESS}?${q}`
}

/** The board an item's address says it was opened from, as the board's query, or null for none. */
export function boardOf(view: string | null): string | null {
  if (view === null || view === '') return null
  return new URLSearchParams(view).get(FROM_KEY) === FROM_BOARD ? boardQuery(view) : null
}

/** The kinds the Pipeline board draws (§2.1): Approvals lists the others (§2.2). */
export const BOARD_KINDS: readonly string[] = ['plan', 'hold', 'merge', 'proposal']
export function approvalsAddress(kind: string | null = null): string {
  return kind === null ? APPROVALS_ADDRESS : `${APPROVALS_ADDRESS}?${new URLSearchParams({ kind }).toString()}`
}

/** §4.5's kinds, as the person reads them. */
export const APPROVAL_KIND: Readonly<Record<string, string>> = {
  run: 'Run',
  plan: 'Plan',
  hold: 'Held plan',
  merge: 'Merge',
  proposal: 'Proposal',
  spec: 'Spec',
  // A workflow step waiting for a member's answer (human-steps.md §4.5),
  // projected from its task. The inbox lists it and never takes the answer.
  input: 'Answer',
  // A step whose work-contract acceptance waits on its owner (jev-contract.md
  // section 5.4), projected from its task: the detail serves Jev's judgment,
  // and the step is decided on its own page, never here.
  contract: 'Contract judgment',
}
