#!/usr/bin/env python3
"""Lightpanda vs headless Chromium, driven by the same Playwright code over CDP.

Part of docs/design/lightpanda-runtime.md (LIGHTPANDA-ANALYSIS, part of #2688).

WHAT IT MEASURES. For every page in PAGES and for each engine, it starts a
FRESH browser process listening for CDP on a free 127.0.0.1 port, connects
with Playwright's `connect_over_cdp`, opens the page in a new context and
records:

* start_ms      process spawn until /json/version answers;
* dom_ready_ms  `page.goto(url, wait_until="domcontentloaded")`;
* load_ms       the same navigation until the `load` event (or the error);
* rss_peak_mib  peak resident memory of the browser's whole process tree,
                sampled every 25 ms from /proc (Chromium is several processes);
* pss_peak_mib  the same with shared pages divided between the processes that
                map them (/proc/<pid>/smaps_rollup): the fairer number for a
                multi-process Chromium, whose summed RSS counts its 290 MB
                binary once per process;
* cpu_s         user+system CPU seconds of the tree (children reaped by the
                browser are included through cutime/cstime);
* text_chars, links, marker_found   what a DOM-reading agent would get:
                `document.body.innerText`, the absolute hrefs of `a[href]`,
                and whether a string the page is known to carry came out;
* screenshot    whether `page.screenshot()` returned an image, and its bytes;
* form          on form pages, whether filling and submitting worked;
* error         every failure, verbatim (first 300 characters).

Then one PARALLEL run per engine: PARALLEL pages opened at once in one
process (one context each), to measure what many pages in one agent cost.

HOW TO RERUN (in a claude-code-browser container; a headless test process may
start its own Chromium through swarm-test-browsers):

    curl -fsSLo "$TMPDIR/lightpanda" \\
      https://github.com/lightpanda-io/browser/releases/download/1.0.2/lightpanda-x86_64-linux
    echo "ae7d01d261efb9e480e18ce3424533332883863bcf70b12a4321b5733f37e19e  $TMPDIR/lightpanda" | sha256sum -c -
    chmod +x "$TMPDIR/lightpanda"
    swarm-test-browsers python3 docs/design/lightpanda-runtime/bench.py \\
      --lightpanda "$TMPDIR/lightpanda" --out "$TMPDIR/results.json"

Elsewhere pass --chromium <path to a chrome binary>. Needs only the Python
`playwright` package (no browsers download: both binaries are given).

Telemetry: Lightpanda sends usage telemetry unless
LIGHTPANDA_DISABLE_TELEMETRY=true, which this script always sets.

Every value a form receives is built at run time (secrets.token_hex); no page
is given a real credential.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import platform
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
NAV_TIMEOUT_MS = 30_000
LOAD_TIMEOUT_MS = 15_000
PARALLEL = 10
SHOTS = ""  # --shots: a directory to keep each page's screenshot in

# (id, kind, url, marker the page is known to carry, form action or None)
PAGES = [
    ("example", "static", "https://example.com/", "documentation examples", None),
    ("python-docs", "static docs", "https://docs.python.org/3/library/json.html", "JSON encoder and decoder", None),
    ("mdn-fetch", "docs, some JS", "https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API", "Fetch API", None),
    ("wikipedia", "large static article", "https://en.wikipedia.org/wiki/Headless_browser", "headless browser", None),
    ("hacker-news", "server-rendered list", "https://news.ycombinator.com/", "Hacker News", None),
    ("books-toscrape", "scraping target, static", "https://books.toscrape.com/", "All products", None),
    ("quotes-js", "scraping target, content rendered by JS", "https://quotes.toscrape.com/js/", "Albert Einstein", None),
    ("github-repo", "heavy server-rendered + JS", "https://github.com/lightpanda-io/browser", "lightpanda", None),
    ("react-dev", "framework site (Next.js)", "https://react.dev/", "React", None),
    ("todomvc-react", "client-only SPA", "https://todomvc.com/examples/react/dist/", "todos", "todo"),
    ("playwright-todomvc", "client-only SPA", "https://demo.playwright.dev/todomvc/", "todos", "todo"),
    ("angular-dev", "framework SPA (Angular)", "https://angular.dev/", "Angular", None),
    ("herokuapp-login", "login form (no real credentials)", "https://the-internet.herokuapp.com/login", "Login Page", "login"),
    ("httpbin-form", "plain HTML form POST", "https://httpbin.org/forms/post", "Customer name", "httpbin"),
    ("herokuapp-iframe", "iframe (TinyMCE editor)", "https://the-internet.herokuapp.com/iframe", "An iFrame containing the TinyMCE", None),
    ("herokuapp-frames", "nested frames", "https://the-internet.herokuapp.com/nested_frames", None, None),
    ("swarm-console", "the SwarmCloud console's sign-in (IAP redirect)", "https://swarm.saga.xyz/", "Sign in", None),
]


FRAME_TEXT_JS = """() => {
  let n = 0;
  const walk = doc => {
    for (const f of doc.querySelectorAll('iframe,frame')) {
      try {
        const d = f.contentDocument;
        if (d) { n += d.body ? d.body.innerText.length : 0; walk(d); }
      } catch (e) { /* cross-origin */ }
    }
  };
  walk(document);
  return n;
}"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tree(pid: int) -> list[int]:
    """pid and every live descendant, read from /proc."""
    children: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                fields = f.read().rsplit(")", 1)[1].split()
            children.setdefault(int(fields[1]), []).append(int(entry))
        except (OSError, IndexError):
            continue
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        out.append(p)
        todo.extend(children.get(p, []))
    return out


