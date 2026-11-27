// A dialog's initial focus is its first action or its heading, never a help
// mark: focus opens the mark's tip, and the tip covered the buttons.
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Button } from '../components/Button'
import { Dialog } from '../components/Overlay'
import { InfoTip } from '../InfoTip'

describe('a dialog whose first control is a help mark', () => {
  it('focuses its first action, not the help mark, and leaves the tip closed', () => {
    render(
      <Dialog title="Run it now?" onClose={() => undefined} actions={<Button>Not now</Button>}>
        <p>
          One line <InfoTip about="Run now" tip="A long sentence behind the mark." />
        </p>
      </Dialog>,
    )
    expect(document.activeElement).not.toBe(screen.getByRole('button', { name: 'About Run now' }))
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Not now' }))
    expect(screen.getByRole('tooltip', { hidden: true }).hasAttribute('hidden')).toBe(true)
  })
})
