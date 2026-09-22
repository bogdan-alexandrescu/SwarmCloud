/// <reference types="vitest/config" />
//
// The component test layer.
//
// WHY THIS FILE EXISTS. Until now this app -- 20,445 lines of TypeScript --
// had no test runner at all, and the Python files under
// tests/unit/control_plane/ that "test the UI" do it by reading `.tsx` files
// as TEXT and asserting on source strings. A source grep cannot catch a render
// error, cannot catch a runtime exception, and passes when the string it looks
// for appears in a comment -- and these files are full of long comments
// quoting the very copy those greps search for. The honesty rules in fetch.ts
// and Shell.tsx are behaviour, so they are tested as behaviour.
//
// WHY VITEST rather than jest. Vite is already the build tool, so Vitest reuses
// `@vitejs/plugin-react` and the existing `tsconfig.json` with no second
// transform pipeline, no babel config and no `ts-jest`. Jest would need its own
// transformer, its own ESM story (this package is `"type": "module"`) and its
// own moduleNameMapper -- three more things to drift from the build.
//
// COST. Four devDependencies, exact-pinned: vitest 3.2.7 (the last 3.x, which
// is the line that peers with vite 5 -- vitest 4 requires vite 6), jsdom
// 25.0.1, @testing-library/react 16.1.0 and its peer @testing-library/dom
// 10.4.0. 183 packages, all devDependencies: NOTHING here reaches the shipped
// bundle, which `vite build` derives from src/main.tsx and cannot reach a test
// file. `@testing-library/jest-dom` was deliberately NOT added -- its matchers
// are sugar over assertions this suite can make on `textContent`, and one more
// dependency to keep current is a real cost for no new coverage.
//
// A separate config from vite.config.ts on purpose: `defineConfig` from 'vite'
// does not type a `test` block, and casting it away is how a config option
// silently stops being read.

import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    // Colocated with the source they cover, so a moved component takes its
    // tests with it and a deleted component leaves a failing import rather
    // than an orphan file nobody runs.
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    setupFiles: ['src/__tests__/setup.ts'],
    // Every mock is undone between tests. Without this a test that stubs
    // `globalThis.fetch` leaves it stubbed, and the offline guard in setup.ts
    // -- the thing that makes "this suite reaches no network" true rather than
    // hoped for -- would only protect the first test in the file.
    restoreMocks: true,
    unstubGlobals: true,
    unstubEnvs: true,
    // CSS IS PROCESSED RATHER THAN STUBBED. Two files depend on it:
    // `brand.test.tsx` asserts B17 -- "an identifier is never restyled" --
    // and `shell.test.tsx` asserts the shell's regions (B3), the density pass
    // (B4.3) and the measured-zero axis (B20). Both do it by injecting the
    // SHIPPED `styles.css` into the test document and reading
    // `getComputedStyle()` off the element the component actually rendered.
    // With the default `css: false`, Vitest stubs every CSS module, so
    // `import STYLES from './styles.css?raw'` resolves to the empty string,
    // the injected sheet has zero rules, and every computed style comes back
    // `""` -- which reads as a pass for a negative assertion. A guard that is
    // strongest when it is broken is the failure mode this whole suite exists
    // to remove.
    //
    // The cost is nothing at runtime: only `src/main.tsx` imports the sheet
    // and no test imports `main.tsx`, so this processes one file and injects
    // nothing anywhere by itself.
    css: true,
    // No watcher, no browser, no coverage instrumentation: this runs inside
    // `make test`, which is a gate rather than a development loop.
    reporters: ['default'],
    // 30s, not Vitest's default 5s, AND THE REASON IS THE `css: true` ABOVE.
    //
    // The tests that matter most here inject the shipped 66KB `styles.css`
    // into jsdom and read `getComputedStyle()` back off the elements the
    // components rendered -- that is what makes them assertions about what
    // ships rather than about a hand-written copy of a rule. jsdom's CSSOM is
    // the slowest thing in this suite by an order of magnitude, and it scales
    // with the size of that sheet.
    //
    // `brand.test.tsx`'s "tells the four apart WITHOUT USING COLOUR" runs in
    // ~550ms on an idle machine and TIMED OUT AT 5461ms on this one while
    // several other agent worktrees were running their own gates (load average
    // 200). A gate that goes red because the machine was busy teaches everyone
    // to re-run it until it passes, which is how a real failure gets re-run
    // away too.
    //
    // 30s still fails fast on a genuine hang -- a test that deadlocks or awaits
    // something that never resolves is 30s, not 30 minutes -- so this buys
    // headroom without giving up the property the timeout exists for.
    testTimeout: 30_000,
    // `globals` stays OFF. Every test imports `describe`/`it`/`expect` from
    // 'vitest' explicitly, so `tsc -b` typechecks the tests with no ambient
    // types injected into the app's own compilation.
    globals: false,
  },
})
