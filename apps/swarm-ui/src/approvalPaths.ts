// The approvals inbox's addresses and kinds (ApprovalDecision.tsx), apart from
// the page so the screens that link to an item reach them without loading it.

/**
 * The page's address, and one item's (`/automate/approvals/<id>`, paths.ts). The
 * inbox's kind filter rides on both as `kind=` (`/automate/approvals?kind=merge`), so
 * opening an item keeps the list it was opened from (AUT-15).
 */
export const APPROVALS_ADDRESS = 'automate/approvals'
export function approvalAddress(id: string, kind: string | null = null): string {
  const q = new URLSearchParams({ approval: id })
  if (kind !== null) q.set('kind', kind)
  return `${APPROVALS_ADDRESS}?${q.toString()}`
}
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
