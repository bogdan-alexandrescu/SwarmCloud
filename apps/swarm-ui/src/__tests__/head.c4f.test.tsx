// ONE PAGE-HEAD SHAPE, ONE AGE PER SCREEN, AND THE CADENCES (#138, #98,
// #117; the owner's rulings of 2026-10-07).
//
// #138: title left, actions right, no subtitle line under any Screen's title;
// the screen's count is a note on its first card; refresh is a quiet control
// in the head's actions carrying its own ticking read age (`⟳ 12 s`).
// #98: that control is the screen's one age; a panel states its freshness only
// when it is stale (`from 6 min ago`) and is silent when fresh.
// #117: the cadences, and a poll that pauses while the tab is hidden and stops
// after fifteen minutes without input, behind a `Paused · resume` control.
//
// BREAK IT: put a `<p className="sub">` back in `PageHead`; print the age on a
// fresh empty panel; drop the idle stop from `Screen`; set any cadence back.

import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  ACCOUNTS_POLL_MS,
  CAPACITY_PHONE_POLL_MS,
  HOLDERS_POLL_MS,
  POOLS_POLL_MS,
  QUOTA_POLL_MS,
  capacityPoll,
} from '../capacityPoll'
import { OVERVIEW_POLL_MS } from '../Overview'
import { AGED_AFTER_MS, IDLE_STOP_MS, PageHead, Screen, staleFoot } from '../Shell'
import { TIMELINE_POLL_MS } from '../TimelineLanes'
import type { Result } from '../fetch'

function setHidden(hidden: boolean): void {
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => (hidden ? 'hidden' : 'visible'),
  })
  document.dispatchEvent(new Event('visibilitychange'))
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

function fake(): void {
  vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
}

afterEach(() => {
  setHidden(false)
  vi.useRealTimers()
})

type Rows = { n: number }
const ok = (): Promise<Result<Rows>> => Promise.resolve({ status: 'ok', data: { n: 3 }, fetchedAt: Date.now() })

