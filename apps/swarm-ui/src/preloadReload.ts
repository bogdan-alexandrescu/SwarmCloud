/**
 * ONE AUTOMATIC RELOAD WHEN A DEPLOY HAS MOVED THE CHUNKS (issue 194).
 *
 * Vite fires `vite:preloadError` on `window` when a dynamic import's preload
 * fails: in practice a tab on the old build asking for a hashed chunk the new
 * image no longer ships. Reloading fetches the new `index.html` and its
 * chunks, which is the fix, so the handler does it without asking.
 *
 * ONCE PER SESSION, until a load succeeds. The reload is recorded in
 * `sessionStorage` first; while the mark is there a second failure is left to
 * throw, so the route's `ChunkBoundary` says "A newer version is out" with a
 * Reload instead of the tab reloading itself in a loop (a deploy that really
 * lacks a chunk would otherwise reload forever).
 *
 * The mark is cleared only once the page has stayed up for
 * `LOAD_SETTLED_MS` after `load`. Clearing it on `load` itself would undo the
 * guard: a page whose first route's chunk is missing loads, clears the mark,
 * fails, reloads, and loops. Ten seconds is long past the first route's
 * import on any connection this console serves, and short enough that a
 * later deploy in the same tab gets its one reload again.
 */
export const CHUNK_RELOAD_KEY = 'swarm-ui:chunk-reload'

const LOAD_SETTLED_MS = 10_000

function readMark(): string | null {
  try {
    return sessionStorage.getItem(CHUNK_RELOAD_KEY)
  } catch {
    // Storage refused (a locked-down profile): no guard can be kept, so the
    // handler must not reload at all rather than risk a loop.
    return 'unavailable'
  }
}

function onPreloadError(event: Event) {
  if (readMark() !== null) return
  try {
    sessionStorage.setItem(CHUNK_RELOAD_KEY, String(Date.now()))
  } catch {
    return
  }
  event.preventDefault()
  window.location.reload()
}

/** The page loaded and stayed up: the next stale chunk may reload once again. */
export function markLoadSucceeded() {
  try {
    sessionStorage.removeItem(CHUNK_RELOAD_KEY)
  } catch {
    // Nothing to clear when storage is refused.
  }
}

/** Installs the handler; returns what removes it (and its pending clear). */
export function installPreloadReload(): () => void {
  let timer: ReturnType<typeof setTimeout> | undefined
  const settle = () => {
    timer = setTimeout(markLoadSucceeded, LOAD_SETTLED_MS)
  }
  window.addEventListener('vite:preloadError', onPreloadError)
  if (document.readyState === 'complete') settle()
  else window.addEventListener('load', settle, { once: true })
  return () => {
    window.removeEventListener('vite:preloadError', onPreloadError)
    window.removeEventListener('load', settle)
    if (timer !== undefined) clearTimeout(timer)
  }
}
