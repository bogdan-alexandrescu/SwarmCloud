// QA1-FIX-38 (automate-25, part of #2596): a screen whose read was ANSWERED
// -- a 404 or a 403 -- offers no `⟳ refresh` in its head, because the panel
// under it says "Reading it again gets the same answer". A failure that a
// re-read can cure keeps it.

import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Screen } from '../Shell'
import type { ApiError, ApiErrorKind } from '../fetch'

function err(kind: ApiErrorKind, httpStatus: number): ApiError {
  return { kind, httpStatus, code: null, message: `The read answered ${httpStatus}.` }
}

function mount(error: ApiError) {
  return render(
    <Screen<{ n: number }> title="New schedule" load={async () => ({ status: 'error', error })}>
      {(d) => <p>{d.n}</p>}
    </Screen>,
  )
}

describe('QA1 automate-25: no refresh over an answer', () => {
  it.each([
    ['a 404 on a list read (not served)', { ...err('not_found', 404), template: '/v1/schedule-types' }],
    ['a 404 on an item read', { ...err('not_found', 404), template: '/v1/schedules/{id}' }],
    ['a 403', err('forbidden', 403)],
  ])('%s: the head offers no refresh', async (_name, error) => {
    mount(error)
    await screen.findByText(error.message)
    if (error.kind === 'not_found') expect(document.body.textContent).toContain('Reading it again gets the same answer')
    // MUTATION: test only `admin_required` in the head's guard and `⟳ refresh` sits over the answer.
    expect(screen.queryByRole('button', { name: /refresh/i })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
  })

  it('keeps the refresh where reading again can help', async () => {
    mount(err('server_error', 503))
    await screen.findByText('The read answered 503.')
    expect(screen.getByRole('button', { name: 'Refresh · not read' })).toBeTruthy()
  })
})
