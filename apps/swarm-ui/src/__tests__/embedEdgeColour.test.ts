// A hub that frames the console draws its own dock over the console's left
// edge and needs that edge's colour to keep the dock readable on it
// (embedEdgeColour.ts). The console tells only the page framing it, and only
// when the colour changes.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { EMBED_EDGE_MESSAGE_TYPE, watchEmbedderEdgeColour, type EdgeWindow } from '../embedEdgeColour'

const PARENT = 'example.com'
const EMBEDDER = `https://dash.${PARENT}`

/** An element as `elementsFromPoint` would find it at the left edge. */
function layer(style: Partial<CSSStyleDeclaration>): HTMLElement {
  const element = document.createElement('div')
  Object.assign(element.style, style)
  document.body.append(element)
  return element
}

/**
 * A console at console.example.com, framed by `referrer` unless `own` puts it
 * in its own tab. It reads the real test document, with `stack` as what lies
 * under the left edge.
 */
function framedConsole({
  stack = [],
  referrer = `${EMBEDDER}/apps`,
  own = false,
}: { stack?: Element[]; referrer?: string; own?: boolean } = {}) {
  const postMessage = vi.fn()
  const events = new EventTarget()
  const scheme = new EventTarget()
  const self = {}
  const win: EdgeWindow = {
    parent: { postMessage },
    self,
    top: own ? self : {},
    location: { hostname: `console.${PARENT}`, protocol: 'https:' },
    innerHeight: 800,
    document: {
      referrer,
      body: document.body,
      documentElement: document.documentElement,
      createElement: document.createElement.bind(document),
      elementsFromPoint: () => stack,
    },
    getComputedStyle: (element) => window.getComputedStyle(element),
    matchMedia: () =>
      ({
        addEventListener: scheme.addEventListener.bind(scheme),
        removeEventListener: scheme.removeEventListener.bind(scheme),
      }) as unknown as MediaQueryList,
    MutationObserver,
    addEventListener: events.addEventListener.bind(events),
    removeEventListener: events.removeEventListener.bind(events),
  }
  return { win, postMessage, events, scheme }
}

const settle = () => vi.advanceTimersByTimeAsync(250)
const reported = (postMessage: ReturnType<typeof vi.fn>) =>
  postMessage.mock.calls.map(([message]) => (message as { rgb: number[] }).rgb)

let dispose = () => {}

beforeEach(() => {
  vi.useFakeTimers()
  document.body.removeAttribute('style')
})

afterEach(() => {
  dispose()
  dispose = () => {}
  document.body.replaceChildren()
  document.documentElement.removeAttribute('data-theme')
  vi.useRealTimers()
})

