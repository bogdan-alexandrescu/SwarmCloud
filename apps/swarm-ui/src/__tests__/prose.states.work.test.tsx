// The states guard's work slice: prosestates.tsx visits each state in both themes, the base is its ceiling.
import { afterEach, vi } from 'vitest'

import { proseStateGroup } from './prosestates'
import { WORK } from './prosestates.work'
import { restoreWorkStubs } from './prosestates.work.mounts'

// Mermaid is not drawn here (diagram.view.test.tsx holds the renderer); the state is the dialog's words.
vi.mock('../DiagramView', () => ({ default: ({ title }: { title: string }) => <figure aria-label={title}><figcaption>Flowchart, two boxes</figcaption></figure> }))

afterEach(restoreWorkStubs)

proseStateGroup('work', WORK)
