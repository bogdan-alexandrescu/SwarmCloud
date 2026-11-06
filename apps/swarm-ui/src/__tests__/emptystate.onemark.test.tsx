// ONE `real zero` PER EMPTY STATE.
//
// THE DEFECT, left behind when #145 landed. `Screen`'s `empty` prop became the
// shared `Absent` primitive (CH-10), and that primitive draws the `real zero`
// mark inside the heading by itself. Five screens had already put the words
// there by hand, so each of their empty states said it twice:
//
//   Agents           heading `No agents · real zero`      + the primitive's mark
//   AttemptTimeline  heading `No attempt · real zero`     + the primitive's mark
//   Capacity         body `<span class="ctl-mark is-zero">` + the primitive's mark
//   Holders          body `<span class="ctl-mark is-zero">` + the primitive's mark
//   Runtimes         body `<span class="ctl-mark is-zero">` + the primitive's mark
//
// A mark said twice is not twice as true. It is a second silhouette a reader
// has to reconcile with the first, and the hand-drawn span carried no
// accessible name at all, so a screen reader heard the words with nothing
// behind them. The primitive's mark is the one that stays: it is the only one
// with a sentence behind it.
//
// MUTATION: put `· real zero` back on either heading, or the span back in any
// of the three bodies. That screen's panel then holds two marks, or says the
// words twice, and its case below fails by name.

import { describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import type { ReactElement } from 'react'

const api = vi.hoisted(() => ({
  loadTasks: vi.fn(),
  loadAttempts: vi.fn(),
  loadAgentDetail: vi.fn(),
  loadCapacity: vi.fn(),
  loadHolders: vi.fn(),
  loadRuntimeTopology: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

import { AgentsScreen } from '../Agents'
import { AttemptTimelineScreen } from '../AttemptTimeline'
import { CapacityScreen } from '../Capacity'
import { HoldersScreen } from '../Holders'
import { RuntimesScreen } from '../Runtimes'

/** A read that succeeded and returned nothing: the one state these panels draw. */
function empty() {
  return { status: 'empty' as const, fetchedAt: Date.now(), serverAt: '2026-09-25T10:00:00Z' }
}

const CASES: { name: string; arrange: () => void; screen: () => ReactElement }[] = [
  {
    name: 'Agents',
    arrange: () => api.loadTasks.mockResolvedValue(empty()),
    screen: () => <AgentsScreen onOpen={() => {}} />,
  },
  {
    name: 'AttemptTimeline',
    arrange: () => {
      api.loadAttempts.mockResolvedValue(empty())
      api.loadAgentDetail.mockResolvedValue(empty())
    },
    screen: () => <AttemptTimelineScreen taskId="tsk_onemark" />,
  },
  {
    name: 'Capacity',
    arrange: () => api.loadCapacity.mockResolvedValue(empty()),
    screen: () => <CapacityScreen />,
  },
  {
    name: 'Holders',
    arrange: () => api.loadHolders.mockResolvedValue(empty()),
    screen: () => <HoldersScreen />,
  },
  {
    name: 'Runtimes',
    arrange: () => api.loadRuntimeTopology.mockResolvedValue(empty()),
    screen: () => <RuntimesScreen />,
  },
]

describe('an empty state carries exactly one `real zero`', () => {
  // THE COUNT IS PRINTED, so a table that ran over fewer screens than it names
  // cannot pass as a clean sweep.
  it('covers all five screens the duplicate was found on', () => {
    expect(CASES.map((c) => c.name)).toEqual(['Agents', 'AttemptTimeline', 'Capacity', 'Holders', 'Runtimes'])
  })

  for (const c of CASES) {
    it(`${c.name}: one mark, the primitive's, and the words once`, async () => {
      c.arrange()
      const { container } = render(c.screen())
      let panel: Element | null = null
      await waitFor(
        () => {
          panel = container.querySelector('.ctl-empty')
          expect(panel, `${c.name} drew no empty state`).not.toBeNull()
        },
        { timeout: 5000 },
      )
      const el = panel as unknown as Element
      // A REAL ZERO, NOT A FAILURE: the panel is the default variant.
      expect(el.className, `${c.name} drew a failure or partial variant`).toBe('ctl-empty')

      const marks = [...el.querySelectorAll('.ctl-mark')]
      expect(marks.length, `${c.name} draws ${marks.length} marks in one empty state`).toBe(1)
      // The one that stays is the primitive's: in the heading, with a sentence.
      expect(marks[0]!.parentElement?.tagName).toBe('H3')
      expect(marks[0]!.classList.contains('is-zero')).toBe(true)
      expect(marks[0]!.getAttribute('aria-label') ?? '', `${c.name}'s mark has no sentence`).not.toBe('')

      // THE WORDS ONCE. The mark's word (`none`, owner 2026-10-11) is the
      // mark's, and no hand-written copy repeats it or the old "real zero".
      // Through `Absent` the mark is an icon -- its silhouette and its
      // sentence, the heading carrying the words (automate2-17,
      // qa2fix26.test.tsx) -- so it draws no word at all; a screen's own
      // `.ctl-empty` still draws the worded mark.
      const icon = marks[0]!.classList.contains('is-icon')
      const word = marks[0]!.textContent ?? ''
      if (icon) expect(word, `${c.name}'s icon mark draws words before its heading`).toBe('')
      else expect(word, `${c.name}'s mark has no word`).not.toBe('')
      const rest = (el.textContent ?? '').replace(word, '')
      expect(rest, `${c.name} says "real zero" outside its mark`).not.toMatch(/real zero/i)
      expect(rest, `${c.name} says the mark's word "none" by hand`).not.toMatch(/(^|[^a-z])none([^a-z]|$)/)
    })
  }
})
