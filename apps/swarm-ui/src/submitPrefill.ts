/**
 * A TASK HANDED TO SUBMIT BY ANOTHER SCREEN (GT-GUIDE: the Git tokens page's
 * "Run a test task"). The handing screen calls `prefillTask` and navigates to
 * `work/new`; the task form opens with the runner chosen, the prompt and the
 * repository filled in, and the person still reads it and presses Submit.
 * Nothing is sent from here.
 *
 * Held in this module, not in the address: a prompt in the URL is a prompt in
 * the history, the referrer and every link a person copies. A reload loses
 * it, which costs one click on the steps card.
 *
 * Read with `peekTaskPrefill` and cleared with `clearTaskPrefill` in an
 * effect, not taken in a state initialiser: strict mode runs an initialiser
 * twice, and a take-and-clear there would hand the second run nothing.
 *
 * It names a runner profile BY NAME, as every caller does (invariant 10):
 * never an image, a command or a resource spec.
 */
export interface TaskPrefill {
  profile: string
  prompt: string
  repositoryUrl: string
}

let pending: TaskPrefill | null = null

export function prefillTask(p: TaskPrefill): void {
  pending = { ...p }
}

export function peekTaskPrefill(): TaskPrefill | null {
  return pending
}

export function clearTaskPrefill(): void {
  pending = null
}
