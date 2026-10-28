/**
 * QA1-FIX-34 (console visual QA pass 1, part of #2596): the cancel dialog on a
 * workflow's page, dark phone-390, from the ovag sweep lane on the fixture
 * console.
 *
 *   overview-agents-86  the confirm button, disabled until the id is typed,
 *                       read as enabled, and the dark scrim barely dimmed the
 *                       page behind it.
 *   overview-agents-87  Escape closed the dialog but focus landed on the step
 *                       panel (`aside.wf-panel`), not the "Cancel workflow"
 *                       button that opened it.
 */
import STYLES from '../styles.css?raw'
import COMPONENTS_CSS from '../styles/components.css?raw'
import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'

import { Button, Dialog, TypedConfirm } from '../components'

/** The declarations of the first rule whose selector list is exactly `selector`. */
function rule(css: string, selector: string): string {
  const flat = css.replace(/\/\*[\s\S]*?\*\//g, '')
  const re = /([^{}]+)\{([^{}]*)\}/g
  for (let m = re.exec(flat); m !== null; m = re.exec(flat)) {
    const sels = m[1]!.split(',').map((s) => s.trim())
    if (sels.includes(selector)) return m[2]!
  }
  return ''
}

function token(name: string): string {
  // The first (dark, `:root`) definition of a token.
  const m = new RegExp(`${name}:\\s*(#[0-9a-fA-F]{3,8})`).exec(STYLES)
  return m === null ? '' : m[1]!.toLowerCase()
}

describe('overview-agents-86: the cancel dialog in dark', () => {
  it('the dark scrim is not drawn in the dark page colour, so it dims the page', () => {
    // MUTATION: put `.c-scrim` back to `color-mix(in srgb, #06101e 42%,
    // transparent)` for every theme: #06101e IS the dark `--bg`, so over the
    // page it changed nothing and over a card it barely darkened it.
    const bg = token('--bg')
    expect(bg).toBe('#06101e')
    const dark = rule(COMPONENTS_CSS, '.c-scrim')
    expect(dark).toMatch(/background:/)
    expect(dark.toLowerCase()).not.toContain(bg)
    expect(dark).toMatch(/background:\s*rgb\(0 0 0 \/ \.6\)/)
    // Light keeps the navy wash it had: over a white page that one did dim.
    const light = rule(COMPONENTS_CSS, ":root[data-theme='light'] .c-scrim")
    expect(light).toMatch(/background:\s*color-mix\(in srgb, #06101e 42%, transparent\)/)
    const media = /@media \(prefers-color-scheme: light\)\s*\{([^{}]*\{[^{}]*\})\s*\}/g
    const blocks = [...COMPONENTS_CSS.matchAll(media)].map((m) => m[1]!)
    expect(blocks.some((b) => rule(b, ":root:not([data-theme='dark']) .c-scrim").includes('#06101e 42%'))).toBe(true)
  })

  it('the confirm is disabled until the id is typed, and a disabled filled-danger button is drawn pale, not red', () => {
    // Not reproduced on main: `.c-btn.is-danger-filled:disabled` (components.css,
    // V138) already pales the fill to `--s-badb` (#3a1512 in dark) with the
    // label in `--text-dim`. Pinned here so the confirm keeps using it.
    // MUTATION: drop `disabled={!match}` from TypedConfirm's verb button, or
    // delete the `.c-btn.is-danger-filled:disabled` rule.
    render(
      <TypedConfirm title="Cancel this workflow?" name="wf_demo" verb="Cancel the workflow" keep="Keep it running" onConfirm={() => {}} onClose={() => {}}>
        <p>1 running step is asked to stop at its next heartbeat.</p>
      </TypedConfirm>,
    )
    const verb = screen.getByRole('button', { name: 'Cancel the workflow' })
    expect(verb).toHaveProperty('disabled', true)
    expect(verb.className).toContain('is-danger-filled')
    const off = rule(COMPONENTS_CSS, '.c-btn.is-danger-filled:disabled')
    expect(off).toMatch(/background:\s*var\(--s-badb\)/)
    expect(off).toMatch(/color:\s*var\(--text-dim\)/)
    expect(token('--s-badb')).not.toBe(token('--s-bad'))
  })
})

describe('overview-agents-87: focus returns to the button that opened the dialog', () => {
  function Page() {
    const [open, setOpen] = useState(false)
    return (
      <>
        <Button kind="danger" onClick={() => setOpen(true)}>
          Cancel workflow
        </Button>
        <aside className="wf-panel" tabIndex={-1} aria-label="Step details">
          <p>The running step.</p>
        </aside>
        {open && (
          <Dialog title="Cancel this workflow?" onClose={() => setOpen(false)} actions={<Button kind="ghost">Keep it running</Button>}>
            <p>1 running step is asked to stop.</p>
          </Dialog>
        )}
      </>
    )
  }

  it('when the press did not move focus (a tap, a scripted click), Escape still lands on the opener, not the step panel', () => {
    // The step panel takes focus when it opens (StepPanel, Workflows.tsx). A
    // press that does not focus the button it lands on -- WebKit's click, a
    // touch, an `element.click()` -- left the panel as `activeElement`, so the
    // dialog remembered the PANEL as its opener and gave focus back there.
    // MUTATION: drop the pressed-control record in Overlay.tsx and take
    // `document.activeElement` alone as the opener again.
    render(<Page />)
    const panel = screen.getByRole('complementary', { name: 'Step details' })
    panel.focus()
    const opener = screen.getByRole('button', { name: 'Cancel workflow' })
    fireEvent.click(opener) // fireEvent moves no focus, like a tap
    expect(document.activeElement).not.toBe(panel)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(opener)
  })

  it('a focused opener is still the one focus returns to', () => {
    render(<Page />)
    const opener = screen.getByRole('button', { name: 'Cancel workflow' })
    opener.focus()
    fireEvent.click(opener)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(document.activeElement).toBe(opener)
  })

  it('a press long before the dialog opened is not taken for its opener', async () => {
    // MUTATION: never forget the pressed control, and a dialog opened later
    // by a key would send focus to whatever was clicked last instead of back
    // where the reader was.
    function Late() {
      const [open, setOpen] = useState(false)
      return (
        <>
          <button type="button">Elsewhere</button>
          <aside
            tabIndex={-1}
            aria-label="Step details"
            onKeyDown={(e) => {
              if (e.key === 'c') setOpen(true)
            }}
          >
            <p>The running step.</p>
          </aside>
          {open && (
            <Dialog title="Later" onClose={() => setOpen(false)} actions={<Button kind="ghost">Close</Button>}>
              <p>Opened by a key.</p>
            </Dialog>
          )}
        </>
      )
    }
    render(<Late />)
    fireEvent.click(screen.getByRole('button', { name: 'Elsewhere' }))
    await new Promise((r) => setTimeout(r, 5))
    const panel = screen.getByRole('complementary', { name: 'Step details' })
    panel.focus()
    fireEvent.keyDown(panel, { key: 'c' })
    expect(screen.getByRole('dialog', { name: 'Later' })).toBeTruthy()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(document.activeElement).toBe(panel)
  })
})