describe('#138: one head shape -- title left, actions right, no sub-line', () => {
  it('draws no sub-line under a Screen title, and its count as a note on the first card', async () => {
    fake()
    render(
      <Screen title="Pools" load={ok} summary={(d) => `${d.n} pools`}>
        {() => <section className="ctl-card">rows</section>}
      </Screen>,
    )
    await advance(0)
    const head = document.querySelector('.c-phead')!
    expect(head).not.toBeNull()
    expect(head.querySelector('.sub'), 'a sub-line under the title').toBeNull()
    expect(document.querySelector('p.sub'), 'a sub-line anywhere on the screen').toBeNull()
    expect(head.textContent, 'the count is still in the head').not.toContain('3 pools')
    const note = document.querySelector('.c-count-note')
    expect(note?.textContent).toBe('3 pools')
    // On the first card: the note comes before the screen's first card.
    const card = document.querySelector('.ctl-card')!
    expect(note!.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('says a real zero on a screen with no empty panel as its note, not under the title', async () => {
    fake()
    const empty = (): Promise<Result<Rows>> => Promise.resolve({ status: 'empty', fetchedAt: Date.now() })
    render(
      <Screen title="Artifacts" load={empty}>
        {() => <p>rows</p>}
      </Screen>,
    )
    await advance(0)
    expect(document.querySelector('.c-count-note')?.textContent).toBe('Nothing to show')
    expect(document.querySelector('.c-phead')!.textContent).not.toContain('Nothing to show')
  })

  it('puts every PageHead child in the right-hand actions, never on a line under the title', () => {
    render(
      <PageHead title="Platform counts">
        <button type="button">Run the count · 24 reads</button>
      </PageHead>,
    )
    const head = document.querySelector('.c-phead')!
    expect(head.querySelector('.sub')).toBeNull()
    const acts = head.querySelector('.c-acts')
    expect(acts?.textContent).toContain('Run the count')
    expect(head.querySelector('.head button'), 'a control beside the title: a second head shape').toBeNull()
  })

  it('makes refresh a quiet head control that carries its own ticking read age', async () => {
    fake()
    render(
      <Screen title="Tenants" load={ok}>
        {() => <p>rows</p>}
      </Screen>,
    )
    await advance(0)
    const control = () => document.querySelector('.c-phead .c-acts .c-refresh') as HTMLButtonElement
    expect(control()).not.toBeNull()
    expect(control().textContent).toBe('⟳ 0 s')
    await advance(12_000)
    expect(control().textContent, 'the age did not tick').toBe('⟳ 10 s')
    expect(control().getAttribute('aria-label')).toMatch(/^Refresh · read 10 s ago/)
  })
})

describe('#98: a panel states its freshness only when it is stale', () => {
  it('words a stale age as `from N ago` and says nothing about a fresh one', () => {
    const now = Date.parse('2026-10-07T12:00:00Z')
    expect(staleFoot(now - 30_000, now)).toBeNull()
    expect(staleFoot(now - 6 * 60_000, now)).toBe('from 6 min ago')
    expect(staleFoot(null, now)).toBeNull()
  })

  it('prints no age on a fresh empty panel, and `from … ago` once it is stale', async () => {
    fake()
    const empty = (): Promise<Result<Rows>> => Promise.resolve({ status: 'empty', fetchedAt: Date.now() })
    render(
      <Screen title="Holders" load={empty} empty={{ heading: 'Nothing held', body: 'No leases.' }}>
        {() => null}
      </Screen>,
    )
    await advance(0)
    const panel = document.querySelector('.ctl-empty')!
    expect(panel.textContent).toContain('Nothing held')
    expect(panel.textContent, 'a fresh panel printed its age').not.toMatch(/ago|Checked/)
    await advance(AGED_AFTER_MS + 60_000)
    expect(document.querySelector('.ctl-empty')!.textContent).toContain('from 6 min ago')
  })
})

describe('#117: a polling screen re-reads, pauses when hidden, stops when idle', () => {
  it('reads like `every 30 s · read 4 s ago` in the head', async () => {
    fake()
    render(
      <Screen title="Pools" load={ok} pollMs={30_000}>
        {() => <p>rows</p>}
      </Screen>,
    )
    await advance(0)
    await advance(5_000)
    expect(document.querySelector('.c-refresh')!.textContent).toBe('⟳ every 30 s · read 5 s ago')
  })

  it('re-reads on its interval, not while hidden, stops after 15 min idle and resumes on click', async () => {
    fake()
    const load = vi.fn(ok)
    render(
      <Screen title="Pools" load={load} pollMs={30_000}>
        {() => <p>rows</p>}
      </Screen>,
    )
    await advance(0)
    expect(load).toHaveBeenCalledTimes(1)
    await advance(30_000)
    expect(load, 'no re-read on the interval').toHaveBeenCalledTimes(2)

    setHidden(true)
    await advance(120_000)
    expect(load, 'read while the tab was hidden').toHaveBeenCalledTimes(2)
    setHidden(false)
    await advance(0)
    expect(load, 'no read on return').toHaveBeenCalledTimes(3)

    // Fifteen minutes with no input: the poll stops and says so.
    await advance(IDLE_STOP_MS)
    const calls = load.mock.calls.length
    await advance(5 * 60_000)
    expect(load, 'kept polling an idle screen').toHaveBeenCalledTimes(calls)
    const resume = screen.getByRole('button', { name: /resume/i })
    expect(resume.textContent).toBe('Paused · resume')

    fireEvent.click(resume)
    await advance(0)
    expect(load, 'resume did not read').toHaveBeenCalledTimes(calls + 1)
    expect(screen.queryByRole('button', { name: /resume/i })).toBeNull()
    await advance(30_000)
    expect(load, 'resume did not restart the poll').toHaveBeenCalledTimes(calls + 2)
  })
})

describe('#117: the cadences, one named constant each', () => {
  it('pins Timeline at 60 s, the capacity screens at 30 s (60 s on a phone), Overview at 20 s', () => {
    expect(TIMELINE_POLL_MS).toBe(60_000)
    expect(POOLS_POLL_MS).toBe(30_000)
    expect(HOLDERS_POLL_MS).toBe(30_000)
    expect(ACCOUNTS_POLL_MS).toBe(30_000)
    expect(QUOTA_POLL_MS).toBe(30_000)
    expect(CAPACITY_PHONE_POLL_MS).toBe(60_000)
    expect(OVERVIEW_POLL_MS).toBe(20_000)
    expect(IDLE_STOP_MS).toBe(15 * 60_000)
  })

  it('slows a capacity screen to 60 s below the phone breakpoint', () => {
    const was = window.matchMedia
    try {
      window.matchMedia = ((q: string) => ({ matches: true, media: q, addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia
      expect(capacityPoll(POOLS_POLL_MS)(null)).toBe(60_000)
      window.matchMedia = ((q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia
      expect(capacityPoll(POOLS_POLL_MS)(null)).toBe(30_000)
    } finally {
      window.matchMedia = was
    }
  })
})
