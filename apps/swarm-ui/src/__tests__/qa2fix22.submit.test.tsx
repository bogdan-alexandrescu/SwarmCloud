/**
 * SUBMIT › START AN AGENT, VISUAL QA PASS 2 (QA2-FIX-22; part of #2694).
 *
 *   runs2-07  with claude-code chosen (a measured room of 0 in the fixture),
 *             the room was told three times: on the card, in the summary's
 *             "room for 0 more" and again as "Room for 0 now:" under Start.
 *             The summary says the room; the note under the button keeps
 *             what happens next (c09) without repeating the figure.
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SubmitScreen } from '../Submit'

const WAIT = { timeout: 4000 }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

afterEach(() => {
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('runs2-07: the room is said once, in the summary', () => {
  it('the note under Start does not repeat the room figure', async () => {
    const { container } = render(<SubmitScreen />)
    await screen.findByRole('button', { name: 'Start one agent' }, WAIT)
    fireEvent.click(container.querySelector<HTMLInputElement>('input[name="runner-profile"][value="claude-code"]')!)
    const send = container.querySelector<HTMLElement>('.sbf-send')!
    // The summary says it.
    expect(visible(send)).toMatch(/room for 0 more/)
    const wait = send.querySelector<HTMLElement>('p.sbf-wait')
    expect(visible(wait)).toBe('It is accepted and waits, holding nothing, until every pool it needs has room.')
    // MUTATION: put back "Room for 0 now:" at the head of the note -> fails.
    expect(visible(wait)).not.toMatch(/room for 0/i)
  })
})
