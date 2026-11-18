// A thin, defensive wrapper over the gstack `browse` CLI.
//
// EVERY METHOD IS ASYNC, AND THAT IS NOT A STYLE CHOICE.
// ------------------------------------------------------
// The first version of this file used `spawnSync`, which is the obvious way to
// call a CLI. It made the whole suite impossible, in a way that took an hour to
// see: the control plane the browser is pointed at runs in THIS process, and
// `spawnSync` blocks the event loop, so node could not answer the page load it
// had just asked the browser to perform. The browser waited, the daemon printed
// "Operation timed out: goto: Timeout 15000ms exceeded", and that timeout left
// the daemon wedged -- after which every later command answered "Headed server
// running (PID n) but not responding" and the suite spent minutes recovering
// from a deadlock it had caused itself.
//
// Two things follow and both are load-bearing:
//   * nothing here may block the loop, so `spawn` plus a promise, never
//     `spawnSync`;
//   * the child's stdout must not be a pipe. `browse` starts a background
//     daemon that inherits the child's stdio and is meant to outlive the
//     command, so with piped stdio the stream never ends and a timeout can
//     never fire. Output goes to temporary files, which have no such writer to
//     wait for.
//
// The rest of these rules cost somebody hours on 2026-09-21 and are enforced
// here rather than left in a runbook:
//
//  * HEADED MODE IS SET IN THE ENVIRONMENT, never with `--headed`. The flag is
//    folded into a config hash; once it is set, every later call that does not
//    repeat it looks like a different configuration and restarts the daemon,
//    which drops every tab mid-run.
//  * Headed mode uses launchPersistentContext(~/.gstack/chromium-profile).
//    `launched` mode is chromium.launch() + newContext() and has NO cookie jar.
//    A silent fall back to it is how a session disappears, so the mode is read
//    back and reported rather than assumed.
//  * `js` RUNS AGAINST THE ACTIVE TAB, and the active tab drifts -- another
//    process opening a tab is enough, and during this session it drifted onto
//    somebody's accounts.google.com sign-in. So this wrapper takes a tab of its
//    own, pins its id, re-selects it before every command, AND makes every
//    evaluated script refuse to act unless location.origin is exactly the
//    target.
//  * `js` is not reliable for a direct value in every build, so anything that
//    matters is stashed on a window property and read back in a second call.

import { spawn } from 'node:child_process'
import { closeSync, mkdtempSync, openSync, readFileSync, rmSync } from 'node:fs'
import { homedir, tmpdir } from 'node:os'
import { join } from 'node:path'

const DEFAULT_BIN = join(homedir(), '.claude', 'skills', 'gstack', 'browse', 'dist', 'browse')

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

export class Browse {
  constructor({ bin = process.env.SWARM_BROWSE_BIN || DEFAULT_BIN, headed = true, origin } = {}) {
    this.bin = bin
    this.origin = origin
    this.tabId = null
    this.env = {
      ...process.env,
      // In the environment. Not on the command line. See the header.
      ...(headed ? { BROWSE_HEADED: '1' } : {}),
      // Long enough that a full matrix run cannot idle the daemon out between
      // two viewports and take the pinned tab with it.
      BROWSE_IDLE_TIMEOUT: process.env.BROWSE_IDLE_TIMEOUT || '1800000',
    }
  }

  raw(...args) {
    return this.rawWithin(45000, ...args)
  }

