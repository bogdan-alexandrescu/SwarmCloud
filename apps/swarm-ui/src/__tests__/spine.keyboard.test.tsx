/**
 * THE SPINE'S KEYBOARD CONTRACT.
 *
 * Collapsed, focusing a section button opens a flyout of its pages. It is a
 * plain labelled group (not role="menu", which would promise arrow keys), it
 * closes when focus leaves the spine and the flyout, and Escape closes it and
 * returns focus to the section button. The phone drawer takes focus on open,
 * keeps Tab inside, closes on Escape and returns focus to its opener.
 *
 * MUTATION: drop the spine's onBlur / onKeyDown (flyout cases) or the drawer
 * effect (drawer cases) and the case that names it goes red.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render } from '@testing-library/react'

import { SkyShell } from '../Spine'

beforeEach(() => {
  vi.useFakeTimers()
  try {
    window.localStorage.setItem('swarm.shell.collapsed', '1')
  } catch {
    /* storage may be unavailable; the collapsed cases skip themselves below */
  }
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  try {
    window.localStorage.clear()
  } catch {
    /* ignore */
  }
})

function shell() {
  return render(
    <SkyShell section="overview" tab="now" title="Overview" go={() => {}} foot={null}>
      <button type="button">page</button>
    </SkyShell>,
  )
}

function sectionButton(c: HTMLElement, key: string): HTMLElement {
  return c.querySelector<HTMLElement>(`.sk-spine [data-sec="${key}"]`)!
}

describe('the collapsed spine flyout', () => {
  function collapsed(): HTMLElement {
    const { container } = shell()
    expect(container.querySelector('.sk-app.is-collapsed'), 'the spine did not start collapsed').not.toBeNull()
    return container
  }

  it('opens as a labelled group, not a menu', () => {
    const c = collapsed()
    const btn = sectionButton(c, 'work')
    act(() => btn.focus())
    act(() => void vi.advanceTimersByTime(100))
    const fly = c.querySelector('.sk-flyout')
    expect(fly).not.toBeNull()
    expect(fly!.getAttribute('role')).toBe('group')
    expect(fly!.getAttribute('aria-label')).toBe('Work pages')
    expect(c.querySelector('[role="menu"], [role="menuitem"]')).toBeNull()
  })

  it('closes on Escape and returns focus to its section button', () => {
    const c = collapsed()
    const btn = sectionButton(c, 'work')
    act(() => btn.focus())
    act(() => void vi.advanceTimersByTime(100))
    const inside = c.querySelector<HTMLElement>('.sk-flyout a')!
    act(() => inside.focus())
    fireEvent.keyDown(inside, { key: 'Escape' })
    expect(c.querySelector('.sk-flyout')).toBeNull()
    expect(document.activeElement).toBe(btn)
  })

  it('closes when focus leaves the spine and the flyout', () => {
    const c = collapsed()
    const btn = sectionButton(c, 'work')
    act(() => btn.focus())
    act(() => void vi.advanceTimersByTime(100))
    expect(c.querySelector('.sk-flyout')).not.toBeNull()
    const page = c.querySelector<HTMLElement>('.sk-main button')!
    act(() => page.focus())
    expect(c.querySelector('.sk-flyout')).toBeNull()
  })

  it('stays open while focus moves into the flyout', () => {
    const c = collapsed()
    const btn = sectionButton(c, 'work')
    act(() => btn.focus())
    act(() => void vi.advanceTimersByTime(100))
    const inside = c.querySelector<HTMLElement>('.sk-flyout a')!
    act(() => inside.focus())
    expect(c.querySelector('.sk-flyout')).not.toBeNull()
  })
})

describe('the phone menu drawer', () => {
  function openDrawer() {
    const { container } = shell()
    const opener = container.querySelector<HTMLButtonElement>('button[aria-label="Open the menu"]')!
    act(() => opener.focus())
    fireEvent.click(opener)
    return { c: container, opener }
  }

  it('moves focus inside on open', () => {
    const { c } = openDrawer()
    expect(c.querySelector('.sk-side')!.contains(document.activeElement)).toBe(true)
  })

  it('wraps Tab at both ends', () => {
    const { c } = openDrawer()
    const items = Array.from(
      c.querySelectorAll<HTMLElement>('.sk-side a[href], .sk-side button:not([disabled]), .sk-side input:not([disabled])'),
    )
    const first = items[0]!
    const last = items[items.length - 1]!
    act(() => last.focus())
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(document.activeElement).toBe(first)
    act(() => first.focus())
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(last)
  })

  it('closes on Escape and returns focus to the opener', () => {
    const { c, opener } = openDrawer()
    expect(c.querySelector('.has-drawer')).not.toBeNull()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(c.querySelector('.has-drawer')).toBeNull()
    expect(document.activeElement).toBe(opener)
  })
})