def rss_bytes(pids: list[int]) -> int:
    total = 0
    for p in pids:
        try:
            with open(f"/proc/{p}/statm") as f:
                total += int(f.read().split()[1]) * PAGE_SIZE
        except OSError:
            continue
    return total


def pss_bytes(pids: list[int]) -> int:
    """Proportional set size: shared pages split between the processes that
    map them, so a multi-process browser is not counted once per process."""
    total = 0
    for p in pids:
        try:
            with open(f"/proc/{p}/smaps_rollup") as f:
                for line in f:
                    if line.startswith("Pss:"):
                        total += int(line.split()[1]) * 1024
                        break
        except OSError:
            continue
    return total


def cpu_seconds(pids: list[int]) -> float:
    total = 0
    for p in pids:
        try:
            with open(f"/proc/{p}/stat") as f:
                fields = f.read().rsplit(")", 1)[1].split()
            # utime stime cutime cstime are fields 14-17 (1-based); after the
            # comm split they are indexes 11-14.
            total += sum(int(x) for x in fields[11:15])
        except (OSError, IndexError):
            continue
    return total / CLK_TCK


class Sampler(threading.Thread):
    def __init__(self, pid: int):
        super().__init__(daemon=True)
        self.pid = pid
        self.peak = 0
        self.pss_peak = 0
        self.cpu = 0.0
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            pids = tree(self.pid)
            self.peak = max(self.peak, rss_bytes(pids))
            self.pss_peak = max(self.pss_peak, pss_bytes(pids))
            self.cpu = max(self.cpu, cpu_seconds(pids))
            self.stop.wait(0.025)


def start_engine(engine: str, binary: str, profile_dir: str) -> tuple[subprocess.Popen, str, float]:
    port = free_port()
    env = dict(os.environ, LIGHTPANDA_DISABLE_TELEMETRY="true")
    if engine == "lightpanda":
        argv = [binary, "serve", "--host", "127.0.0.1", "--port", str(port), "--log-level", "error"]
    else:
        argv = [
            binary, "--headless=new", f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}", "--no-first-run", "--no-default-browser-check",
            "--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox", "about:blank",
        ]
    t0 = time.perf_counter()
    proc = subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    url = f"http://127.0.0.1:{port}"
    deadline = t0 + 20
    while True:
        try:
            with urllib.request.urlopen(f"{url}/json/version", timeout=1) as r:
                r.read()
            break
        except OSError:
            if time.perf_counter() > deadline or proc.poll() is not None:
                proc.kill()
                raise RuntimeError(f"{engine} did not answer /json/version (exit {proc.poll()})")
            time.sleep(0.01)
    return proc, url, (time.perf_counter() - t0) * 1000


def stop_engine(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, 15)
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, 9)
        except OSError:
            pass


def short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc).strip().splitlines()[0] if str(exc).strip() else ''}"[:300]


