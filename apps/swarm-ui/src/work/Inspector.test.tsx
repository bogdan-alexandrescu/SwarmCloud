import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'

import {
  FOLD_BELOW,
  INSPECTOR_INITIAL,
  INSPECTOR_MAX_FALLBACK,
  INSPECTOR_MIN,
  Inspector,
  KEY_STEP,
  MAIN_MIN,
  HANDLE_W,
  inspectorMax,
  inspectorSpec,
  startsFolded,
  widthClassOf,
  type WidthClass,
} from './Inspector'

// jsdom has no PointerEvent, and without one a pointer event carries no clientX.
if (typeof window.PointerEvent === 'undefined') {
  class PointerEventShim extends MouseEvent {
    pointerId: number
    constructor(type: string, init: PointerEventInit = {}) {
      super(type, init)
      this.pointerId = init.pointerId ?? 0
    }
  }
  window.PointerEvent = PointerEventShim as unknown as typeof PointerEvent
}

const PAGE = 1440
// From the spec, not a literal: the screen's key is built per screen, and a per-browser pane width (kept on sign-out).
const KEY = inspectorSpec('agents').key

/** Every element measures as a page `PAGE` wide whose right edge is at `PAGE`: jsdom lays nothing out. */
function pageOf(width: number) {
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
    x: 0, y: 0, top: 0, left: 0, bottom: 800, right: width, width, height: 800, toJSON: () => ({}),
  } as DOMRect)
}

function phone(on: boolean) {
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: on && q.includes('720px'),
    media: q,
    addEventListener: () => {},
    removeEventListener: () => {},
  }))
}

function page(props: Partial<Parameters<typeof Inspector>[0]> = {}) {
  return (
    <Inspector screen="agents" main={<table><tbody><tr><td>row</td></tr></tbody></table>} title="build-contacts" state="Running" {...props}>
      {'children' in props ? props.children : <p>agent body</p>}
    </Inspector>
  )
}

const handle = () => screen.getByRole('separator', { name: 'Resize the inspector' })
const main = () => document.querySelector('.wi-main') as HTMLElement
const dockVar = () => (document.querySelector('.wi') as HTMLElement).style.getPropertyValue('--wi-dock')

beforeEach(() => {
  localStorage.clear()
  pageOf(PAGE)
  phone(false)
})
afterEach(() => localStorage.clear())

describe('the drag handle', () => {
  it('drags the inspector wider and narrower, within its minimum and its ceiling', () => {
    // The ceiling leaves the left pane MAIN_MIN on a narrow page and is INSPECTOR_MAX_FALLBACK on a wide one.
    expect(inspectorMax(1000)).toBe(1000 - MAIN_MIN - HANDLE_W)
    expect(inspectorMax(PAGE)).toBe(INSPECTOR_MAX_FALLBACK)
    expect(inspectorMax(0)).toBe(INSPECTOR_MAX_FALLBACK)
    expect(inspectorMax(400)).toBe(INSPECTOR_MIN)
    render(page())
    expect(handle().getAttribute('aria-valuenow')).toBe(String(INSPECTOR_INITIAL))
    fireEvent.pointerDown(handle(), { pointerId: 1, clientX: PAGE - INSPECTOR_INITIAL })
    fireEvent.pointerMove(handle(), { pointerId: 1, clientX: PAGE - 600 })
    expect(handle().getAttribute('aria-valuenow')).toBe('600')
    expect(dockVar()).toBe('600px')
    // Past the left edge: the inspector stops at its ceiling.
    fireEvent.pointerMove(handle(), { pointerId: 1, clientX: 10 })
    expect(handle().getAttribute('aria-valuenow')).toBe(String(inspectorMax(PAGE)))
    // Past the right edge: the inspector keeps its minimum.
    fireEvent.pointerMove(handle(), { pointerId: 1, clientX: PAGE + 50 })
    expect(handle().getAttribute('aria-valuenow')).toBe(String(INSPECTOR_MIN))
    fireEvent.pointerMove(handle(), { pointerId: 1, clientX: PAGE - 500 })
    fireEvent.pointerUp(handle(), { pointerId: 1, clientX: 0 })
    expect(handle().getAttribute('aria-valuenow')).toBe('500')
    expect(localStorage.getItem(KEY)).toBe('500')
    // A move after the release does nothing.
    fireEvent.pointerMove(handle(), { pointerId: 1, clientX: PAGE - 700 })
    expect(handle().getAttribute('aria-valuenow')).toBe('500')
  })

  it('moves on the keyboard: ArrowLeft widens, ArrowRight narrows, Home and End are the ends', () => {
    render(page())
    fireEvent.keyDown(handle(), { key: 'ArrowLeft' })
    expect(handle().getAttribute('aria-valuenow')).toBe(String(INSPECTOR_INITIAL + KEY_STEP))
    fireEvent.keyDown(handle(), { key: 'ArrowRight' })
    fireEvent.keyDown(handle(), { key: 'ArrowRight' })
    expect(handle().getAttribute('aria-valuenow')).toBe(String(INSPECTOR_INITIAL - KEY_STEP))
    fireEvent.keyDown(handle(), { key: 'Home' })
    expect(handle().getAttribute('aria-valuenow')).toBe(String(INSPECTOR_MIN))
    fireEvent.keyDown(handle(), { key: 'ArrowRight' })
    expect(handle().getAttribute('aria-valuenow')).toBe(String(INSPECTOR_MIN))
    fireEvent.keyDown(handle(), { key: 'End' })
    const max = inspectorMax(PAGE)
    expect(handle().getAttribute('aria-valuenow')).toBe(String(max))
    expect(handle().getAttribute('aria-valuemax')).toBe(String(max))
    fireEvent.keyDown(handle(), { key: 'ArrowLeft' })
    expect(handle().getAttribute('aria-valuenow')).toBe(String(max))
    expect(localStorage.getItem(KEY)).toBe(String(max))
  })
})