  /**
   * `raw`, with an explicit ceiling.
   *
   * A wedged daemon does not answer AT ALL, so every call against one costs the
   * full timeout. Readiness probes therefore get a short leash and real work
   * gets a long one.
   */
  async rawWithin(timeout, ...args) {
    const dir = mkdtempSync(join(tmpdir(), 'swarm-ui-test-'))
    const outPath = join(dir, 'out')
    const errPath = join(dir, 'err')
    const o = openSync(outPath, 'w')
    const e = openSync(errPath, 'w')

    const result = await new Promise((resolve) => {
      const child = spawn(this.bin, args, { env: this.env, stdio: ['ignore', o, e] })
      let settled = false
      const timer = setTimeout(() => {
        if (settled) return
        settled = true
        child.kill('SIGKILL')
        resolve({ status: 124, signal: 'SIGKILL' })
      }, timeout)
      child.on('error', (err) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        resolve({ status: 127, spawnError: err.message })
      })
      child.on('close', (code, signal) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        resolve({ status: code, signal })
      })
    })

    closeSync(o)
    closeSync(e)
    let out = ''
    let err = ''
    try {
      out = readFileSync(outPath, 'utf8')
      err = readFileSync(errPath, 'utf8')
    } catch {
      // Unreadable output does not change the diagnosis; the status does.
    }
    rmSync(dir, { recursive: true, force: true })

    return {
      code: result.status === null ? 124 : result.status,
      out: out.trim(),
      err:
        result.signal === 'SIGKILL'
          ? `browse ${args[0]} did not answer within ${timeout}ms`
          : (result.spawnError ?? err.trim()),
    }
  }

  /**
   * `healthy`/`headed` or an explanation. Never guessed.
   *
   * THE TIMEOUT IS A PARAMETER BECAUSE THE FIRST CALL IS NOT LIKE THE REST.
   * When no daemon is running, `status` STARTS one -- and starting one means
   * launching Chromium, which takes tens of seconds. Killing that call on a
   * short timeout leaves the half-started daemon that then answers "Headed
   * server running (PID n) but not responding" to everything: the suite
   * manufacturing its own wedge and then recovering from it. So the first probe
   * is patient and the ones after it are not.
   */
  async status(timeout = 60000) {
    const r = await this.rawWithin(timeout, 'status')
    const mode = /^Mode:\s*(\S+)/m.exec(r.out)
    return {
      ok: /^Status:\s*healthy/m.test(r.out),
      mode: mode ? mode[1] : null,
      raw: r.out || r.err,
    }
  }

  /**
   * Wait for a daemon, starting one if there is none, and report what we got.
   *
   * ONE PROCESS DOES THIS, once, before anything else. Two processes probing
   * concurrently both try to spawn the server, and the loser prints "Another
   * instance is starting the server, waiting..." and then times out, leaving a
   * half-started daemon that answers nothing for the rest of the run.
   *
   * It never throws. A browser that cannot be reached is the runner's problem
   * to report, and a suite that dies here with a stack trace has told the
   * reader less than one that says "mode: launched" at the top of its report.
   */
  async warmUp({ attempts = 6, waitMs = 2500, log = () => {} } = {}) {
    let last = null
    let replaced = false
    for (let i = 0; i < attempts; i++) {
      last = await this.status(i === 0 ? 60000 : 15000)
      if (last.ok) return last

      // "Headed server running (PID n) but not responding. Run
      // '/open-gstack-browser' to restart."
      //
      // This state does not clear on its own and `restart` does not clear it
      // either -- `status`, `stop` and `restart` all answer with the same line,
      // because they all go through the same unreachable socket. The daemon's
      // own advice is to replace the process, so that is what this does, once,
      // and only for a pid whose command line really is the browse server.
      // Killing something because a string in someone else's error message
      // looked like a pid is not a thing a test suite may do.
      const stuck = /server running \(PID (\d+)\) but not responding/i.exec(last.raw || '')
      if (stuck && !replaced) {
        const pid = Number(stuck[1])
        const ps = await this.command('ps', ['-o', 'command=', '-p', String(pid)])
        if (ps.includes('browse/src/server.ts')) {
          log(`the browse daemon (pid ${pid}) is wedged; replacing it`)
          await this.killPid(pid)
          // AND THE CHROMIUM IT WAS HOLDING. Headed mode is
          // launchPersistentContext over ~/.gstack/chromium-profile, and a
          // profile directory can only be owned by one Chromium at a time.
          // Killing the daemon alone leaves its browser holding the lock, so
          // the replacement daemon cannot take the profile and wedges in
          // exactly the same way -- which is how a single wedge turned into a
          // run that never started and three daemons that never died.
          await this.killProfileHolders(log)
          replaced = true
          await sleep(4000)
          continue
        }
        log(`pid ${pid} is not a browse server, so it was left alone`)
      }
      await sleep(waitMs)
    }
    return last ?? { ok: false, mode: null, raw: 'the browse daemon never answered' }
  }

  async killPid(pid) {
    try {
      process.kill(pid, 'SIGKILL')
    } catch {
      // Already gone, which is the outcome we wanted anyway.
    }
  }

  /**
   * Kill every Chromium holding the gstack profile directory.
   *
   * MATCHED ON THE PROFILE PATH, never on "chromium". The person running this
   * has their own browser open and this must not touch it: the only processes
   * selected are ones whose command line names
   * ~/.gstack/chromium-profile, which is the driver's own profile and nothing
   * else's.
   */
  async killProfileHolders(log) {
    const marker = join(homedir(), '.gstack', 'chromium-profile')
    const listing = await this.command('ps', ['-o', 'pid=,command=', '-ax'])
    let killed = 0
    for (const line of listing.split('\n')) {
      if (!line.includes(`--user-data-dir=${marker}`)) continue
      const pid = Number(line.trim().split(/\s+/)[0])
      if (!Number.isFinite(pid) || pid <= 1) continue
      await this.killPid(pid)
      killed++
    }
    if (killed) log(`released the driver's chromium profile (${killed} process(es))`)
  }

  /** Read one short command's stdout. Used only to identify a pid. */
  command(bin, args) {
    return new Promise((resolve) => {
      const child = spawn(bin, args, { stdio: ['ignore', 'pipe', 'ignore'] })
      let out = ''
      child.stdout.on('data', (d) => (out += d))
      child.on('close', () => resolve(out))
      child.on('error', () => resolve(''))
    })
  }

  /**
   * Take a tab of our own and remember its id.
   *
   * NOT `goto` ON WHATEVER TAB IS IN FRONT. The daemon is shared: headed mode
   * is a persistent context over ~/.gstack/chromium-profile, and during this
   * session the active tab drifted onto an accounts.google.com sign-in that
   * somebody else had opened. `goto` would have navigated that tab away mid
   * sign-in. `goto` is kept only as the fallback for the one state where
   * `newtab` cannot work: a daemon with no page at all, which answers "No
   * active page" to everything.
   *
   * THE COMMAND'S EXIT STATUS IS ADVISORY; THE PAGE'S OWN URL IS THE TRUTH. A
   * daemon still settling aborts the incoming navigation and then completes it
   * ("net::ERR_ABORTED; maybe frame was detached?"), and the first command
   * after Chromium launches can outlast the client's health-check window. Both
   * print a failure for a navigation that succeeded, so the page is asked where
   * it is and the page is believed.
   */
  async open(url) {
    const want = url.split('#')[1] ?? ''

    if (this.tabId !== null) {
      const listed = await this.raw('tabs')
      if (listed.code === 0 && new RegExp(`\\[${this.tabId}\\]`).test(listed.out)) {
        await this.select()
        const again = await this.raw('goto', url)
        if (again.code === 0 && (await this.isAt(want))) return again
      }
      this.tabId = null
    }

    let last = ''
    for (let i = 0; i < 4; i++) {
      const nt = await this.raw('newtab', url)
      const opened = /Opened tab (\d+)/.exec(nt.out)
      if (opened) {
        this.tabId = Number(opened[1])
        await this.select()
        if (await this.isAt(want)) return nt
        last = 'the new tab did not settle on the requested hash'
      } else if (/No active page/i.test(nt.err + nt.out)) {
        // The one case `newtab` cannot serve. Establish a page, then pin
        // whatever tab that produced.
        const g = await this.raw('goto', url)
        const listed = await this.raw('tabs')
        const active = /^\s*→\s*\[(\d+)\]/m.exec(listed.out)
        if (active && (await this.isAt(want))) {
          this.tabId = Number(active[1])
          return g
        }
        last = `could not establish a page: ${g.err || g.out || listed.err}`
      } else {
        last = nt.err || nt.out
      }
      await sleep(2000)
    }
    throw new Error(`could not open ${url} after four attempts: ${last}`)
  }

  /** Does the pinned tab really hold this origin and hash? */
  async isAt(hash) {
    const r = await this.raw('js', 'location.origin + "|" + location.hash')
    return r.code === 0 && r.out.includes(this.origin) && r.out.includes(hash)
  }

  async select() {
    if (this.tabId === null) return
    await this.raw('tab', String(this.tabId))
  }

  async goto(url) {
    await this.select()
    return this.raw('goto', url)
  }

  async viewport(w, h) {
    await this.select()
    return this.raw('viewport', `${w}x${h}`)
  }

  async press(key) {
    await this.select()
    return this.raw('press', key)
  }

  async screenshot(path) {
    await this.select()
    return this.raw('screenshot', path)
  }

  /**
   * Evaluate an expression that returns a JSON-serialisable value.
   *
   * THE ORIGIN GUARD IS NOT OPTIONAL and is applied here rather than left to
   * each caller, because the one caller that forgets is the one that does the
   * damage. A script that finds itself on the wrong tab returns a structured
   * refusal; it never falls through and acts.
   */
  /**
   * The origin-guarded wrapper, as source.
   *
   * Exposed because the matrix runs its audit inside a `chain` -- one process
   * for the whole unit instead of eight -- and that composition still has to
   * carry the guard. The guard is the wrapper's job, not the caller's; a
   * caller that can build an unguarded script is a caller that eventually
   * does.
   */
  guarded(expression, { expectOrigin = this.origin } = {}) {
    return `(() => {
      if (${JSON.stringify(String(expectOrigin))} !== location.origin) {
        window.__swarmUiTest = JSON.stringify({
          __wrongOrigin: true, saw: location.origin, wanted: ${JSON.stringify(String(expectOrigin))}
        });
        return 'guarded';
      }
      try {
        window.__swarmUiTest = JSON.stringify((() => { ${expression} })());
      } catch (e) {
        window.__swarmUiTest = JSON.stringify({ __threw: true, message: String(e && e.message || e) });
      }
      return 'stored';
    })()`
  }

  /**
   * Interpret what the guarded script stashed on the window.
   *
   * Shared by `eval` and by the chained matrix unit so the two cannot drift
   * apart in how they treat a refusal or an in-page throw.
   */
  interpret(raw) {
    let parsed
    try {
      parsed = JSON.parse(raw)
    } catch {
      throw new Error(`read-back was not JSON: ${String(raw).slice(0, 300)}`)
    }
    if (parsed && parsed.__wrongOrigin) {
      throw new Error(
        `origin guard refused: the active tab is ${parsed.saw}, wanted ${parsed.wanted}. ` +
          `The pinned tab drifted; nothing was evaluated.`,
      )
    }
    if (parsed && parsed.__threw) {
      throw new Error(`in-page script threw: ${parsed.message}`)
    }
    return parsed
  }

  /**
   * Split a chain's output into one result per command.
   *
   * `browse chain` prints "[cmd] result" blocks separated by blank lines, not
   * JSON, so this is where that shape is known -- once, here, rather than in
   * every caller.
   */
  static parseChain(out) {
    return String(out)
      .split(/\n\s*\n/)
      .map((block) => {
        const m = /^\[([a-z-]+)\]\s*([\s\S]*)$/.exec(block.trim())
        return m ? { command: m[1], text: m[2].trim() } : { command: null, text: block.trim() }
      })
      .filter((r) => r.text !== '' || r.command !== null)
  }

  async eval(expression, { expectOrigin = this.origin } = {}) {
    await this.select()
    const guarded = `(() => {
      if (${JSON.stringify(String(expectOrigin))} !== location.origin) {
        window.__swarmUiTest = JSON.stringify({
          __wrongOrigin: true, saw: location.origin, wanted: ${JSON.stringify(String(expectOrigin))}
        });
        return 'guarded';
      }
      try {
        window.__swarmUiTest = JSON.stringify((() => { ${expression} })());
      } catch (e) {
        window.__swarmUiTest = JSON.stringify({ __threw: true, message: String(e && e.message || e) });
      }
      return 'stored';
    })()`
    const put = await this.raw('js', guarded)
    if (put.code !== 0) throw new Error(`browse js failed: ${put.err || put.out}`)
    const got = await this.raw('js', 'window.__swarmUiTest')
    if (got.code !== 0) throw new Error(`browse js read-back failed: ${got.err || got.out}`)
    return this.interpret(got.out)
  }

  /**
   * One viewport's whole audit in a single invocation.
   *
   * Resize, evaluate, screenshot and read the result back, as one `chain`.
   * Eight process starts per unit became one: `browse` is a 61MB bun binary
   * and the matrix is eighty units, so the saving is not a micro-optimisation,
   * it is the difference between a suite somebody runs and a suite somebody
   * schedules.
   */
  async auditViewport({ width, height, expression, screenshotPath }) {
    const steps = [
      ['tab', String(this.tabId)],
      ['viewport', `${width}x${height}`],
      ['js', this.guarded(expression)],
    ]
    if (screenshotPath) steps.push(['screenshot', screenshotPath])
    steps.push(['js', 'window.__swarmUiTest'])

    const r = await this.chain(steps, { timeout: 120000 })
    if (r.code !== 0) throw new Error(`the audit chain failed: ${r.err || r.out}`)
    const results = Browse.parseChain(r.out)
    const reads = results.filter((x) => x.command === 'js')
    if (reads.length < 2) {
      throw new Error(`the audit chain returned ${reads.length} js result(s), expected 2: ${r.out.slice(0, 200)}`)
    }
    return this.interpret(reads[reads.length - 1].text)
  }

  /** Fire an in-page script for its side effect only. */
  async run(expression) {
    await this.select()
    return this.raw('js', `(() => { ${expression} })()`)
  }

  /**
   * Run a sequence of commands in ONE invocation.
   *
   * `browse` is a 61MB bun binary and every call pays its start-up. The
   * keyboard walk is a Tab press and a focus read per control -- call it
   * seventy round trips on a busy screen -- and one at a time that is minutes
   * per view, which is how the first version of this suite spent longer
   * tabbing through one screen than auditing all twenty. `chain` takes a JSON
   * array of [cmd, ...args] on stdin and runs them in order, so the whole walk
   * is one process.
   *
   * It stops at the first error, which is the behaviour we want: a walk that
   * lost the tab halfway is not a walk with a few missing steps, it is a walk
   * whose remaining results are about some other page.
   */
  async chain(commands, { timeout = 180000 } = {}) {
    const dir = mkdtempSync(join(tmpdir(), 'swarm-ui-test-'))
    const outPath = join(dir, 'out')
    const errPath = join(dir, 'err')
    const o = openSync(outPath, 'w')
    const e = openSync(errPath, 'w')

    const result = await new Promise((resolve) => {
      const child = spawn(this.bin, ['chain'], { env: this.env, stdio: ['pipe', o, e] })
      let settled = false
      const timer = setTimeout(() => {
        if (settled) return
        settled = true
        child.kill('SIGKILL')
        resolve({ status: 124, signal: 'SIGKILL' })
      }, timeout)
      child.on('error', () => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        resolve({ status: 127 })
      })
      child.on('close', (code, signal) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        resolve({ status: code, signal })
      })
      child.stdin.end(JSON.stringify(commands))
    })

    closeSync(o)
    closeSync(e)
    let out = ''
    let err = ''
    try {
      out = readFileSync(outPath, 'utf8')
      err = readFileSync(errPath, 'utf8')
    } catch {
      // The status still carries the diagnosis.
    }
    rmSync(dir, { recursive: true, force: true })
    return { code: result.status === null ? 124 : result.status, out: out.trim(), err: err.trim() }
  }

  async close() {
    if (this.tabId === null) return
    await this.raw('closetab', String(this.tabId))
    this.tabId = null
  }
}
