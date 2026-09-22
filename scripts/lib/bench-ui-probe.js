// The page-side half of scripts/bench-ui.sh. Run through `browse eval`.
//
// ORIGIN GUARD FIRST, ALWAYS.
// `browse js` and `browse eval` run against the ACTIVE tab, and the active tab
// drifts -- a daemon shared with another session, a popup, an OAuth redirect.
// During this project's browser work a relative fetch on a drifted tab posted
// to accounts.google.com. So this refuses to measure anything unless the
// origin is exactly the one the caller named, and says which origin it found
// instead. A benchmark that measured the wrong page would publish numbers that
// look entirely plausible.
//
// THE NONCE IS NOT DECORATION.
// The result is stashed on `window.__swarmBench` and read back in a second
// call, because `js` does not reliably return a direct value. That read-back
// has a failure mode that cost this collector its first run: if `eval` does
// not execute -- the driver refuses a path outside its allowed roots, the tab
// drifted, the page was still navigating -- the property still holds the
// PREVIOUS screen's payload, and the reader cannot tell. Every screen would
// then be recorded with some earlier screen's numbers, which look entirely
// plausible.
//
// So the shell mints a fresh nonce per capture, substitutes it here, and
// refuses any payload that does not carry it. A stale read is then a failed
// capture -- which is recorded as not-measured -- instead of a wrong number.
//
// __SWARM_EXPECTED_ORIGIN__ and __SWARM_NONCE__ are both substituted by the
// shell before this file is handed to the driver.
(() => {
  const expected = '__SWARM_EXPECTED_ORIGIN__';
  const nonce = '__SWARM_NONCE__';
  if (location.origin !== expected) {
    window.__swarmBench = JSON.stringify({
      nonce: nonce,
      ok: false,
      reason: 'origin guard: expected ' + expected + ', this tab is ' + location.origin,
    });
    return 'guarded';
  }

  const nav = performance.getEntriesByType('navigation')[0] || null;
  const paints = {};
  for (const p of performance.getEntriesByType('paint')) paints[p.name] = p.startTime;

  // Only same-origin /v1 requests. The UI is served from the same origin as
  // the API behind the load balancer, and in development through the vite
  // proxy, so anything else on this page is an asset rather than a data read.
  const api = performance
    .getEntriesByType('resource')
    .filter((e) => e.name.indexOf(expected + '/v1/') === 0)
    .map((e) => ({
      // The route TEMPLATE, not the URL: a per-task path would make every
      // sample its own metric with n=1, which the engine then refuses as
      // insufficient -- correctly, and uselessly.
      route: e.name
        .slice(expected.length)
        .split('?')[0]
        .replace(/\/(task|wf|tsk)_[A-Za-z0-9]+/g, '/{id}'),
      duration: e.duration,
      ttfb: e.responseStart > 0 ? e.responseStart - e.startTime : null,
      // 0 means the body size was not exposed (an opaque or cached response),
      // which is not the same as an empty body.
      bytes: e.transferSize > 0 ? e.transferSize : null,
    }));

  window.__swarmBench = JSON.stringify({
    nonce: nonce,
    ok: true,
    origin: location.origin,
    href: location.href,
    nav: nav
      ? {
          ttfb_ms: nav.responseStart,
          dom_content_loaded_ms: nav.domContentLoadedEventEnd,
          load_ms: nav.loadEventEnd,
        }
      : null,
    first_contentful_paint_ms:
      typeof paints['first-contentful-paint'] === 'number'
        ? paints['first-contentful-paint']
        : null,
    api,
  });
  return 'stored';
})();
