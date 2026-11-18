// THE INTERFACE CRASHED, IN THE TAKEOVER SHAPE (states.html C, §14).
//
// The one app-wide state left. It already said the right thing; it now says
// it in the shape every whole-app state takes -- the cause, the line that
// this says nothing about the platform, and one action. MUTATION: put the
// `.state failed` box back, or drop the reassurance.
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ErrorBoundary } from '../ErrorBoundary'

function Boom(): never {
  throw new TypeError("Cannot read properties of undefined (reading 'steps')")
}

describe('the crashed interface', () => {
  it('takes over with the cause, the reassurance and Reload', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const { container } = render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    )
    const panel = container.querySelector('.app-takeover')!
    expect(panel).not.toBeNull()
    expect(panel.getAttribute('role')).toBe('alert')
    expect(screen.getByRole('heading', { name: 'The interface crashed' })).toBeTruthy()
    expect(panel.textContent).toContain('Whatever is running is still running.')
    expect(screen.getByRole('button', { name: 'Reload' }).className).toContain('c-btn is-primary')
    expect(panel.textContent).toContain("reading 'steps'")
    expect(container.querySelector('.state.failed')).toBeNull()
  })
})