describe('the console tells the hub framing it the colour of its left edge', () => {
  it('posts nothing in its own tab', async () => {
    const { win, postMessage } = framedConsole({ own: true, stack: [layer({ backgroundColor: 'rgb(11, 58, 122)' })] })
    dispose = watchEmbedderEdgeColour(win)
    await settle()
    expect(postMessage).not.toHaveBeenCalled()
  })

  it('posts the edge colour to the framing page under the parent domain, and never to every origin', async () => {
    const { win, postMessage } = framedConsole({ stack: [layer({ backgroundColor: 'rgb(11, 58, 122)' })] })
    dispose = watchEmbedderEdgeColour(win)
    await settle()
    expect(postMessage.mock.calls).toEqual([[{ type: 'hub:edge', rgb: [11, 58, 122] }, EMBEDDER]])
    expect(EMBED_EDGE_MESSAGE_TYPE).toBe('hub:edge')
    expect(postMessage).not.toHaveBeenCalledWith(expect.anything(), '*')
  })

  it.each([
    ['another domain', 'https://dash.example.org/'],
    ['another scheme', `http://dash.${PARENT}/`],
    ['nothing to go by', ''],
  ])('posts nothing when the framing page is %s', async (_case, referrer) => {
    const { win, postMessage } = framedConsole({ referrer, stack: [layer({ backgroundColor: 'rgb(11, 58, 122)' })] })
    dispose = watchEmbedderEdgeColour(win)
    await settle()
    expect(postMessage).not.toHaveBeenCalled()
  })

  it('looks past see-through layers to the background under them', async () => {
    const { win, postMessage } = framedConsole({
      stack: [
        layer({ backgroundColor: 'rgba(0, 0, 0, 0)' }),
        layer({ backgroundColor: 'rgba(255, 255, 255, 0.2)' }),
        layer({ backgroundColor: 'rgb(250, 250, 250)' }),
      ],
    })
    dispose = watchEmbedderEdgeColour(win)
    await settle()
    expect(reported(postMessage)).toEqual([[250, 250, 250]])
  })

  it("reads a gradient's first colour when the layer has no colour of its own", async () => {
    const { win, postMessage } = framedConsole({
      stack: [layer({ backgroundImage: 'linear-gradient(rgb(42, 30, 92), rgb(14, 10, 32))' })],
    })
    dispose = watchEmbedderEdgeColour(win)
    await settle()
    expect(reported(postMessage)).toEqual([[42, 30, 92]])
  })

  it('takes the gradient over the colour under it, since a background image paints above its colour', async () => {
    const { win, postMessage } = framedConsole({
      stack: [
        layer({
          backgroundColor: 'rgb(0, 0, 0)',
          backgroundImage: 'linear-gradient(rgb(255, 255, 255), rgb(255, 255, 255))',
        }),
      ],
    })
    dispose = watchEmbedderEdgeColour(win)
    await settle()
    expect(reported(postMessage)).toEqual([[255, 255, 255]])
  })

  it("falls back to the page's own background when nothing at the edge has one", async () => {
    const { win, postMessage } = framedConsole()
    document.body.style.backgroundColor = 'rgb(12, 26, 46)'
    dispose = watchEmbedderEdgeColour(win)
    await settle()
    expect(reported(postMessage)).toEqual([[12, 26, 46]])
  })

  it('posts again when the edge changes colour, and not when it stays the same', async () => {
    const sidebar = layer({ backgroundColor: 'rgb(255, 255, 255)' })
    const { win, postMessage } = framedConsole({ stack: [sidebar] })
    dispose = watchEmbedderEdgeColour(win)
    await settle()

    document.documentElement.dataset['theme'] = 'dark'
    await settle()
    expect(reported(postMessage)).toEqual([[255, 255, 255]])

    sidebar.style.backgroundColor = 'rgb(16, 16, 16)'
    await settle()
    expect(reported(postMessage)).toEqual([
      [255, 255, 255],
      [16, 16, 16],
    ])
  })

  it('still posts while the page keeps changing faster than the settle time', async () => {
    const { win, postMessage } = framedConsole({ stack: [layer({ backgroundColor: 'rgb(11, 58, 122)' })] })
    dispose = watchEmbedderEdgeColour(win)
    for (let i = 0; i < 5; i++) {
      document.body.append(document.createElement('div'))
      await vi.advanceTimersByTimeAsync(100)
    }
    expect(reported(postMessage)).toEqual([[11, 58, 122]])
  })

  it('reads the edge again after a colour-scheme change and after a resize', async () => {
    const light = layer({ backgroundColor: 'rgb(255, 255, 255)' })
    const dark = layer({ backgroundColor: 'rgb(16, 16, 16)' })
    const dim = layer({ backgroundColor: 'rgb(30, 30, 30)' })
    const stack: Element[] = [light]
    const { win, postMessage, events, scheme } = framedConsole({ stack })
    dispose = watchEmbedderEdgeColour(win)
    await settle()

    // What lies under the edge changes without a DOM change the observer sees.
    stack[0] = dark
    await settle()
    expect(reported(postMessage)).toEqual([[255, 255, 255]])

    scheme.dispatchEvent(new Event('change'))
    await settle()
    stack[0] = dim
    events.dispatchEvent(new Event('resize'))
    await settle()
    expect(reported(postMessage)).toEqual([
      [255, 255, 255],
      [16, 16, 16],
      [30, 30, 30],
    ])
  })

  it('stops watching once disposed', async () => {
    const sidebar = layer({ backgroundColor: 'rgb(255, 255, 255)' })
    const { win, postMessage, events, scheme } = framedConsole({ stack: [sidebar] })
    watchEmbedderEdgeColour(win)()
    sidebar.style.backgroundColor = 'rgb(16, 16, 16)'
    scheme.dispatchEvent(new Event('change'))
    events.dispatchEvent(new Event('resize'))
    await settle()
    expect(postMessage).not.toHaveBeenCalled()
  })

  it('is started from the console root beside the ready message, and stopped when it unmounts', () => {
    const app = readFileSync(join(__dirname, '..', 'App.tsx'), 'utf8')
    expect(app).toContain('useEffect(() => watchEmbedderEdgeColour(window), [])')
  })
})
