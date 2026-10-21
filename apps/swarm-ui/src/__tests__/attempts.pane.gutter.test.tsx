// task-015 (visual QA run 2, #1680): AT 1440 THE ATTEMPTS PANE'S HEADER LINE
// RAN INTO THE PANE'S SCROLLBAR.
//
// From 561px up the split's pane is the one vertical scroller (agents.css,
// "THE COLUMN DOES NOT SCROLL; ITS PANE DOES", owner 2026-10-03; held by
// details.v3.test.tsx "the detail has ONE scroll container"), so its
// scrollbar sits on the pane's right edge -- and the attempts' right-aligned
// figures (`attempts` toolbar's event count, the head's read age) sat flush
// against it. The pane keeps its scroller; the attempts' content keeps a
// gutter off it. On a phone the column scrolls with the page and the drawer's
// own padding is the gutter, so no second inset is added there.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { at, attempt, ev, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadAttempts: vi.fn(),
  loadAgentDetail: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AttemptTimelineScreen } = await import('../AttemptTimeline')

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

afterEach(cleanup)

async function mount(): Promise<HTMLElement> {
  const t = task({ state: 'RUNNING', started_at: at(1) })
  api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1, { completed_at: null })] }))
  api.loadAgentDetail.mockResolvedValue(ok({ task: t, events: [ev('submitted', at(-1), null)], eventsDetail: null }))
  const { container } = render(<AttemptTimelineScreen taskId={t.id} />)
  await waitFor(() => expect(container.querySelector('.ctl-toolbar')).not.toBeNull())
  return container as HTMLElement
}

/** The right padding the cascade gives each element between `el` and `top`, outermost last. */
function insets(el: HTMLElement, top: HTMLElement, env: CascadeEnv): string[] {
  const out: string[] = []
  for (let n: HTMLElement | null = el; n !== null && n !== top; n = n.parentElement) {
    const v = painted(n, ['padding-right', 'padding-inline-end'], env)
    if (v !== null && !/^0(px)?$/.test(v.trim())) out.push(v.trim())
  }
  return out
}

describe('the Attempts pane keeps its content off the pane’s scrollbar', () => {
  it('at 1440 the toolbar sits inside a right gutter; at 390 it does not', async () => {
    const root = await mount()
    const toolbar = root.querySelector<HTMLElement>('.ctl-toolbar')!
    // MUTATION: delete `.att-pane`'s `padding-right` (or its wrapper) and the
    // `attempts · 3 events` line runs into the scrollbar again.
    expect(insets(toolbar, root, WIDE)).toEqual(['var(--ctl-s3)'])
    // MUTATION: drop the `min-width: 561px` query and a phone loses 12px of an
    // already narrow column to a scrollbar that is not there.
    expect(insets(toolbar, root, PHONE)).toEqual([])
  })

  it('the head’s read age shares the same gutter', async () => {
    const root = await mount()
    const acts = root.querySelector<HTMLElement>('.c-phead')
    // MUTATION: wrap only the body, not the Screen, and the head's right end
    // (`.c-acts`) stays flush against the scrollbar.
    expect(acts).not.toBeNull()
    expect(insets(acts!, root, WIDE)).toEqual(['var(--ctl-s3)'])
  })
})
