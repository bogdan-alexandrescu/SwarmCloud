// QA1B-09 (part of #2596): pass 1b applies QA1-FIX-23's outside-territory
// patches to Submit.tsx and styles/submit.css.
//
//   * runs-submit-21 (second half): the page head read "6 runner profiles ·
//     20 pools you are admitted against" -- the pool count is nothing the
//     person submitting chooses from. It says how many runners there are.
//   * The runner card's holder is no longer drawn with `.sbf-runner-holder`
//     (RunnerPicker.tsx names the pool in words), so the mono rule is gone.
//
// Each case is red on main and green with the change; the MUTATION line on
// each names the edit that turns it red again.

import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'

import { SubmitScreen } from '../Submit'
import SUBMIT_CSS from '../styles/submit.css?raw'

const WAIT = { timeout: 4000 }

describe('runs-submit-21: the page head counts the runners to choose from', () => {
  // MUTATION: put back "N runner profiles · M pools you are admitted against".
  it('says how many runners there are, not how many pools', async () => {
    const { container } = render(<SubmitScreen />)
    await screen.findByRole('button', { name: 'Start one agent' }, WAIT)
    const text = (container.textContent ?? '').replace(/\s+/g, ' ')
    expect(text).toMatch(/\d+ runners? to choose from/)
    expect(text).not.toContain('runner profiles ·')
    expect(text).not.toContain('pools you are admitted against')
  })
})

describe('the unused mono holder rule is removed', () => {
  // MUTATION: restore the `.sbf-runner-holder` rule in styles/submit.css.
  it('styles/submit.css no longer carries `.sbf-runner-holder`', () => {
    expect(SUBMIT_CSS).not.toContain('.sbf-runner-holder')
  })
})
