// U7 DIAGRAM-VIEW (docs/design/plans-pipeline.md §11, §6.6): a plan's diagram
// source is untrusted agent output, drawn so that nothing in it can run.
//
// Mermaid's parser runs for real here (it needs no layout); only `render` is
// replaced, because jsdom has no `getBBox` to lay a diagram out with. The
// replacement returns an SVG carrying every construct the sanitiser must take
// out, so the test proves the image the page shows holds none of them.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import mermaid from 'mermaid'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'

import DiagramView from '../DiagramView'

const FLOW = ['flowchart LR', '  A[Plan] -->|approve| B[Build]', '  B --> C[Review]', '  C -->|changes| A', '  C --> D[Merge]'].join('\n')

/** What an unsanitised renderer could hand back. `fill` names the theme's background, so a theme switch shows in the image. */
function hostileSvg(fill: string): string {
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 100 40" onload="alert(1)">` +
    `<script>alert(2)</script>` +
    `<rect width="100" height="40" fill="${fill}" onclick="alert(3)"/>` +
    `<foreignObject width="10" height="10"><div>html</div></foreignObject>` +
    `<a href="javascript:alert(4)"><text>link</text></a>` +
    `<a xlink:href="https://example.invalid/x"><text>xlink</text></a>` +
    `<image href="https://example.invalid/track.png" width="1" height="1"/>` +
    `<use href="https://example.invalid/sprite.svg#x"/>` +
    `<path d="M0 0L10 10" marker-end="url(#arrow)" style="fill:url(https://example.invalid/p)"/>` +
    `<text x="4" y="20" onmouseover="alert(5)">Plan</text>` +
    `</svg>`
  )
}

let renderSpy: MockInstance<typeof mermaid.render>
let lastBackground = ''