def try_form(page, form: str) -> dict:
    """A form flow without visuals; every value is made at run time."""
    value = secrets.token_hex(6)
    if form == "todo":
        box = page.locator("input.new-todo")
        box.fill(f"item {value}")
        box.press("Enter")
        page.wait_for_function("v => document.body.innerText.includes(v)", arg=value, timeout=5000)
        return {"ok": True, "check": "new todo appears in the list"}
    if form == "login":
        page.fill("#username", f"user-{value}")
        page.fill("#password", secrets.token_hex(8))
        page.click("button[type=submit]")
        page.wait_for_selector("#flash", timeout=8000)
        flash = page.inner_text("#flash")
        return {"ok": "invalid" in flash.lower(), "check": "the site's 'invalid' flash after a refused sign-in"}
    if form == "httpbin":
        page.fill("input[name=custname]", f"name-{value}")
        page.check("input[value=medium]")
        page.click("button")
        page.wait_for_function("v => document.body.innerText.includes(v)", arg=value, timeout=8000)
        return {"ok": True, "check": "the POST echo carries the typed name"}
    raise ValueError(form)


def one_page(pw, engine: str, binary: str, page_def: tuple) -> dict:
    pid_, kind, url, marker, form = page_def
    rec: dict = {"engine": engine, "page": pid_, "kind": kind, "url": url}
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as profile:
        try:
            proc, cdp, rec["start_ms"] = start_engine(engine, binary, profile)
        except RuntimeError as exc:
            rec["error"] = short(exc)
            return rec
        sampler = Sampler(proc.pid)
        sampler.start()
        errors: list[str] = []
        try:
            browser = pw.chromium.connect_over_cdp(cdp, timeout=10_000)
            ctx = browser.new_context()
            page = ctx.new_page()
            t0 = time.perf_counter()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                rec["dom_ready_ms"] = round((time.perf_counter() - t0) * 1000)
                try:
                    page.wait_for_load_state("load", timeout=LOAD_TIMEOUT_MS)
                    rec["load_ms"] = round((time.perf_counter() - t0) * 1000)
                except Exception as exc:  # noqa: BLE001 -- recorded, not raised
                    errors.append("load: " + short(exc))
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:  # noqa: BLE001 -- networkidle is best effort
                    pass
                rec["final_url"] = page.url
                text = page.evaluate("() => document.body ? document.body.innerText : ''")
                rec["text_chars"] = len(text)
                rec["text_head"] = " ".join(text.split())[:160]
                links = page.evaluate(
                    "() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)"
                )
                rec["links"] = len(links)
                rec["links_unique_http"] = len({x for x in links if x.startswith("http")})
                rec["frames"] = len(page.frames)
                # Iframe content two ways: Playwright's frame tree, and the
                # DOM's own contentDocument (same-origin frames only).
                rec["iframes_in_dom"] = page.evaluate("() => document.querySelectorAll('iframe,frame').length")
                pw_chars = 0
                for fr in page.frames[1:]:
                    try:
                        pw_chars += fr.evaluate("() => document.body ? document.body.innerText.length : 0")
                    except Exception:  # noqa: BLE001 -- a detached frame counts nothing
                        pass
                rec["iframe_text_chars_playwright"] = pw_chars
                rec["iframe_text_chars_dom"] = page.evaluate(FRAME_TEXT_JS)
                if marker:
                    rec["marker_found"] = marker.lower() in text.lower()
                if form:
                    try:
                        rec["form"] = try_form(page, form)
                    except Exception as exc:  # noqa: BLE001
                        rec["form"] = {"ok": False, "error": short(exc)}
                try:
                    shot = page.screenshot(timeout=10_000)
                    if SHOTS:
                        with open(os.path.join(SHOTS, f"{engine}-{pid_}.png"), "wb") as f:
                            f.write(shot)
                    rec["screenshot"] = {"ok": True, "bytes": len(shot)}
                except Exception as exc:  # noqa: BLE001
                    rec["screenshot"] = {"ok": False, "error": short(exc)}
            except Exception as exc:  # noqa: BLE001
                errors.append("goto: " + short(exc))
            rec["process_alive_at_end"] = proc.poll() is None
            try:
                ctx.close()
            except Exception as exc:  # noqa: BLE001
                errors.append("close: " + short(exc))
        except Exception as exc:  # noqa: BLE001
            errors.append("connect: " + short(exc))
        finally:
            time.sleep(0.1)
            sampler.stop.set()
            sampler.join()
            rec["rss_peak_mib"] = round(sampler.peak / 2**20, 1)
            rec["pss_peak_mib"] = round(sampler.pss_peak / 2**20, 1)
            rec["cpu_s"] = round(sampler.cpu, 2)
            stop_engine(proc)
        if errors:
            rec["error"] = errors
    return rec


