// Build-time probe of the patched jev-browser (JEV-IMAGE, part of #1030), run by
// selftest.py `jev_browser` against a Chromium it started with a CDP port. Not
// shipped: bind-mounted like selftest.py.
//
//     DEBUG=pw:protocol JEV_BROWSER_CDP_ENDPOINT=http://127.0.0.1:PORT \
//         node attach-probe.mjs /opt/swarm/jev-browser/node_modules/jev-browser/src/index.mjs
//
// It goes through `JevBrowser.launch()`, exactly the call the MCP server makes,
// and prints one JSON line on stdout: what the attached session saw. The
// caller reads that line, asks the browser over HTTP whether it is still
// running with its tab, and reads Playwright's protocol log on stderr for the
// methods the live gate refuses (cdppolicy.py) -- so a jev-browser bump that
// starts emulating a device or creating a context fails the build here, not
// through the gate in a tenant's attempt.
import { pathToFileURL } from "node:url";

const { JevBrowser } = await import(pathToFileURL(process.argv[2]).href);
const b = await JevBrowser.launch();
const out = {
  attached: b.attached === true,
  contexts: b.browser.contexts().length,
  viewport: b.page.viewportSize(),
};
await b.page.goto("data:text/html,<title>jev attach ok</title><h1>jev attach ok</h1><button>Go</button>");
out.title = await b.page.title();
const snap = await b.snapshotText({ interactive: true });
out.snapshot_has_button = /Go/.test(typeof snap === "string" ? snap : JSON.stringify(snap));
out.webmcp_tools = (await b.webmcpTools()).length;
out.contexts_after = b.browser.contexts().length;
await b.close();
out.connected_after_close = b.browser.isConnected();
console.log(JSON.stringify(out));