describe('the remembered width', () => {
  it('survives a remount, per screen', () => {
    const { unmount } = render(page())
    fireEvent.keyDown(handle(), { key: 'ArrowLeft' })
    unmount()
    render(page())
    expect(handle().getAttribute('aria-valuenow')).toBe(String(INSPECTOR_INITIAL + KEY_STEP))
    // Another screen keeps its own.
    const other = render(page({ screen: 'workflow' }))
    expect(other.container.querySelector('[role=separator]')?.getAttribute('aria-valuenow')).toBe(String(INSPECTOR_INITIAL))
  })

  it('a stored width out of range is clamped, and junk reads as the default', () => {
    localStorage.setItem(KEY, '9000')
    const a = render(page())
    expect(handle().getAttribute('aria-valuenow')).toBe(String(inspectorMax(PAGE)))
    a.unmount()
    localStorage.setItem(KEY, 'wide')
    render(page())
    expect(handle().getAttribute('aria-valuenow')).toBe(String(INSPECTOR_INITIAL))
  })

  it('a storage that throws loses the preference and nothing else', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError')
    })
    render(page())
    expect(handle().getAttribute('aria-valuenow')).toBe(String(INSPECTOR_INITIAL))
    fireEvent.keyDown(handle(), { key: 'ArrowLeft' })
    expect(handle().getAttribute('aria-valuenow')).toBe(String(INSPECTOR_INITIAL + KEY_STEP))
  })

  it('a localStorage getter that throws (a private window) renders the default', () => {
    const getter = vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => {
      throw new Error('SecurityError')
    })
    try {
      render(page())
      expect(handle().getAttribute('aria-valuenow')).toBe(String(INSPECTOR_INITIAL))
      fireEvent.keyDown(handle(), { key: 'End' })
      expect(handle().getAttribute('aria-valuenow')).toBe(String(inspectorMax(PAGE)))
    } finally {
      getter.mockRestore()
    }
  })
})

describe('fold to a rail and back', () => {
  it('folds from the header and unfolds from the rail, keeping its width', () => {
    const onFold = vi.fn()
    render(page({ onFold }))
    fireEvent.keyDown(handle(), { key: 'ArrowLeft' })
    fireEvent.click(screen.getByRole('button', { name: 'Fold the inspector' }))
    expect(onFold).toHaveBeenLastCalledWith(true)
    expect(screen.queryByRole('separator')).toBeNull()
    expect(screen.queryByText('agent body')).toBeNull()
    expect(document.querySelector('.wi')?.classList.contains('is-folded')).toBe(true)
    expect(document.querySelector('.wi-rail')?.textContent).toContain('build-contacts')
    // The left pane never moves: the same node before and after.
    const left = main()
    fireEvent.click(screen.getByRole('button', { name: 'Open the inspector' }))
    expect(onFold).toHaveBeenLastCalledWith(false)
    expect(main()).toBe(left)
    expect(screen.getByText('agent body')).toBeTruthy()
    expect(handle().getAttribute('aria-valuenow')).toBe(String(INSPECTOR_INITIAL + KEY_STEP))
  })

  it('starts folded when the page asks, and a page under FOLD_BELOW is the one that asks', () => {
    expect(startsFolded(1280)).toBe(true)
    expect(startsFolded(1440)).toBe(false)
    expect(startsFolded(FOLD_BELOW)).toBe(false)
    expect(startsFolded(0)).toBe(false)
    render(page({ startFolded: startsFolded(1280) }))
    expect(screen.getByRole('button', { name: 'Open the inspector' })).toBeTruthy()
    expect(screen.queryByText('agent body')).toBeNull()
  })

  it('with nothing to inspect the left pane takes the page: no handle, no rail', () => {
    render(page({ children: null }))
    expect(screen.queryByRole('separator')).toBeNull()
    expect(document.querySelector('.wi-rail')).toBeNull()
    expect(document.querySelector('.wi')?.classList.contains('is-alone')).toBe(true)
  })
})

