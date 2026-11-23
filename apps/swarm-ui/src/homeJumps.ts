// Home's jump links (Home.tsx), apart from the page so the spine's panel draws
// them without loading it.

/** The jump links of Home's own panel, in the order the page draws them (Spine.tsx). */
export const HOME_JUMPS: readonly { id: string; label: string }[] = [
  { id: 'home-needs', label: 'Needs you' },
  { id: 'home-work', label: 'Your work' },
  { id: 'home-finished', label: 'Recently finished' },
  { id: 'home-start', label: 'Start' },
]
