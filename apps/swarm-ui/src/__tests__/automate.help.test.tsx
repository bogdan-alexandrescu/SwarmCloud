// THE AUTOMATE PAGES' `?` (live QA 2026-10-11, AUT-13 and AUT-14).
//
// WHAT EACH CASE HOLDS:
//   * Schedules and Approvals each say their own topic under the section's
//     question, and `Help →` opens that topic, not the top of Help (AUT-14);
//   * a pinned card dismissed with Escape hands focus back to its `?` even
//     when the click that pinned it never focused the `?` -- Safari's click,
//     or an automation driver's (AUT-13, the ADM-12 pattern);
//   * a card that was only hovered open moves no focus when it closes.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { HELP } from '../help'

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  window.history.replaceState(null, '', '/')
})

async function openAt(path: string): Promise<HTMLButtonElement> {
  window.history.replaceState(null, '', path)
  const { App } = await import('../App')
  render(<App />)
  return await waitFor(() => {
    const g = document.querySelector<HTMLButtonElement>('main.work .ctl-q .ctl-q-glyph')
    expect(g).not.toBeNull()
    return g!
  })
}

describe('the Automate pages’ `?`', () => {
  for (const [path, topic] of [
    ['/automate/schedules', 'schedules'],
    ['/automate/approvals', 'approvals'],
  ] as const) {
    it(`on ${path}, says the ${topic} topic and links to it`, async () => {
      const glyph = await openAt(path)
      fireEvent.click(glyph)
      const card = await waitFor(() => {
        const c = document.querySelector<HTMLElement>('.ctl-q-card')
        expect(c).not.toBeNull()
        return c!
      })
      // MUTATION: drop `topic` from `SectionQuestion` and both pages draw the same card, linked to #help.
      expect(card.querySelector(`[data-topic="${topic}"]`)?.textContent).toBe(HELP[topic].short)
      expect(card.querySelector('a')!.getAttribute('href')).toBe(`#${HELP[topic].anchor}`)
    })
  }

  it('returns focus to the `?` when a pinned card is dismissed with Escape, though the click never focused it', async () => {
    const glyph = await openAt('/automate/schedules')
    // A click that does not move focus: the press lands, focus stays on <body>.
    ;(document.activeElement as HTMLElement | null)?.blur()
    fireEvent.click(glyph)
    await waitFor(() => expect(document.querySelector('.ctl-q-card')).not.toBeNull())
    expect(document.activeElement).toBe(document.body)
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(document.querySelector('.ctl-q-card')).toBeNull())
    // MUTATION: drop the `wasPinned && adrift` branch in `useCardBridge` and this is <body>.
    expect(document.activeElement).toBe(glyph)
    // Returning focus does not reopen the card it just closed.
    expect(document.querySelector('.ctl-q-card')).toBeNull()
  })

  it('moves no focus when a card that was only hovered open closes', async () => {
    const glyph = await openAt('/automate/schedules')
    ;(document.activeElement as HTMLElement | null)?.blur()
    const anchor = glyph.closest('.ctl-q')!
    fireEvent.pointerEnter(anchor)
    await waitFor(() => expect(document.querySelector('.ctl-q-card')).not.toBeNull(), { timeout: 2000 })
    fireEvent.pointerLeave(anchor)
    await waitFor(() => expect(document.querySelector('.ctl-q-card')).toBeNull(), { timeout: 2000 })
    expect(document.activeElement).toBe(document.body)
  })
})