beforeEach(() => {
  lastBackground = ''
  const init = mermaid.initialize.bind(mermaid)
  vi.spyOn(mermaid, 'initialize').mockImplementation((config) => {
    lastBackground = String(config.themeVariables?.background ?? '')
    init(config)
  })
  renderSpy = vi.spyOn(mermaid, 'render').mockImplementation(async (id: string) => ({
    svg: hostileSvg(lastBackground),
    diagramType: 'flowchart-v2',
    bindFunctions: undefined,
    id,
  }) as unknown as Awaited<ReturnType<typeof mermaid.render>>)
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function decodedSvg(img: HTMLImageElement): string {
  const src = img.getAttribute('src') ?? ''
  expect(src.startsWith('data:image/svg+xml')).toBe(true)
  return decodeURIComponent(src.slice(src.indexOf(',') + 1))
}

describe('a source that could run something is shown as source, never rendered', () => {
  const cases: [string, string, RegExp][] = [
    ['a click handler', 'flowchart LR\n  A --> B\n  click A call alert()', /click/i],
    ['an init directive', '%%{init: {"securityLevel": "loose"}}%%\nflowchart LR\n  A --> B', /directive/i],
    ['an HTML label', 'flowchart LR\n  A["<img src=x onerror=alert(1)>"] --> B', /HTML/i],
    ['an actor link menu', 'sequenceDiagram\n  participant A\n  link A: Docs @ https://example.invalid/\n  A->>A: hi', /link/i],
  ]
  for (const [what, source, reason] of cases) {
    it(`refuses ${what}, naming the reason and the line`, async () => {
      const { container } = render(<DiagramView source={source} title="Flow" theme="light" />)
      const failure = await screen.findByTestId('dg-failed')
      expect(failure.textContent).toMatch(reason)
      expect(failure.textContent).toMatch(/line \d+/)
      // the source, in full, in a code block
      for (const line of source.split('\n')) expect(container.querySelector('pre')?.textContent).toContain(line)
      expect(container.querySelector('img')).toBeNull()
      expect(renderSpy).not.toHaveBeenCalled()
    })
  }
})

describe('a parse failure is shown, never a blank', () => {
  it("shows the parser's reason and the source", async () => {
    const source = 'flowchart LR\n  A -->'
    const { container } = render(<DiagramView source={source} title="Broken" theme="light" />)
    const failure = await screen.findByTestId('dg-failed')
    expect(failure.textContent).toMatch(/did not render/i)
    expect(failure.textContent).toMatch(/line \d+/i)
    expect(failure.textContent).toMatch(/expecting/i)
    expect(container.querySelector('pre')?.textContent).toContain('A -->')
    expect(container.querySelector('img')).toBeNull()
    expect(renderSpy).not.toHaveBeenCalled()
  })

  it('shows a source over the edge cap as a failure with its count', async () => {
    const lines = ['flowchart LR']
    for (let i = 0; i < 101; i++) lines.push(`  n${i % 50} --> n${(i + 1) % 50}`)
    render(<DiagramView source={lines.join('\n')} title="Too big" theme="light" />)
    const failure = await screen.findByTestId('dg-failed')
    expect(failure.textContent).toMatch(/101 edges/)
    expect(renderSpy).not.toHaveBeenCalled()
  })
})

describe('the drawing', () => {
  it('is an <img> whose SVG holds no script, foreignObject, a or on* attribute', async () => {
    render(<DiagramView source={FLOW} title="Release flow" why="How a plan reaches main" theme="light" />)
    const img = (await screen.findByRole('img', { name: /Release flow/ })) as HTMLImageElement
    expect(img.tagName).toBe('IMG')
    expect(img.getAttribute('alt')).toContain('How a plan reaches main')
    const svg = decodedSvg(img)
    const doc = new DOMParser().parseFromString(svg, 'image/svg+xml')
    expect(doc.querySelector('parsererror')).toBeNull()
    expect(doc.documentElement.localName).toBe('svg')
    for (const tag of ['script', 'foreignObject', 'a', 'image', 'use']) expect(doc.getElementsByTagName(tag)).toHaveLength(0)
    for (const el of Array.from(doc.getElementsByTagName('*'))) {
      for (const attr of Array.from(el.attributes)) {
        expect(attr.name.toLowerCase().startsWith('on')).toBe(false)
        expect(['href', 'xlink:href']).not.toContain(attr.name.toLowerCase())
        expect(attr.value).not.toMatch(/url\(\s*(?!#)/i)
      }
    }
    expect(svg).not.toMatch(/alert\(|javascript:|example\.invalid/)
    // the drawing itself survives: the local marker and the label
    expect(svg).toContain('url(#arrow)')
    expect(svg).toContain('Plan')
    // rendered strictly
    const config = vi.mocked(mermaid.initialize).mock.calls.at(-1)?.[0]
    expect(config?.securityLevel).toBe('strict')
    expect(config?.htmlLabels).toBe(false)
    expect(config?.secure).toEqual(expect.arrayContaining(['securityLevel', 'theme', 'themeVariables', 'htmlLabels', 'secure']))
  })

  it('switching the theme changes the image, and switching back reuses the first drawing', async () => {
    const { rerender } = render(<DiagramView source={FLOW} title="Release flow" theme="light" />)
    const light = decodedSvg((await screen.findByRole('img', { name: /Release flow/ })) as HTMLImageElement)
    rerender(<DiagramView source={FLOW} title="Release flow" theme="dark" />)
    await waitFor(() => expect(decodedSvg(screen.getByRole('img', { name: /Release flow/ }) as HTMLImageElement)).not.toBe(light))
    const calls = renderSpy.mock.calls.length
    rerender(<DiagramView source={FLOW} title="Release flow" theme="light" />)
    await waitFor(() => expect(decodedSvg(screen.getByRole('img', { name: /Release flow/ }) as HTMLImageElement)).toBe(light))
    expect(renderSpy.mock.calls.length).toBe(calls)
  })
})

describe('the text outline', () => {
  it('lists every edge of the flowchart, with its label', async () => {
    render(<DiagramView source={FLOW} title="Release flow" theme="light" />)
    await screen.findByRole('img', { name: /Release flow/ })
    const outline = screen.getByTestId('dg-outline')
    const items = Array.from(outline.querySelectorAll('li')).map((li) => li.textContent)
    expect(items).toEqual(['Plan → Build, approve', 'Build → Review', 'Review → Plan, changes', 'Review → Merge'])
  })

  it('lists each message of a sequence diagram in order', async () => {
    const seq = 'sequenceDiagram\n  participant P as Planner\n  P->>W: plan\n  W-->>P: done'
    render(<DiagramView source={seq} title="Hand-off" theme="light" />)
    await screen.findByRole('img', { name: /Hand-off/ })
    const items = Array.from(screen.getByTestId('dg-outline').querySelectorAll('li')).map((li) => li.textContent)
    expect(items).toEqual(['Planner → W: plan', 'W → Planner: done'])
  })
})

describe('export', () => {
  it('copies the source', async () => {
    const writeText = vi.fn(async () => undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    render(<DiagramView source={FLOW} title="Release flow" theme="light" />)
    await screen.findByRole('img', { name: /Release flow/ })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /copy the source/i }))
    })
    expect(writeText).toHaveBeenCalledWith(FLOW)
    expect(await screen.findByText(/copied/i)).toBeTruthy()
  })

  it('downloads the sanitised SVG', async () => {
    const blobs: Blob[] = []
    const create = vi.fn((b: Blob) => {
      blobs.push(b)
      return 'blob:diagram'
    })
    Object.assign(URL, { createObjectURL: create, revokeObjectURL: vi.fn() })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
    render(<DiagramView source={FLOW} title="Release flow" theme="light" />)
    const img = (await screen.findByRole('img', { name: /Release flow/ })) as HTMLImageElement
    fireEvent.click(screen.getByRole('button', { name: /download the svg/i }))
    expect(click).toHaveBeenCalled()
    expect(blobs).toHaveLength(1)
    expect(blobs[0]!.type).toBe('image/svg+xml')
    // jsdom's Blob has no text(); a FileReader reads it
    const text = await new Promise<string>((resolve) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result))
      reader.readAsText(blobs[0]!)
    })
    expect(text).toBe(decodedSvg(img))
    expect(screen.getByRole('button', { name: /download a png/i })).toBeTruthy()
  })
})
