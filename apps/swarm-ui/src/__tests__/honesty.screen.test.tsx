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

import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

import { Screen } from '../Shell'
import type { ApiError, ApiErrorKind, Result } from '../fetch'
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
    // The sub-line is assembled from several text nodes, so it is read off the
    // element rather than matched as one string.
    expect(document.querySelector('.sub')?.textContent).toContain('2 rows')
    expect(screen.getByRole('button', { name: 'refresh' })).toBeTruthy()
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
    expect(screen.getByText(/Checked/)).toBeTruthy()
    expect(body().textContent).toContain('Nothing to show')
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
    await waitFor(() => expect(body().textContent).toContain('Nothing to show'))
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

  it('offers a retry, and the sub-line does not pretend a number is available', async () => {
    renderScreen({ status: 'error', error: err('server_error') })
    expect(await screen.findByRole('button', { name: 'Try again' })).toBeTruthy()
    expect(body().textContent).toContain('Could not read.')
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
    expect(body().textContent).toContain('Admin only.')
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
    // "read <age>" is the ok/empty wording. A stale screen must not use it.
    expect(body().textContent).not.toMatch(/\brows · read\b/)
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

    const refresh = await screen.findByRole('button', { name: 'refresh' })
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

    const refresh = await screen.findByRole('button', { name: 'refresh' })
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
    await waitFor(() => expect(body().textContent).toContain('Reading…'))
    expectNoFigures(body())
    expect(document.querySelectorAll('.skeleton').length).toBeGreaterThan(0)
  })
})
