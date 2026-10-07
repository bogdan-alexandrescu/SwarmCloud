// THE HONESTY RULES, AS BEHAVIOUR.
//
// Every screen in this app loads through `Screen`, so `Screen` is where the
// rules either hold or do not:
//
//   * a failed read renders NO zero;
//   * a partial read states what it could not see;
//   * an unmeasured figure is an em dash and a measured zero is a 0;
//   * a failure with data in hand keeps the data, labelled with its age;
//   * an admin gate is information, not breakage.
//
// The Python files that "test the UI" can only check that those sentences
// appear in the source. They do appear in the source -- in the comments above
// this paragraph, among others -- which is exactly why a source grep is not a
// test of them. Here they are asserted on the DOM the component produces.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'

import { AGED_AFTER_MS, MAX_BACKOFF_MS, Screen, nextPollDelay, type ScreenReading } from '../Shell'
import type { ApiError, ApiErrorKind, Result } from '../fetch'
import { useNow } from '../useNow'
import { expectNoFigures } from './setup'

interface Rows {
  rows: { name: string; n: number }[]
}

const ROWS: Rows = { rows: [{ name: 'alpha', n: 7 }, { name: 'beta', n: 0 }] }

function err(kind: ApiErrorKind, over: Partial<ApiError> = {}): ApiError {
  return { kind, httpStatus: 503, code: 'unavailable', message: 'The read did not complete.', ...over }
}

/**
 * Render a Screen whose read resolves to exactly the Result under test.
 *
 * Deliberately NOT a mocked `fetch`: the states are the unit here, and driving
 * them through HTTP would test `read` twice and `Screen` once.
 */