describe('the header slot and close', () => {
  it('carries the title, the state and the buttons, and close calls onClose', () => {
    const onClose = vi.fn()
    render(page({ onClose, actions: <button type="button">Copy link</button>, label: 'Agent inspector' }))
    const dock = screen.getByRole('complementary', { name: 'Agent inspector' })
    expect(dock.querySelector('.wi-title-text')?.textContent).toBe('build-contacts')
    expect(dock.querySelector('.wi-state')?.textContent).toBe('Running')
    expect(screen.getByRole('button', { name: 'Copy link' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Close the inspector' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('has no close button without onClose', () => {
    render(page())
    expect(screen.queryByRole('button', { name: 'Close the inspector' })).toBeNull()
  })
})

describe('the phone sheet', () => {
  it('shows only the left pane until something is picked', () => {
    phone(true)
    render(page({ phoneOpen: false }))
    expect(screen.queryByText('agent body')).toBeNull()
    expect(screen.queryByRole('separator')).toBeNull()
    expect(document.querySelector('.wi-rail')).toBeNull()
  })

  it('opens as a full sheet with a back control; back and Escape both go back', () => {
    phone(true)
    const onBack = vi.fn()
    render(page({ phoneOpen: true, onBack, onClose: () => {}, backLabel: 'Agents' }))
    const sheet = screen.getByRole('dialog', { name: 'Inspector' })
    expect(sheet.textContent).toContain('agent body')
    expect(document.querySelector('.wi')?.classList.contains('is-sheet')).toBe(true)
    expect(screen.queryByRole('separator')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Fold the inspector' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Close the inspector' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '← Agents' }))
    expect(onBack).toHaveBeenCalledTimes(1)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onBack).toHaveBeenCalledTimes(2)
  })

  it('back falls back to close', () => {
    phone(true)
    const onClose = vi.fn()
    render(page({ phoneOpen: true, onClose }))
    fireEvent.click(screen.getByRole('button', { name: '← Back' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})

describe("the left pane's width class", () => {
  it('names the bounds the design drops columns at', () => {
    expect(widthClassOf(1000)).toBe('wide')
    expect(widthClassOf(701)).toBe('wide')
    expect(widthClassOf(700)).toBe('medium')
    expect(widthClassOf(561)).toBe('medium')
    expect(widthClassOf(560)).toBe('narrow')
    expect(widthClassOf(421)).toBe('narrow')
    expect(widthClassOf(420)).toBe('slim')
  })

  it('changes as the inspector widens, folds and unfolds, and tells the page', () => {
    const seen: WidthClass[] = []
    render(page({ onWidthClass: (c) => seen.push(c) }))
    // 1440 - 440 - 9 = 991.
    expect(main().dataset.width).toBe('wide')
    fireEvent.pointerDown(handle(), { pointerId: 1, clientX: PAGE - INSPECTOR_INITIAL })
    fireEvent.pointerMove(handle(), { pointerId: 1, clientX: PAGE - 800 }) // 631
    expect(main().dataset.width).toBe('medium')
    fireEvent.pointerMove(handle(), { pointerId: 1, clientX: PAGE - 950 }) // 481
    expect(main().dataset.width).toBe('narrow')
    fireEvent.pointerUp(handle(), { pointerId: 1 })
    fireEvent.click(screen.getByRole('button', { name: 'Fold the inspector' }))
    expect(main().dataset.width).toBe('wide')
    expect(seen).toEqual(['wide', 'medium', 'narrow', 'wide'])
  })

  it('at 1280 the inspector at its minimum leaves a wide pane; the page width is re-read on resize', () => {
    pageOf(1280)
    render(page())
    fireEvent.keyDown(handle(), { key: 'Home' })
    expect(main().dataset.width).toBe('wide') // 1280 - 340 - 9 = 931
    expect(inspectorMax(1280)).toBe(1280 - MAIN_MIN - HANDLE_W)
    pageOf(900)
    act(() => {
      window.dispatchEvent(new Event('resize'))
    })
    // 900 - 340 - 9 = 551, and the inspector's ceiling follows the page.
    expect(main().dataset.width).toBe('narrow')
    expect(handle().getAttribute('aria-valuemax')).toBe(String(inspectorMax(900)))
  })
})
