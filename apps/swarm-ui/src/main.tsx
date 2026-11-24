import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
// Before App: every screen's sheet keeps its place ahead of styles.css (shellStyles.ts).
import './shellStyles'
import { App } from './App'
import { ErrorBoundary } from './ErrorBoundary'
import { installPreloadReload } from './preloadReload'
import { applyTheme, readTheme } from './theme'
// The rebrand's two faces, SELF-HOSTED: Vite bundles the woff2 files from
// these packages, so the console never asks Google Fonts for anything.
import '@fontsource-variable/inter-tight'
import '@fontsource/dm-mono/400.css'
import '@fontsource/dm-mono/500.css'
import './styles.css'

// The remembered theme goes on <html> before the first paint of the app.
applyTheme(readTheme())

// A stale chunk after a deploy reloads the tab once (preloadReload.ts), before
// anything renders that could import one.
installPreloadReload()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