def parallel(pw, engine: str, binary: str) -> dict:
    """PARALLEL pages at once in one browser process.

    Lightpanda allows ONE browser context per CDP connection ("Cannot have
    more than one browser context at a time", measured 2026-10-31), so each
    page gets its own connection and context, on both engines alike; the
    process allows 16 connections by default (--cdp-max-connections).
    """
    urls = [p[2] for p in PAGES if p[0] != "swarm-console"][:PARALLEL]
    rec: dict = {"engine": engine, "pages": len(urls)}
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as profile:
        proc, cdp, rec["start_ms"] = start_engine(engine, binary, profile)
        sampler = Sampler(proc.pid)
        sampler.start()
        try:
            pages = []
            t0 = time.perf_counter()
            for u in urls:
                browser = pw.chromium.connect_over_cdp(cdp, timeout=10_000)
                pg = browser.new_context().new_page()
                pages.append((u, pg))
            # Navigations are issued without waiting so they overlap.
            for u, pg in pages:
                pg.evaluate("u => { window.location.href = u }", u)
            done, failed = 0, []
            for u, pg in pages:
                try:
                    pg.wait_for_url(lambda x: x != "about:blank", timeout=NAV_TIMEOUT_MS)
                    pg.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
                    if pg.evaluate("() => (document.body ? document.body.innerText : '').length") > 0:
                        done += 1
                    else:
                        failed.append(f"{u}: empty body")
                except Exception as exc:  # noqa: BLE001
                    failed.append(f"{u}: {short(exc)}")
            rec["wall_ms"] = round((time.perf_counter() - t0) * 1000)
            rec["ok"] = done
            rec["failed"] = failed
        except Exception as exc:  # noqa: BLE001
            rec["error"] = short(exc)
        finally:
            sampler.stop.set()
            sampler.join()
            rec["rss_peak_mib"] = round(sampler.peak / 2**20, 1)
            rec["pss_peak_mib"] = round(sampler.pss_peak / 2**20, 1)
            rec["cpu_s"] = round(sampler.cpu, 2)
            stop_engine(proc)
    return rec


def find_chromium() -> str:
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    hits = sorted(glob.glob(os.path.join(root, "chromium-*", "chrome-linux*", "chrome")))
    if not hits:
        sys.exit("no Chromium under PLAYWRIGHT_BROWSERS_PATH: pass --chromium or run under swarm-test-browsers")
    return hits[-1]


def version(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return short(exc)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--lightpanda", required=True)
    ap.add_argument("--chromium", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--only", default="", help="comma-separated page ids")
    ap.add_argument("--parallel-only", action="store_true", help="skip the per-page runs")
    ap.add_argument("--shots", default="", help="directory to save each screenshot in")
    args = ap.parse_args()
    global SHOTS
    SHOTS = args.shots
    chromium = args.chromium or find_chromium()
    engines = {"lightpanda": args.lightpanda, "chromium": chromium}
    pages = [p for p in PAGES if not args.only or p[0] in args.only.split(",")]
    out = {
        "when_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": {"machine": platform.machine(), "cpus": os.cpu_count(), "kernel": platform.release(),
                 "cgroup_cpu_max": open("/sys/fs/cgroup/cpu.max").read().strip()
                 if os.path.exists("/sys/fs/cgroup/cpu.max") else None},
        "versions": {"lightpanda": version([args.lightpanda, "version"]),
                     "chromium": version([chromium, "--version"])},
        "runs": args.runs,
        "pages": [],
        "parallel": [],
    }
    with sync_playwright() as pw:
        for run in range(0 if args.parallel_only else args.runs):
            for page_def in pages:
                for engine, binary in engines.items():
                    rec = one_page(pw, engine, binary, page_def)
                    rec["run"] = run
                    out["pages"].append(rec)
                    print(json.dumps({k: rec.get(k) for k in (
                        "run", "engine", "page", "start_ms", "dom_ready_ms", "rss_peak_mib",
                        "pss_peak_mib", "cpu_s", "text_chars", "links", "marker_found", "error")}), flush=True)
                    time.sleep(1)  # about one navigation a second on any one site
        if not args.only or args.parallel_only:
            for engine, binary in engines.items():
                rec = parallel(pw, engine, binary)
                out["parallel"].append(rec)
                print(json.dumps(rec), flush=True)
    with open(args.out, "w") as f:
        json.dump(out, f, indent=1)
        f.write("\n")


if __name__ == "__main__":
    main()
