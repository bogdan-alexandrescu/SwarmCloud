/**
 * VISUAL QA PASS 2, runs2-10 (part of #2694): the fixture console's five
 * newest lone agents carry a prompt, so Submit's "Start from a recent one"
 * says what each did rather than `claude-code agent · <hex>` five times.
 */
import { render, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { SubmitChooser } from '../SubmitChooser'

const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

describe('runs2-10: each recent row says what it did', () => {
  it('names each of the five by the first line of its prompt', async () => {
    const { container } = render(<SubmitChooser go={() => {}} />)
    const recent = container.querySelector<HTMLElement>('.sb-recent')!
    await waitFor(() => expect(recent.querySelectorAll('.sb-recent-i').length).toBe(5), { timeout: 4000 })
    const names = [...recent.querySelectorAll('.sb-recent-n')].map(visible)
    // MUTATION: drop the five agents' `input.prompt` in `fixtureTaskRows`
    // and every name reads 'claude-code agent · <hex>'.
    for (const name of names) expect(name).not.toMatch(/^claude-code agent · [0-9a-f]{8}$/)
    expect(new Set(names).size).toBe(5)
  }, 20000)
})