function renderScreen(result: Result<Rows>, opts: { empty?: boolean } = {}) {
  return render(
    <Screen<Rows>
      title="Agents"
      load={async () => result}
      summary={(d) => <>{d.rows.length} rows</>}
      empty={opts.empty === false ? undefined : { heading: 'No agents are running', body: 'The read succeeded and returned nothing.' }}
    >
      {(d) => (
        <table>
          <tbody>
            {d.rows.map((r) => (
              <tr key={r.name}>
                <th>{r.name}</th>
                <td>{r.n}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Screen>,
  )
}

const body = (): HTMLElement => document.body

// ---------------------------------------------------------------------------
// ok
// ---------------------------------------------------------------------------

describe('ok', () => {
  it('renders the rows and says when they were read', async () => {
    renderScreen({ status: 'ok', data: ROWS, fetchedAt: Date.now(), serverAt: new Date().toISOString() })
    expect(await screen.findByText('alpha')).toBeTruthy()
    // #138: the count is a note over the first card, not a line under the
    // title, and WHEN it was read is on the head's refresh control, which is
    // also the way to read again. Both are assembled from several text nodes,
    // so they are read off the element rather than matched as one string.
    expect(document.querySelector('.c-count-note')?.textContent).toBe('2 rows')
    expect(document.querySelector('.c-phead .c-acts .c-refresh')?.textContent).toBe('⟳ 0 s')
    expect(screen.getByRole('button', { name: 'Refresh · read 0 s ago' })).toBeTruthy()
    // One shape (§6.12): title left, actions right, no line under the title.
    expect(document.querySelector('.sub'), 'the sub-line under the title is back').toBeNull()
  })

  it('renders a MEASURED zero as 0, never as an em dash', async () => {
    // The other half of the em-dash rule, and the half that is easy to lose
    // while fixing the first: a screen that prints "—" for a real zero has
    // stopped reporting the thing it measured.
    renderScreen({ status: 'ok', data: { rows: [{ name: 'beta', n: 0 }] }, fetchedAt: Date.now() })
    const cell = await screen.findByText('0')
    expect(cell.textContent).toBe('0')
    expect(body().textContent).not.toContain('—')
  })
})

// ---------------------------------------------------------------------------
// empty
// ---------------------------------------------------------------------------

describe('empty', () => {
  it('is a real absence, stated as one, with the rows not rendered at all', async () => {
    renderScreen({ status: 'empty', fetchedAt: Date.now() })
    expect(await screen.findByText('No agents are running')).toBeTruthy()
    expect(screen.queryByText('alpha')).toBeNull()
    // An empty read draws no count note: there is no card for it to sit on,
    // and the panel already says what was found.
    expect(document.querySelector('.c-count-note')).toBeNull()
    // WHEN IT WAS CHECKED is still on screen -- on the head's refresh control,
    // which is where every other state of this component puts its age (#98).
    expect(document.querySelector('.c-refresh')?.textContent).toBe('⟳ 0 s')
  })

  /**
   * CH-10. THE EMPTY STATE IS THE §6.9 SHAPE, NOT A HAND-BUILT BOX.
   *
   * It was a `.state` div with an h3, a paragraph and a `Checked just now.` --
   * no mark, so a real zero and a failed read differed by colour alone.
   *
   * RE-POINTED AGAIN (#98, owner ruling 2026-10-07). The CH-1/CH-10
   * settlement on #87 brought the panel's `Checked …` foot back, ticking. The
   * ruling makes a panel state freshness ONLY WHEN IT IS STALE: the head's
   * refresh control carries the age of a fresh read, so the foot is silent
   * while fresh and says `from 6 min ago` once stale (the next describe).
   *
   * MUTATION: put the `.state` box back. The panel is not `.ctl-empty` and
   * carries no `real zero` mark. MUTATION: draw the foot while fresh (the old
   * `Checked just now.`, or any age). The panel has a `.ctl-empty-foot`.
   */
  it('draws through the shared empty state: a mark and the heading, and no foot while fresh', async () => {
    renderScreen({ status: 'empty', fetchedAt: Date.now() })
    const heading = await screen.findByText('No agents are running')
    const panel = heading.closest('.ctl-empty')
    expect(panel, 'the empty state is not the shared .ctl-empty shape').not.toBeNull()
    expect(panel!.className, 'a real zero drew a failure or partial variant').toBe('ctl-empty')
    expect(panel!.querySelector('h3 > .ctl-mark.is-zero')?.textContent).toBe('real zero')
    expect(document.querySelector('.state'), 'the hand-built .state box is back').toBeNull()
    expect(
      panel!.querySelector('.ctl-empty-foot'),
      'a fresh empty state repeats the age the refresh control already carries',
    ).toBeNull()
    expect(document.querySelector('.c-refresh')?.textContent).toBe('⟳ 0 s')
  })

  /**
   * CH-10 and CP-21: THE LINK OUT. §6.9's shape ends in one, and `Screen`'s
   * `empty` prop had no slot for it, so thirteen screens could not follow the
   * rule however their authors wanted to.
   *
   * MUTATION: drop the link from the render. There is no link in the panel.
   */
  it('ends in the link out a screen gives it', async () => {
    render(
      <Screen<Rows>
        title="Holders"
        load={async () => ({ status: 'empty', fetchedAt: Date.now() })}
        empty={{
          heading: 'No unreleased leases',
          body: 'Across every tenant.',
          link: { href: '#capacity/pools', label: 'Pools' },
        }}
      >
        {(d) => <span>{d.rows.length}</span>}
      </Screen>,
    )
    const link = await screen.findByRole('link', { name: 'Pools' })
    expect(link.getAttribute('href')).toBe('#capacity/pools')
    expect(link.closest('.ctl-empty'), 'the link is not inside the empty state').not.toBeNull()
    expect(link.className).toContain('ctl-link')
  })

  it('cannot reach the row renderer at all', async () => {
    // The structural guarantee: `children` only ever receives DATA, and
    // `empty` carries none, so there is nothing to render rows from. A
    // component that counted rows here would be counting a value that does
    // not exist.
    const children = vi.fn(() => <div>rows</div>)
    render(
      <Screen<Rows> title="Agents" load={async () => ({ status: 'empty', fetchedAt: Date.now() })}>
        {children}
      </Screen>,
    )
    // The read has settled when the control carries an age; until then it
    // says `reading…`. No `empty` prop, so nothing else is drawn for it.
    await waitFor(() => expect(document.querySelector('.c-refresh')?.textContent).toBe('⟳ 0 s'))
    expect(body().textContent).not.toContain('rows')
    expect(children).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// error -- the rule this product exists for
// ---------------------------------------------------------------------------

const READ_FAILURES: ReadonlyArray<[string, ApiError]> = [
  ['a 500', err('server_error', { httpStatus: 500, code: null, message: 'Internal Server Error' })],
  ['a 503', err('upstream_degraded')],
  ['group resolution', err('tenant_unresolved')],
  ['an unreachable API', err('unreachable', { httpStatus: null, code: null, message: 'Failed to fetch' })],
  ['a disabled tenant', err('tenant_disabled', { httpStatus: 403 })],
  ['the wrong domain', err('wrong_domain', { httpStatus: 403 })],
  ['a conflict', err('conflict', { httpStatus: 409 })],
]

describe('a failed read', () => {
  it.each(READ_FAILURES)('%s renders no rows and no numbers about the platform', async (_name, error) => {
    renderScreen({ status: 'error', error })
    await screen.findByText(error.message)

    // THE RULE. Not "it shows an error" -- that is satisfied by a screen that
    // also prints "0 agents" beside it. NOTHING numeric about the platform may
    // appear. The HTTP status and the error code are provenance about the
    // REQUEST and are allowed through by name.
    const provenance = error.httpStatus === null ? [] : [`HTTP ${error.httpStatus}`]
    expectNoFigures(body(), provenance)
    expect(screen.queryByText('alpha')).toBeNull()
    expect(screen.queryByText('No agents are running')).toBeNull()
  })

  it.each(READ_FAILURES)('%s carries the sentence that says what the failure is NOT evidence of', async (_name, error) => {
    renderScreen({ status: 'error', error })
    await screen.findByText(error.message)
    expect(body().textContent).toMatch(/says nothing about what is running|belongs to another tenant/)
  })

  it('offers a retry, and the head does not pretend a read or a number is available', async () => {
    renderScreen({ status: 'error', error: err('server_error') })
    expect(await screen.findByRole('button', { name: 'Try again' })).toBeTruthy()
    // Nothing was read, so the head's control carries no age and no count:
    // `⟳ refresh`, named as not read.
    const ctl = screen.getByRole('button', { name: 'Refresh · not read' })
    expect(ctl.textContent).toBe('⟳ refresh')
    expect(document.querySelector('.c-count-note')).toBeNull()
  })

  it('a rate limit gets a countdown and NO retry button', async () => {
    // The server has just said how long to wait. A "Try again" button invites
    // somebody to hammer the wall they were told about, and every open tab
    // counts against the same 20 rps per principal.
    renderScreen({
      status: 'error',
      error: err('rate_limited', { httpStatus: 429, code: 'rate_limited', message: 'Slow down.', retryAfterSeconds: 11 }),
    })
    await screen.findByText('Slow down.')
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
    expect(body().textContent).toContain('11')
    expect(screen.getByRole('button', { name: /paused/ }).hasAttribute('disabled')).toBe(true)
  })

  it('an expired session offers a reload rather than a retry', async () => {
    renderScreen({ status: 'error', error: err('session_expired', { httpStatus: 200, code: null, message: 'Reload to sign in again.' }) })
    expect(await screen.findByRole('button', { name: 'Reload to sign in' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// admin_required -- a gate, not a failure
// ---------------------------------------------------------------------------

describe('admin_required', () => {
  it('is drawn as information and never as breakage', async () => {
    renderScreen({
      status: 'error',
      error: err('admin_required', { httpStatus: 403, code: 'forbidden', message: 'Admin group membership is required.' }),
    })

    const panel = await screen.findByRole('status')
    expect(panel.className).toContain('admin-gate')
    expect(panel.className).not.toContain('failed')
    expect(panel.textContent).toContain('Nothing is wrong with the platform, and nothing failed.')
    // The reassurance for a real failure would be a lie here.
    expect(body().textContent).not.toContain('This is a failure to read the platform')
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
    // An admin gate is not a read to renew: the head carries no refresh
    // control at all, and the panel says `admin only` (#138).
    expect(document.querySelector('.c-refresh'), 'a refresh was offered on an admin gate').toBeNull()
  })
})

// ---------------------------------------------------------------------------
// stale -- old data, still on screen, labelled
// ---------------------------------------------------------------------------

describe('stale', () => {
  it('keeps the rows, dims them, and says how old they are and why', async () => {
    const fetchedAt = Date.now() - 4 * 60 * 1000
    renderScreen({ status: 'stale', data: ROWS, fetchedAt, error: err('upstream_degraded', { message: 'The quota broker did not answer.' }) })

    expect(await screen.findByText('alpha')).toBeTruthy()
    const banner = screen.getByRole('status')
    expect(banner.textContent).toContain('showing older data')
    expect(banner.textContent).toContain('4m ago')
    expect(banner.textContent).toContain('The quota broker did not answer.')
    expect(body().textContent).toContain('not refreshed')

    // The dimming is the signal. Without the class the numbers read as live.
    const dimmed = document.querySelector('.stale-body')
    expect(dimmed).not.toBeNull()
    expect(dimmed?.textContent).toContain('alpha')
  })

  it('never renders a stale payload as though it had just been read', async () => {
    renderScreen({ status: 'stale', data: ROWS, fetchedAt: Date.now() - 60_000, error: err('unreachable', { httpStatus: null }) })
    await screen.findByText('alpha')
    // A bare age is the ok/empty wording. A stale screen must say `not
    // refreshed` before its age, and carry the stale class.
    const ctl = document.querySelector('.c-refresh')
    expect(ctl?.textContent).toBe('⟳ not refreshed · read 1 min ago')
    expect(ctl?.className).toBe('c-refresh is-stale')
  })

  it('a failed refresh after a good read becomes stale, not a blank screen', async () => {
    // The stale rule is owned by this component, not by each screen, which is
    // why it cannot be forgotten per-screen. Driven here through the real
    // retry control: first read ok, second read fails.
    let call = 0
    const load = async (): Promise<Result<Rows>> => {
      call += 1
      return call === 1
        ? { status: 'ok', data: ROWS, fetchedAt: Date.now() - 120_000 }
        : { status: 'error', error: err('upstream_degraded', { message: 'The second read failed.' }) }
    }

    render(
      <Screen<Rows> title="Agents" load={load} summary={(d) => <>{d.rows.length} rows</>}>
        {(d) => <ul>{d.rows.map((r) => <li key={r.name}>{r.name}</li>)}</ul>}
      </Screen>,
    )

    const refresh = await screen.findByRole('button', { name: /^Refresh · read / })
    refresh.click()

    await waitFor(() => expect(body().textContent).toContain('The second read failed.'))
    // The rows are STILL THERE. A blank screen here would be the platform's
    // defining bug in the other direction: data that exists, rendered as gone.
    expect(screen.getByText('alpha')).toBeTruthy()
    expect(document.querySelector('.stale-body')).not.toBeNull()
  })

  it('a genuine zero after a good read REPLACES the rows', async () => {
    // The mirror of the rule above, and it must not be collapsed into it:
    // keeping old rows when the platform truly has none is showing data that
    // is gone.
    let call = 0
    const load = async (): Promise<Result<Rows>> => {
      call += 1
      return call === 1
        ? { status: 'ok', data: ROWS, fetchedAt: Date.now() }
        : { status: 'empty', fetchedAt: Date.now() }
    }

    render(
      <Screen<Rows>
        title="Agents"
        load={load}
        empty={{ heading: 'No agents are running', body: 'The read succeeded and returned nothing.' }}
      >
        {(d) => <ul>{d.rows.map((r) => <li key={r.name}>{r.name}</li>)}</ul>}
      </Screen>,
    )

    const refresh = await screen.findByRole('button', { name: /^Refresh · read / })
    refresh.click()
    await waitFor(() => expect(screen.queryByText('alpha')).toBeNull())
    expect(screen.getByText('No agents are running')).toBeTruthy()
  })
})

// ---------------------------------------------------------------------------
// loading
// ---------------------------------------------------------------------------

describe('loading', () => {
  it('shows a skeleton with no numbers in it', async () => {
    // A skeleton that renders zeros is a screen that answers a question it has
    // not asked yet.
    render(
      <Screen<Rows> title="Agents" load={() => new Promise<Result<Rows>>(() => {})}>
        {(d) => <span>{d.rows.length}</span>}
      </Screen>,
    )
    await waitFor(() => expect(document.querySelector('.c-refresh')?.textContent).toBe('⟳ reading…'))
    expect(screen.getByRole('button', { name: 'Reading' }).hasAttribute('disabled')).toBe(true)
    expectNoFigures(body())
    expect(document.querySelectorAll('.skeleton').length).toBeGreaterThan(0)
  })
})

// ---------------------------------------------------------------------------
// The clock the two describes below run on
// ---------------------------------------------------------------------------

/**
 * A clock the test owns. `Date` is faked WITH the timers, so `Date.now()` and
 * every interval move together and an age is a function of how far the test
 * advanced rather than of how busy the machine was.
 */
function fakeClock(): void {
  vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

/** The head's refresh control, which carries the screen's one ticking age (#138, #98). */
const sub = (): string => document.querySelector('.c-refresh')?.textContent ?? ''

function rowsOf(d: Rows) {
  return (
    <ul>
      {d.rows.map((r) => (
        <li key={r.name}>{r.name}</li>
      ))}
    </ul>
  )
}

const okNow = (): Result<Rows> => ({ status: 'ok', data: ROWS, fetchedAt: Date.now() })

// ---------------------------------------------------------------------------
// CH-1 -- the age moves on its own
// ---------------------------------------------------------------------------

describe('the age on the head’s refresh control moves on its own (CH-1, #98)', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  /**
   * It was computed once, at render, and nothing re-rendered it: a screen left
   * open overnight still said `read just now` beside a head that said 9h. The
   * age lives on the refresh control now (#138), and the rule is the same.
   *
   * MUTATION: compute the control's age from `Date.now()` at render with no
   * clock driving a re-render. A minute later it still says `0 s`.
   */
  it('ticks the refresh control’s age without a new read', async () => {
    fakeClock()
    renderScreen(okNow())
    await advance(0)
    expect(sub()).toBe('⟳ 0 s')
    await advance(60_000)
    expect(sub()).toBe('⟳ 1 min')
    expect(screen.getByRole('button', { name: 'Refresh · read 1 min ago' })).toBeTruthy()
  })

  /**
   * An age that keeps counting is still only a number. Past the point the
   * screen trusts a read for, the data takes the stale treatment -- dimmed,
   * `not refreshed` -- which is what a reader notices without doing arithmetic.
   *
   * MUTATION: tick the age but never switch treatment. `.stale-body` never
   * appears and the refresh control never says `not refreshed`.
   */
  it('switches to the stale treatment once the read is older than the screen trusts', async () => {
    fakeClock()
    renderScreen(okNow())
    await advance(0)
    expect(document.querySelector('.stale-body')).toBeNull()
    expect(sub()).not.toContain('not refreshed')
    // Past the five minutes a read is trusted for (Shell.tsx), by one tick.
    await advance(AGED_AFTER_MS + 5_000)
    expect(sub()).toBe('⟳ not refreshed · read 5 min ago')
    expect(document.querySelector('.c-refresh')?.className).toBe('c-refresh is-stale')
    expect(document.querySelector('.stale-body')?.textContent).toContain('alpha')
  })

  /**
   * ONE CLOCK, NOT ONE PER COMPONENT. The head, the dock and every refresh
   * control read `useNow(5000)`, and two readers of one cadence get one instant --
   * which is what stops "22s" beside "just now" (CH-1). AG-2's inspector reads
   * the same hook.
   *
   * MUTATION: give each caller its own interval, started when it mounts. The
   * second reader, mounted 2s after the first, reads a different instant.
   */
  it('gives every reader of one cadence the same instant', async () => {
    fakeClock()
    const seen: Record<string, number> = {}
    function Reader({ name }: { name: string }) {
      seen[name] = useNow(5_000)
      return null
    }
    const first = render(<Reader name="head" />)
    await advance(2_000)
    render(<Reader name="sub" />)
    await advance(5_000)
    expect(seen.sub).toBe(seen.head)
    const before = seen.head!
    await advance(5_000)
    expect(seen.head).toBe(before + 5_000)
    expect(seen.sub).toBe(seen.head)
    first.unmount()
  })

  /**
   * CH-1 AND CH-10, THE EMPTY STATE'S FOOT, RE-POINTED (#98, owner ruling
   * 2026-10-07). The settlement on #87 had the foot tick `Checked …` on the
   * shared clock. The ruling makes a panel's foot state freshness ONLY WHEN
   * STALE: silent while fresh (the head's control carries that age), and
   * `from 6 min ago` once the read is older than `AGED_AFTER_MS`. It still
   * ticks on the same shared clock (`useNow`, `AGE_TICK_MS`) as the control,
   * so the two can never disagree.
   *
   * MUTATION: draw the foot at any age (it is there while fresh). MUTATION:
   * never draw it (an hour-old empty read is silent). MUTATION: compute it from
   * `Date.now()` at render, or from a clock of its own (it never appears, or
   * says a different age from the control above it).
   */
  it('keeps the empty state’s foot silent while fresh and ticks it once stale', async () => {
    fakeClock()
    renderScreen({ status: 'empty', fetchedAt: Date.now() })
    await advance(0)
    const foot = (): string | null => document.querySelector('.ctl-empty .ctl-empty-foot')?.textContent ?? null
    expect(foot(), 'a fresh empty state repeats the control’s age').toBeNull()
    await advance(60_000)
    expect(foot(), 'a one-minute-old empty read is not stale').toBeNull()
    expect(sub()).toBe('⟳ 1 min')
    // Past the five minutes a read is trusted for: the foot speaks.
    await advance(AGED_AFTER_MS)
    expect(foot()).toBe('from 6 min ago')
    expect(sub()).toBe('⟳ not refreshed · read 6 min ago')
    // An hour on, still the same instant as the control's.
    await advance(60 * 60_000)
    expect(foot()).toBe('from 1 h ago')
    expect(sub()).toBe('⟳ not refreshed · read 1 h ago')
  })

  /** MUTATION: leave StaleBanner reading its age once. It stays at `4m ago`. */
  it('ticks the stale banner age too', async () => {
    fakeClock()
    renderScreen({
      status: 'stale',
      data: ROWS,
      fetchedAt: Date.now() - 4 * 60_000,
      error: err('upstream_degraded', { message: 'The quota broker did not answer.' }),
    })
    await advance(0)
    expect(screen.getByRole('status').textContent).toContain('4m ago')
    await advance(60_000)
    expect(screen.getByRole('status').textContent).toContain('5m ago')
  })
})

// ---------------------------------------------------------------------------
// AG-1 -- a screen that polls
// ---------------------------------------------------------------------------

describe('a screen that polls (AG-1)', () => {
  afterEach(() => {
    vi.useRealTimers()
    // The instance property shadows jsdom's own getter; deleting it restores it.
    delete (document as unknown as { hidden?: boolean }).hidden
  })

  /**
   * The Agents list read once and never again, while its 1s clock went on
   * adding to rows nobody re-read: a finished agent read `running` and was
   * counted in Live. `pollMs` is the seam that screen sets.
   *
   * MUTATION: accept `pollMs` and schedule nothing. `load` is called once.
   */
  it('re-reads on its cadence and keeps the rows between reads', async () => {
    fakeClock()
    const load = vi.fn(async (): Promise<Result<Rows>> => okNow())
    render(
      <Screen<Rows> title="Agents" load={load} pollMs={5_000}>
        {rowsOf}
      </Screen>,
    )
    await advance(0)
    expect(load).toHaveBeenCalledTimes(1)
    await advance(5_000)
    expect(load).toHaveBeenCalledTimes(2)
    await advance(5_000)
    expect(load).toHaveBeenCalledTimes(3)
    // A poll never blanks the rows back to skeletons.
    expect(screen.getByText('alpha')).toBeTruthy()
    expect(document.querySelectorAll('.skeleton')).toHaveLength(0)
  })

  /** The cadence may depend on what was read: 5s while Live holds rows, 30s otherwise. */
  it('takes its cadence from the data when given a function', async () => {
    fakeClock()
    const load = vi.fn(async (): Promise<Result<Rows>> => okNow())
    render(
      <Screen<Rows>
        title="Agents"
        load={load}
        pollMs={(d) => (d !== null && d.rows.length > 5 ? 5_000 : 30_000)}
      >
        {rowsOf}
      </Screen>,
    )
    await advance(0)
    await advance(5_000)
    expect(load, 'polled at the busy cadence with two rows').toHaveBeenCalledTimes(1)
    await advance(25_000)
    expect(load).toHaveBeenCalledTimes(2)
  })

  /**
   * A background tab polling every 5s for eight hours is 5,760 requests
   * against a 20 rps per-principal budget for nothing
   * (docs/web-ui/03-agents-and-workflows.md §2.5).
   *
   * MUTATION: ignore `document.hidden`. The hidden tab goes on reading.
   * MUTATION: forget to resume. The returning tab never reads again.
   */
  it('stops while the tab is hidden and reads at once when it comes back', async () => {
    fakeClock()
    let hidden = false
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
    const load = vi.fn(async (): Promise<Result<Rows>> => okNow())
    render(
      <Screen<Rows> title="Agents" load={load} pollMs={5_000}>
        {rowsOf}
      </Screen>,
    )
    await advance(0)
    expect(load).toHaveBeenCalledTimes(1)

    hidden = true
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await advance(60_000)
    expect(load, 'a hidden tab went on polling').toHaveBeenCalledTimes(1)

    hidden = false
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await advance(0)
    expect(load, 'the tab came back and did not read').toHaveBeenCalledTimes(2)
  })

  /**
   * On repeated failure, back off rather than hammer -- and keep the rows, as
   * the stale rule already does for a manual refresh.
   *
   * MUTATION: re-read at the base cadence after a failure. The third read
   * lands at 5s instead of 10s.
   */
  it('backs off after a failed read instead of hammering, and keeps the rows', async () => {
    fakeClock()
    let call = 0
    const load = vi.fn(async (): Promise<Result<Rows>> => {
      call += 1
      return call === 1
        ? okNow()
        : { status: 'error', error: err('upstream_degraded', { message: 'Busy.' }) }
    })
    render(
      <Screen<Rows> title="Agents" load={load} pollMs={5_000}>
        {rowsOf}
      </Screen>,
    )
    await advance(0)
    await advance(5_000)
    expect(load).toHaveBeenCalledTimes(2)
    await advance(5_000)
    expect(load, 'read again at the base cadence right after a failure').toHaveBeenCalledTimes(2)
    await advance(5_000)
    expect(load).toHaveBeenCalledTimes(3)
    expect(screen.getByText('alpha')).toBeTruthy()
    expect(document.querySelector('.stale-body')).not.toBeNull()
  })

  /**
   * A POLL KEEPS AN EMPTY STATE ON SCREEN WHILE IT READS. The load effect put
   * the screen back to `loading` on every read whenever it held no rows, and an
   * empty read holds none -- so a tenant with no agents, polled on the 30s
   * cadence §2.5 asks for, had its empty panel swapped for skeleton rows and its
   * sub-line for `Reading…` on every poll, and then back.
   *
   * The second read never settles here, so what is asserted is the screen
   * DURING a poll, which is the moment the flash happened.
   *
   * MUTATION: reset to `loading` on any read made without rows in hand. The
   * empty panel is gone while the poll is in flight.
   */
  it('keeps an empty state on screen while a poll reads', async () => {
    fakeClock()
    let call = 0
    const load = vi.fn((): Promise<Result<Rows>> => {
      call += 1
      return call === 1
        ? Promise.resolve<Result<Rows>>({ status: 'empty', fetchedAt: Date.now() })
        : new Promise<Result<Rows>>(() => {})
    })
    render(
      <Screen<Rows>
        title="Agents"
        load={load}
        pollMs={5_000}
        empty={{ heading: 'No agents are running', body: 'The read succeeded and returned nothing.' }}
      >
        {rowsOf}
      </Screen>,
    )
    await advance(0)
    expect(document.querySelector('.ctl-empty')?.textContent).toContain('No agents are running')
    await advance(5_000)
    expect(load, 'the poll did not happen, so this proves nothing').toHaveBeenCalledTimes(2)
    expect(document.querySelector('.ctl-empty')?.textContent, 'the poll blanked the empty state').toContain(
      'No agents are running',
    )
    expect(document.querySelectorAll('.skeleton')).toHaveLength(0)
  })

  /**
   * AND A FAILED FIRST READ STAYS UP WHILE A BACK-OFF RETRY READS. With no
   * rows in hand the same reset unmounted the failure panel -- and its `Try
   * again` button -- on every retry, so a reader reaching for the button could
   * find skeletons under the pointer instead.
   *
   * MUTATION: as above. The panel and its button are gone during the retry.
   */
  it('keeps a failed read and its retry button on screen while a back-off retry reads', async () => {
    fakeClock()
    let call = 0
    const load = vi.fn((): Promise<Result<Rows>> => {
      call += 1
      return call === 1
        ? Promise.resolve<Result<Rows>>({ status: 'error', error: err('upstream_degraded', { message: 'Busy.' }) })
        : new Promise<Result<Rows>>(() => {})
    })
    render(
      <Screen<Rows> title="Agents" load={load} pollMs={5_000}>
        {rowsOf}
      </Screen>,
    )
    await advance(0)
    expect(document.querySelector('.state.failed')).not.toBeNull()
    // One failure in a row: the wait has doubled from 5s to 10s.
    await advance(10_000)
    expect(load, 'the retry did not happen, so this proves nothing').toHaveBeenCalledTimes(2)
    expect(document.querySelector('.state.failed'), 'the retry unmounted the failure panel').not.toBeNull()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
    expect(document.querySelectorAll('.skeleton')).toHaveLength(0)
  })

  /**
   * An admin gate or an expired session is not fixed by asking again, so a
   * polling screen stops asking. A person has to act first.
   */
  it('stops polling on an answer only a person can change', async () => {
    fakeClock()
    const load = vi.fn(
      async (): Promise<Result<Rows>> => ({
        status: 'error',
        error: err('admin_required', { httpStatus: 403, message: 'Admin group membership is required.' }),
      }),
    )
    render(
      <Screen<Rows> title="Agents" load={load} pollMs={5_000}>
        {rowsOf}
      </Screen>,
    )
    await advance(0)
    await advance(10 * 60_000)
    expect(load).toHaveBeenCalledTimes(1)
  })

  /** The cadence is shown where the age is, so a reader knows the age will move. */
  it('says its cadence beside the age', async () => {
    fakeClock()
    render(
      <Screen<Rows>
        title="Agents"
        load={async () => okNow()}
        summary={(d) => <>{d.rows.length} rows</>}
        pollMs={5_000}
      >
        {rowsOf}
      </Screen>,
    )
    await advance(0)
    expect(sub()).toBe('⟳ every 5 s · read 0 s ago')
    expect(screen.getByRole('button', { name: 'Refresh · read 0 s ago · re-reads every 5 s' })).toBeTruthy()
  })

  /**
   * What a screen that ticks its own clock over the rows needs to know: when
   * those rows were read, and how often they are meant to be. Agents stops its
   * 1s clock once the data is older than one interval (AG-1's other half).
   */
  it('tells its children when the rows were read and at what cadence', async () => {
    fakeClock()
    const seen: ScreenReading[] = []
    const t0 = Date.now()
    render(
      <Screen<Rows> title="Agents" load={async () => okNow()} pollMs={5_000}>
        {(d, reading) => {
          seen.push(reading)
          return rowsOf(d)
        }}
      </Screen>,
    )
    await advance(0)
    const last = seen[seen.length - 1]
    expect(last?.fetchedAt).toBe(t0)
    expect(last?.pollMs).toBe(5_000)
  })

  /** The back-off doubles, and stops doubling at the cap. */
  it('doubles its wait per failure in a row, up to five minutes', () => {
    expect(nextPollDelay(5_000, 0)).toBe(5_000)
    expect(nextPollDelay(5_000, 1)).toBe(10_000)
    expect(nextPollDelay(5_000, 3)).toBe(40_000)
    expect(nextPollDelay(5_000, 20)).toBe(MAX_BACKOFF_MS)
    // A screen that already polls slower than the cap is never made FASTER by
    // failing: its own cadence is the floor.
    expect(nextPollDelay(10 * 60_000, 2)).toBe(10 * 60_000)
  })

  /** And a screen that was not asked to poll reads exactly once. */
  it('does not poll unless asked', async () => {
    fakeClock()
    const load = vi.fn(async (): Promise<Result<Rows>> => okNow())
    render(
      <Screen<Rows> title="Agents" load={load}>
        {rowsOf}
      </Screen>,
    )
    await advance(0)
    await advance(10 * 60_000)
    expect(load).toHaveBeenCalledTimes(1)
    // Read once and never again: the control says its age and no cadence.
    expect(sub()).toMatch(/^⟳ (not refreshed · read )?\d+ (s|min)( ago)?$/)
    expect(sub()).not.toContain('every')
  })
})
