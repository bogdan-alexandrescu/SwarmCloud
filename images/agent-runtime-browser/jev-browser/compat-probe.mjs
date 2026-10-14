// Build-time probe of the patched jev-browser's backend adapter (JUDGE-ADAPT, part of #1030),
// run by selftest.py `jev_compat` with the environment it is checking. Not shipped: bind-mounted
// like selftest.py. No browser and no network: `fetch` is replaced before the package is loaded.
//
//     JEV_API_COMPAT=internal JEV_API_URL=http://internal.invalid/jev/v1/decide \
//         node compat-probe.mjs /opt/swarm/jev-browser/node_modules/jev-browser/src/index.mjs fixtures/internal-decide.json
//
// It prints one JSON line on stdout: every request the package sent (URL, whether the
// Authorization header carried TYPESAFE_API_KEY or the placeholder, the type of `state`, the
// choice criteria) and, against an internal backend, what jev-browser's own decide() and
// resolve() made of the fixture's recorded internal-API answers -- so a jev-browser bump that
// reads answers differently, or a patch that stops adapting, fails the build here.
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const [, , index, fixturePath] = process.argv;
const fixture = JSON.parse(readFileSync(fixturePath, "utf8"));
const sent = [];
let replies = [];
globalThis.fetch = async (url, init) => {
  const body = JSON.parse(init.body);
  const auth = init.headers.Authorization;
  sent.push({
    url: String(url),
    auth: auth === `Bearer ${process.env.TYPESAFE_API_KEY}` ? "typesafe_key" : auth === `Bearer ${m.INTERNAL_PLACEHOLDER_KEY}` ? "placeholder" : "other",
    state_type: typeof body.state,
    state_parses: typeof body.state !== "string" || (() => { try { JSON.parse(body.state); return true; } catch { return false; } })(),
    choices: Object.fromEntries(Object.entries(body.questions).filter(([, q]) => q.type === "choice").map(([n, q]) => [n, q.criteria])),
    questions: Object.keys(body.questions).sort(),
  });
  const answers = replies.length ? replies.shift() : { answers: Object.fromEntries(Object.keys(body.questions).map(n => [n, { noul: 0.98 }])) };
  return new Response(JSON.stringify(answers), { status: 200, headers: { "content-type": "application/json" } });
};

const m = await import(pathToFileURL(index).href);
const out = { mode: m.compatMode(), key: m.apiKey() === m.INTERNAL_PLACEHOLDER_KEY ? "placeholder" : "other" };

// A bare jev() call with an object state, as every jev-browser call makes it.
await m.jev({ task: { goal: "g" }, page: { url: "data:,", title: "t", text: "x" } }, { q: { type: "noul", instructions: "Answer about `page`: is it there?" } });

if (out.mode === "internal") {
  // jev-browser's own session code, without a browser: decide() asks, resolve() reads the answers.
  const b = Object.create(m.JevBrowser.prototype);
  b.stats = { calls: 0, jev_ms: 0, tokens: 0 };
  const small = { url: "https://example.test/", title: "t", text: "Search the site", metrics: {}, dialogs: [], elements: [
    { i: 0, tag: "a", text: "Home", href: "/" },
    { i: 1, tag: "input:text", label: "Search" },
    { i: 2, tag: "button", text: "Go" },
  ] };
  replies = [fixture.single];
  const a1 = await b.decide(small, "press Go", {}, []);
  const r1 = b.resolve(small, a1, {});
  out.single = { stages: a1.stages, tool: r1.tool, target: r1.target, target_type: typeof r1.target, p_target: r1.p_target };

  const big = { ...small, elements: Array.from({ length: 250 }, (_, i) => ({ i, tag: "button", text: `Item ${i}` })) };
  replies = [fixture.group, fixture.target];
  const a2 = await b.decide(big, "open Item 137", {}, []);
  const r2 = b.resolve(big, a2, {});
  out.grouped = { stages: a2.stages, groups_considered: a2.groups_considered, tool: r2.tool, target: r2.target, target_type: typeof r2.target, p_target: r2.p_target };

  // A choice nothing can describe fails that call, with the reason, and sends nothing.
  const before = sent.length;
  try { await m.jev({ page: small }, { q: { type: "choice", instructions: "Which one?" } }); out.undescribable = "sent"; }
  catch (e) { out.undescribable = String(e.message); }
  out.undescribable_sent = sent.length - before;
}
out.sent = sent;
console.log(JSON.stringify(out));
