"""The browser acceptance check loads a page it controls and reads it back (#357).

THE DEFECT THIS PINS. Every browser task the smoke suite and
scripts/prove-gke-dispatch.sh ran was `actions: [{type: screenshot}]` with no
url, so every one screenshotted about:blank -- and a run whose page never
loaded, whose renderer drew nothing or whose text extraction was broken still
came back SUCCEEDED. The about:blank task stays, relabelled for the one thing it
proves (Chromium starts); the fixture task beside it loads a page whose colour
and text the check chose, and then asserts both came back.

The helpers are run for real here, sourced by bash as the suites source them,
against PNGs this file writes -- with every PNG row filter, because Chromium's
encoder picks a filter per row and a decoder that only knows filter 0 would
read a real screenshot as noise.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import subprocess
import zlib
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from swarm_common.profiles import RUNNER_PROFILES, check_inputs, url_refusal

ROOT = Path(__file__).resolve().parents[3]


def run_lib(snippet: str, *args: str, env_extra: dict[str, str] | None = None):
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    env.pop("SWARM_ENV_FILE", None)
    env.pop("SWARM_BROWSER_FIXTURE_BASE", None)
    env.update(env_extra or {})
    script = (
        f'source "{ROOT}/scripts/lib/common.sh"; '
        f'source "{ROOT}/scripts/lib/testlib.sh"; '
        + snippet
    )
    return subprocess.run(
        ["bash", "-c", script, "browser-fixture", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


# -- a PNG writer, for the decoder under test ----------------------------------

def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def write_png(path: Path, rows: list[list[tuple[int, ...]]], *, alpha: bool = False) -> None:
    """Rows of pixels -> a PNG, cycling the row filter through 0..4."""
    channels = 4 if alpha else 3
    width = len(rows[0])
    raw = bytearray()
    previous = bytes(width * channels)
    for y, row in enumerate(rows):
        line = bytes(v for pixel in row for v in pixel[:channels])
        kind = y % 5
        out = bytearray()
        for i, value in enumerate(line):
            left = line[i - channels] if i >= channels else 0
            up = previous[i]
            up_left = previous[i - channels] if i >= channels else 0
            predictor = (0, left, up, (left + up) // 2, _paeth(left, up, up_left))[kind]
            out.append((value - predictor) & 0xFF)
        raw.append(kind)
        raw += out
        previous = line

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, len(rows), 8, 6 if alpha else 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b"")
    )



def fixture_rgb() -> tuple[int, int, int]:
    result = run_lib('printf "%s" "${BROWSER_FIXTURE_RGB}"')
    assert result.returncode == 0, result.stderr
    value = result.stdout.strip()
    assert len(value) == 6, value
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def shows_colour(path: Path, rgb: str, minimum: str = "0.5"):
    return run_lib('png_shows_colour "$1" "$2" "$3"', str(path), rgb, minimum)


def block_page(rgb, *, width=40, height=30, text_rows=(12, 14)) -> list[list[tuple[int, ...]]]:
    """A colour block filling the page, with a few 'text' pixels in white."""
    rows = []
    for y in range(height):
        row = []
        for x in range(width):
            in_text = text_rows[0] <= y < text_rows[1] and 5 <= x < 25 and x % 2 == 0
            row.append((255, 255, 255) if in_text else tuple(rgb))
        rows.append(row)
    return rows


# -- the pixel assertion -------------------------------------------------------

def test_a_blank_white_screenshot_fails(tmp_path):
    """about:blank: one colour, every pixel white. The defect, as a PNG."""
    path = tmp_path / "blank.png"
    write_png(path, [[(255, 255, 255)] * 40 for _ in range(30)])
    result = shows_colour(path, "ffffff", "0.01")
    assert result.returncode != 0, result.stderr
    assert "blank" in result.stderr


def test_the_fixture_page_passes(tmp_path):
    rgb = fixture_rgb()
    path = tmp_path / "fixture.png"
    write_png(path, block_page(rgb))
    result = shows_colour(path, "%02x%02x%02x" % rgb)
    assert result.returncode == 0, result.stderr


def test_the_fixture_page_passes_as_rgba(tmp_path):
    rgb = fixture_rgb()
    path = tmp_path / "fixture-rgba.png"
    write_png(path, [[(*p, 255) for p in row] for row in block_page(rgb)], alpha=True)
    result = shows_colour(path, "%02x%02x%02x" % rgb)
    assert result.returncode == 0, result.stderr


def test_a_page_of_another_colour_fails(tmp_path):
    """A page that loaded something, but not the fixture: a non-blank
    screenshot is not enough on its own."""
    path = tmp_path / "other.png"
    write_png(path, block_page((20, 120, 40)))
    result = shows_colour(path, "%02x%02x%02x" % fixture_rgb())
    assert result.returncode != 0
    assert "fixture colour" in result.stderr


def test_too_little_of_the_colour_fails(tmp_path):
    """The fixture block fills the viewport; a sliver of it is a different page."""
    rgb = fixture_rgb()
    rows = [[(255, 255, 255)] * 40 for _ in range(30)]
    rows[0][0] = rgb
    rows[0][1] = (0, 0, 0)
    path = tmp_path / "sliver.png"
    write_png(path, rows)
    result = shows_colour(path, "%02x%02x%02x" % rgb)
    assert result.returncode != 0


def test_a_file_that_is_not_a_png_fails_loudly(tmp_path):
    path = tmp_path / "error.png"
    path.write_text('{"error": {"code": 404}}')
    result = shows_colour(path, "%02x%02x%02x" % fixture_rgb())
    assert result.returncode != 0
    assert "not a PNG" in result.stderr


# -- the text assertion ---------------------------------------------------------

def test_the_text_check_finds_the_string_and_only_it(tmp_path):
    page = tmp_path / "page.txt"
    page.write_text("Swarm acceptance fixture\nswarm-fixture-test-run-1\n")
    assert run_lib('text_file_contains "$1" "$2"', str(page),
                   "swarm-fixture-test-run-1").returncode == 0
    assert run_lib('text_file_contains "$1" "$2"', str(page),
                   "swarm-fixture-test-run-2").returncode != 0
    # Literal, not a pattern: a regex would let `.` match anything.
    assert run_lib('text_file_contains "$1" "$2"', str(page),
                   "swarm.fixture.test.run.1").returncode != 0
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    assert run_lib('text_file_contains "$1" "$2"', str(empty), "x").returncode != 0


# -- the fixture host's availability is a skip, not a failure ------------------

#: A stand-in for curl, as a shell function: writes BODY to the -o file and
#: prints the -w line as "<STUB_CODE> <STUB_TYPE>", or exits STUB_RC. No
#: network: the preflight is exercised for real against what it is handed.
CURL_STUB = (
    'curl() { local out=""; while [[ $# -gt 0 ]]; do '
    'case "$1" in -o) out="$2"; shift 2 ;; *) shift ;; esac; done; '
    '[[ "${STUB_RC:-0}" -eq 0 ]] || return "${STUB_RC}"; '
    'printf "%s" "${STUB_BODY:-}" >"${out}"; '
    'printf "%s %s" "${STUB_CODE:-200}" "${STUB_TYPE:-text/html; charset=utf-8}"; }; '
)


def fixture_page_html() -> str:
    result = run_lib('browser_fixture_html "$1"', "test-run-1")
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize(
    "stub, says",
    [
        ({"STUB_RC": "28"}, "httpbin.org unavailable (curl exit 28)"),
        ({"STUB_CODE": "503", "STUB_BODY": "<html>Service Unavailable</html>"},
         "httpbin.org unavailable (HTTP 503)"),
        ({"STUB_TYPE": "application/json", "STUB_BODY": "{}"},
         "httpbin.org answered application/json, not text/html"),
        ({"STUB_BODY": "<html><title>Just a moment...</title></html>"},
         "httpbin.org answered 200 without the fixture's title"),
    ],
)
def test_an_unavailable_fixture_host_is_a_skip_that_names_it(stub, says):
    """httpbin down, slow or answering something else is httpbin's
    availability: the check SKIPs naming the host, submits nothing, and records
    no failure -- a release is not turned red by a third party."""
    result = run_lib(
        CURL_STUB
        + 'test_run_id() { printf "test-run-1"; }; '
        'submit_task() { echo SUBMITTED >&2; return 1; }; '
        't_check_browser_fixture "unit" 1 "{}" || exit 9; '
        '[[ "${TESTS_FAILED}" -eq 0 && "${TESTS_SKIPPED}" -eq 1 ]] || exit 8',
        env_extra=stub,
    )
    assert result.returncode == 0, result.stderr
    assert "SKIP" in result.stderr and says in result.stderr, result.stderr
    assert "page load not measured" in result.stderr
    assert "SUBMITTED" not in result.stderr


def test_an_unavailable_fixture_host_fails_when_skips_are_failures():
    """SUITE_SKIPS_ARE_FAILURES=1 still turns the skip into a failure, as it
    does for every other skip."""
    result = run_lib(
        CURL_STUB
        + 'test_run_id() { printf "test-run-1"; }; '
        't_check_browser_fixture "unit" 1 "{}"; '
        '[[ "${TESTS_FAILED}" -eq 1 ]] || exit 8',
        env_extra={"STUB_CODE": "502", "SUITE_SKIPS_ARE_FAILURES": "1"},
    )
    assert result.returncode == 0, result.stderr


def test_a_host_serving_the_fixture_passes_the_preflight():
    result = run_lib(
        CURL_STUB + 'browser_fixture_preflight "https://httpbin.org/base64/x"',
        env_extra={"STUB_BODY": fixture_page_html()},
    )
    assert result.returncode == 0, result.stderr


def test_a_succeeded_task_on_another_page_fails_naming_where_it_ended():
    """The host answered here but the runner ended elsewhere (an upstream error
    page, a redirect): a FAIL, carrying the runner's final_url and title, so an
    error page shows up as one."""
    result = run_lib(
        CURL_STUB
        + 'test_run_id() { printf "test-run-1"; }; '
        'submit_task() { printf "task-1"; }; '
        'wait_for_state() { printf "SUCCEEDED"; }; '
        'task_artifact_raw() { ARTIFACT_HTTP=404; return 1; }; '
        'task_field() { case "$2" in '
        '*.title*) printf "502 Bad Gateway" ;; '
        '*final_url*) printf "https://httpbin.org/status/502" ;; esac; }; '
        'if t_check_browser_fixture "unit" 1 "{}"; then exit 9; fi; '
        '[[ "${FIXTURE_TASK_ID}" == task-1 && "${FIXTURE_TASK_FINAL}" == SUCCEEDED ]] || exit 8',
        env_extra={"STUB_BODY": fixture_page_html()},
    )
    assert result.returncode == 0, result.stderr
    assert "titled '502 Bad Gateway'" in result.stderr
    assert "final_url: https://httpbin.org/status/502" in result.stderr


# -- the fixture input the suites submit -----------------------------------------

def fixture_input(run_id: str = "test-run-1", base: str | None = None) -> dict:
    """What t_check_browser_fixture SUBMITS, captured at submit_task.

    The input is a literal on its submit_task line (so the contract-parity scan
    can read it), so it is taken from there: submit_task is replaced by one
    that records its arguments and refuses, and test_run_id by a fixed one.
    The check then records a FAIL and returns, having called nothing else.
    curl is stubbed to serve the fixture, so the preflight passes offline.
    """
    extra = {"SWARM_BROWSER_FIXTURE_BASE": base} if base else {}
    extra["STUB_BODY"] = fixture_page_html()
    extra["CAPTURE"] = str(Path(os.environ.get("TMPDIR", "/tmp")) / f"fixture-{os.getpid()}.json")
    result = run_lib(
        CURL_STUB
        + 'RUN="$1"; test_run_id() { printf "%s" "${RUN}"; }; '
        'submit_task() { printf "%s|%s" "$1" "$2" >"${CAPTURE}"; return 1; }; '
        'if t_check_browser_fixture "unit" 1 "{}"; then exit 9; fi; '
        '[[ "${TESTS_FAILED}" -eq 1 ]] || exit 8',
        run_id,
        env_extra=extra,
    )
    assert result.returncode == 0, result.stderr
    captured = Path(extra["CAPTURE"])
    profile, _, body = captured.read_text().partition("|")
    captured.unlink()
    assert profile == "browser"
    return json.loads(body)


def decoded_page(url: str) -> str:
    encoded = urlsplit(url).path.rsplit("/", 1)[-1]
    return base64.urlsafe_b64decode(encoded).decode("utf-8")


def test_the_fixture_input_is_one_the_browser_profile_accepts():
    body = fixture_input()
    # `prompt` is the one key every profile takes; the rest is the profile's
    # to declare, checked by the rule the API applies (as test_profile_input).
    check_inputs(RUNNER_PROFILES["browser"], {k: v for k, v in body.items() if k != "prompt"})
    assert url_refusal(body["url"]) == "", url_refusal(body["url"])
    assert "test-run-1" in body["prompt"]
    assert body["extract_text"] is True, "page.txt is the text the check reads"


def test_the_fixture_page_carries_the_known_colour_and_string():
    body = fixture_input("test-run-7")
    html = decoded_page(body["url"])
    rgb = "%02x%02x%02x" % fixture_rgb()
    assert f"#{rgb}" in html
    text = run_lib('browser_fixture_text "$1"', "test-run-7").stdout.strip()
    assert text and text in html
    assert "test-run-7" in text, "the string is per run, so a stale page cannot pass"


def test_the_fixture_screenshot_is_named_for_the_check():
    shots = [a for a in fixture_input()["actions"] if a.get("type") == "screenshot"]
    assert shots == [{"type": "screenshot", "name": "fixture.png", "full_page": False}]


def test_the_fixture_host_can_be_pointed_elsewhere():
    """A self-hosted httpbin, for a deployment whose egress does not reach the
    public one. Same encoding, so the same assertions hold."""
    body = fixture_input(base="https://httpbin.internal.example.com/base64/")
    assert body["url"].startswith("https://httpbin.internal.example.com/base64/")
    assert "Swarm acceptance fixture" in decoded_page(body["url"])


def test_the_about_blank_input_is_still_there_and_still_only_proves_a_start():
    """Kept: it needs nothing outside the platform. It is the fixture check, not
    this one, that says a page loaded."""
    result = run_lib('profile_input browser run-1')
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert "url" not in body
    assert body["actions"] == [{"type": "screenshot", "name": "proof.png", "full_page": False}]


@pytest.mark.parametrize("script", ["smoke-test.sh", "prove-gke-dispatch.sh"])
def test_both_suites_run_the_fixture_check(script):
    text = (ROOT / "scripts" / script).read_text()
    assert "t_check_browser_fixture" in text
    assert "Chromium starts" in text
