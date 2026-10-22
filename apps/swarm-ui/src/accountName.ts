/**
 * AN ACCOUNT AS THE CONSOLE NAMES IT (#1913; owner decision 2026-10-22, BGD,
 * "Editable display name"): its Name when its owner gave one, else its label,
 * exactly as before there were Names.
 *
 * The Name is DISPLAY TEXT. The label is the account's id (`<owner>:<label>`)
 * and is built into its Secret Manager secret and a Kubernetes annotation, so
 * it cannot change; renaming sets the Name instead. Wherever the id was
 * printed it stays printed beside this, so the Name never hides which account
 * it is, and nothing that matches, sorts by identity or arms a removal reads
 * this in place of the id or the label.
 *
 * Its own module rather than `api.ts`: several screens' tests replace `api`
 * wholesale, and a helper every render calls must not vanish with it.
 */
export function accountName(a: { label: string; display_name?: string | null }): string {
  const name = typeof a.display_name === 'string' ? a.display_name.trim() : ''
  return name === '' ? a.label : name
}
