/**
 * THE APP ACTUALLY MOUNTS WHAT IT WAS BUILT TO MOUNT.
 *
 * A verifier found on 2026-09-22 that deleting `<ProductHeader />` from
 * App.tsx left the entire suite green -- 222 of 222. The header is the whole
 * deliverable of B30, it was tested exhaustively in isolation, and the one
 * fact nobody asserted was that the product renders it. A component can be
 * perfect and unreachable.
 *
 * The same hole existed for the help affordance and the environment badge, so
 * this file guards the mounting of each rather than the component behind it --
 * those have their own tests. The assertion is deliberately shallow: it says
 * "this is on the page", not "this looks right".
 */
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'

import { App } from '../App'

describe('the shell mounts the frame it was built with', () => {
  it('renders the product header, not just a nav row', () => {
    render(<App />)
    // Deleting <ProductHeader/> from App.tsx must turn this red. Before this
    // test it did not.
    expect(document.querySelector('.brand')).not.toBeNull()
  })

  it('names the environment somewhere a reader will see it', () => {
    render(<App />)
    const header = document.querySelector('.brand-row, .brand')
    expect(header).not.toBeNull()
    // Every treatment classifyEnvironment can produce. The guarantee is that
    // the frame NAMES the environment -- an operator must be able to tell which
    // one they are about to change something in without reading a URL. It is
    // deliberately not asserting WHICH: that is classifyEnvironment's own test.
    expect(header?.textContent ?? '').toMatch(/local|dev|staging|prod|unknown/i)
  })

  it('offers the help affordance from the frame', () => {
    render(<App />)
    const help = document.querySelector('[href*="#help"], [aria-label*="elp"]')
    expect(help, 'the Help destination must be reachable from the shell').not.toBeNull()
  })
})
