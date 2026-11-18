// THE UNIFIED/SPLIT CHOICE IS REMEMBERED, AND THE VIEWER IS CORRECT WITHOUT
// IT. Every read and write is inside try/catch, as Activity.tsx's timeline
// view is: storage throws in a private window and in embedded previews, and
// the viewer then opens unified and keeps the choice for the session only.

export type ViewMode = 'unified' | 'split'

export const VIEW_KEY = 'swarm.diff.view'

export function rememberedViewMode(): ViewMode {
  try {
    return window.localStorage.getItem(VIEW_KEY) === 'split' ? 'split' : 'unified'
  } catch {
    return 'unified'
  }
}

export function rememberViewMode(mode: ViewMode): void {
  try {
    window.localStorage.setItem(VIEW_KEY, mode)
  } catch {
    // Nothing to do: the choice still holds until the viewer unmounts.
  }
}
