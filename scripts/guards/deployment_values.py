#!/usr/bin/env python3
"""The deployment-values guard (docs/design/generic-repository.md sections 1, 3 and 4).

Finds what ties the repository to a company, a person or a live deployment, in
two layers:

* SHAPE: the rules below, tracked, holding no value: a project id after
  `projects/` or `--project`, a project number, a service-account address, an
  email address or a URL host outside the placeholder set, a real-shaped OAuth
  client id, a bucket or a tenant secret, a tracked deployment file. What may
  pass is exactly scripts/guards/placeholders.json.
* NAME: the pattern list, which holds only values (the company's name, people,
  the shared project's other-team resources) and so lives outside the tree:
  the `DEPLOYMENT_PATTERNS` repository secret in CI, the
  `swarm-deployment-patterns` Secret Manager entry for the worker's gate.

Output never carries a matched value. Enforcing mode prints one line a finding,
`path:line C<n> <rule-id>`, where a name entry's rule id is `list:<line in the
list>`. Report mode (the default until every cleanup lane has merged, section
4.4) prints only a count per directory, so a public log is not a map. Summary
mode (a fork's pull request, section 4.3) prints only pass or fail and the
count.

Failing closed (section 4.2): with no list under CI, or a list whose digest
line does not match, or a malformed entry, the shape layer still runs and the
exit is 2. With no list locally the exit is 3, never 0.

Standard library only: the wrapper runs it with the system python3, before any
virtual environment exists.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable, Iterator

#: Lane 12 flips this to "enforce" once `--tree` reports zero (section 4.4).
DEFAULT_MODE = "report"
MODES = ("report", "enforce", "summary")

PLACEHOLDERS = Path(__file__).resolve().parent / "placeholders.json"
SECRET_SLOT = "swarm-deployment-patterns"
LIST_VARIABLE = "DEPLOYMENT_PATTERNS"
LIST_FILE_VARIABLE = "DEPLOYMENT_PATTERNS_FILE"

#: Files that must name the repository's own address (section 2.5); the address
#: is read from GITHUB_REPOSITORY or the origin remote, never written here.
ADDRESS_FILES = ("README.md", "plugin/.claude-plugin/plugin.json", ".claude-plugin/marketplace.json")
ADDRESS_PREFIXES = (".github/ISSUE_TEMPLATE/",)

EXIT_FINDINGS, EXIT_CLOSED, EXIT_NO_LIST = 1, 2, 3
CATEGORY = re.compile(r"^C(?:[1-9]|1[01])$")
DIGEST_LINE = re.compile(r"^# sha256 ([0-9a-f]{64})$")
_SEPARATORS = "-_."


class ListError(Exception):
    """The pattern list is unavailable, altered or malformed. Its message never
    holds an entry's value."""


# ---------------------------------------------------------------------------
# The placeholder set
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Allow:
    projects: frozenset[str]
    numbers: frozenset[str]
    domains: tuple[str, ...]
    reserved_tlds: frozenset[str]
    email_domains: frozenset[str]
    addresses: frozenset[str]
    hosts: tuple[str, ...]
    tailnet: str
    buckets: frozenset[str]
    tenants: frozenset[str]
    app_ids: frozenset[str]
    repository: str
    #: The owner's rulings of 2026-11-07 (placeholders.json `_rulings`).
    cluster_suffixes: frozenset[str] = frozenset()
    url_paths: tuple[str, ...] = ()
    tenant_slugs: frozenset[str] = frozenset()
    exempt: frozenset[tuple[str, int]] = frozenset()

    @classmethod
    def load(cls, path: Path = PLACEHOLDERS) -> Allow:
        data = json.loads(path.read_text(encoding="utf-8"))
        allow = data["allowlist"]
        domains = tuple(allow["domains"])
        home = allow["release_home"]
        return cls(
            projects=frozenset([*data["projects"].values(), *allow["public_projects"],
                                *(e["project"] for e in home["projects"])]),
            numbers=frozenset(data["project_numbers"].values()),
            domains=domains,
            reserved_tlds=frozenset(allow["reserved_tlds"]),
            email_domains=frozenset(allow["email_domains"]),
            addresses=frozenset([*(a.lower() for a in allow["public_addresses"]),
                                 *(e["address"].lower() for e in allow["public_service_addresses"])]),
            hosts=domains + tuple(allow["public_hosts"]) + tuple(e["host"] for e in allow["public_product_hosts"]),
            tailnet=data["tailnet"],
            buckets=frozenset([*data["buckets"], data["shared_project_resources"]["bucket"],
                               *(e["bucket"] for e in home["buckets"])]),
            tenants=frozenset(data["tenants"]),
            app_ids=frozenset([data["github"]["app_id"], data["github"]["installation_id"],
                               *(e["id"] for e in allow["public_app_ids"])]),
            repository=data["github"]["repository"],
            cluster_suffixes=frozenset(e["suffix"] for e in allow["cluster_dns_suffixes"]),
            url_paths=tuple(e["path"] for e in allow["public_url_paths"]),
            tenant_slugs=frozenset(e["slug"] for e in allow["tenant_slugs"]),
            exempt=frozenset((e["path"], int(e["line"])) for e in allow["exempt_lines"]),
        )

    def screened(self, entries: list[Entry]) -> Allow:
        """Without the generic tenant slugs a list entry matches: nothing on the
        private pattern list is ever allowed."""
        return replace(self, tenant_slugs=frozenset(
            s for s in self.tenant_slugs if not any(e.pattern.search(s) for e in entries)))


def _under(host: str, suffixes: Iterable[str]) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


def _reserved(host: str, allow: Allow) -> bool:
    return host == "localhost" or host.rsplit(".", 1)[-1] in allow.reserved_tlds


# ---------------------------------------------------------------------------
# The shape layer
# ---------------------------------------------------------------------------

#: Not followed by more of the value, nor by a template's start: `gs://b-${X}`
#: is a template, not a bucket named `b`.
_END = r"(?![A-Za-z0-9_.$<{-])"
_PROJECT_ID = r"([a-z][a-z0-9-]{4,28}[a-z0-9])"


@dataclass(frozen=True)
class Rule:
    rule_id: str
    category: str
    pattern: re.Pattern[str]
    #: Given the match, True when what it found is allowed.
    allowed: Callable[[re.Match[str], Allow], bool]


def _email_allowed(m: re.Match[str], allow: Allow) -> bool:
    domain = m.group(2).lower()
    return (
        domain.endswith("gserviceaccount.com")  # C3's rule reads these
        or _under(domain, allow.domains)
        or _reserved(domain, allow)
        or domain in allow.email_domains
        or m.group(0).lower() in allow.addresses
    )


#: A git config key scoped to a URL, `credential.https://<host>.helper`: the
#: last label is the variable, not part of the host.
_GIT_URL_SECTION = re.compile(r"\b(?:credential|http|url)\.$")
_GIT_URL_VARIABLES = frozenset({
    "helper", "username", "usehttppath", "proxy", "proxyauthmethod", "extraheader", "sslverify", "sslcert",
    "sslkey", "sslcainfo", "cookiefile", "useragent", "insteadof", "pushinsteadof", "postbuffer",
    "lowspeedlimit", "lowspeedtime", "followredirects", "emptyauth", "delegation",
})
#: Cloud Run's URL shapes: `<service>-<10-letter hash>-<region code>.a.run.app`
#: and `<service>-<project number>.<region>.run.app`. The shape is public; a
#: real hash or a project number outside the placeholders is a deployment value.
_RUN_APP_HASHED = re.compile(r"^[a-z0-9-]+-([a-z0-9]{10})-[a-z]{2,4}\.a\.run\.app$")
_RUN_APP_NUMBERED = re.compile(r"^[a-z0-9-]+-(\d{10,13})\.[a-z0-9-]+\.run\.app$")


def _run_app_allowed(host: str, allow: Allow) -> bool:
    if m := _RUN_APP_HASHED.match(host):
        return len(set(m.group(1))) == 1  # `xxxxxxxxxx`: a placeholder, not a hash
    if m := _RUN_APP_NUMBERED.match(host):
        return m.group(1) in allow.numbers
    return True


def _host_allowed(m: re.Match[str], allow: Allow) -> bool:
    host = m.group(1).lower()
    head, _, last = host.rpartition(".")
    if last in _GIT_URL_VARIABLES and "." in head and _GIT_URL_SECTION.search(m.string, 0, m.start()):
        host = head
    if host.endswith(".run.app"):
        return _run_app_allowed(host, allow)
    return (host.endswith(".ts.net") or _under(host, allow.hosts) or _reserved(host, allow)
            or host.rsplit(".", 1)[-1] in allow.cluster_suffixes)


def _project_path_allowed(m: re.Match[str], allow: Allow) -> bool:
    """A project id, or a page under a public documentation path (`.../concepts/projects/...`)."""
    before = m.string[max(0, m.start() - 200):m.start() + len("projects/")]
    return m.group(1) in allow.projects or before.endswith(allow.url_paths)


#: Google-owned service agent domains that are not a project id: Cloud
#: Storage's agent is service-<project number>@gs-project-accounts, and the
#: project-number rule still holds that number to the placeholder set.
_GOOGLE_AGENT_DOMAINS = frozenset({"gs-project-accounts"})


SHAPE_RULES: tuple[Rule, ...] = (
    Rule("project-path", "C1", re.compile(r"\bprojects/" + _PROJECT_ID + _END), _project_path_allowed),
    Rule("project-flag", "C1", re.compile(r"--project(?:=|[ \t]+)[\"']?" + _PROJECT_ID + _END),
         lambda m, a: m.group(1) in a.projects),
    Rule("workload-pool", "C1", re.compile(r"\b" + _PROJECT_ID + r"\.svc\.id\.goog\b"),
         lambda m, a: m.group(1) in a.projects),
    Rule("project-number", "C2", re.compile(
        r"(?:\bprojects/(\d{10,13})\b|\b(\d{10,13})-compute@|\bservice-(\d{10,13})@|\b(\d{10,13})@cloudbuild\b"
        r"|\b(\d{10,13})-(?![a-z0-9]{32}\.)[a-z0-9]+\.apps\.googleusercontent\.com)"),
         lambda m, a: next(g for g in m.groups() if g) in a.numbers),
    Rule("service-account", "C3", re.compile(r"\b[a-z0-9-]+@([a-z0-9-]+)\.iam\.gserviceaccount\.com\b"),
         lambda m, a: m.group(1) in a.projects or m.group(1).startswith("gcp-sa-")
         or m.group(1) in _GOOGLE_AGENT_DOMAINS),
    Rule("url-host", "C4", re.compile(r"\bhttps?://([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})" + _END),
         _host_allowed),
    Rule("tailnet", "C4", re.compile(r"\b([a-z0-9-]+\.ts\.net)\b"),
         lambda m, a: m.group(1) == a.tailnet),
    # A string escape is not part of a local part: `"\n@pytest.fixture"` is a
    # decorator after a newline, and `"text\nname@host"` is still `name@host`.
    Rule("email", "C5", re.compile(
        r"(?:(?<![A-Za-z0-9._%+\\-])|(?<=\\[nrt]))([A-Za-z0-9._%+-]+)@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})\b"),
         _email_allowed),
    Rule("oauth-client", "C6", re.compile(r"\b\d+-[a-z0-9]{32}\.apps\.googleusercontent\.com\b"),
         lambda m, a: False),
    Rule("github-app-id", "C6", re.compile(r"(?i)\b(?:github_)?(?:app|installation)[_-]?id\b[\"']?\s*[:=]\s*[\"']?(\d{3,})\b"),
         lambda m, a: m.group(1) in a.app_ids),
    Rule("bucket", "C7", re.compile(r"\bgs://([a-z0-9][a-z0-9._-]{1,61}[a-z0-9])" + _END),
         lambda m, a: m.group(1) in a.buckets),
    Rule("tenant-secret", "C7", re.compile(
        r"\bswarm-tenant-([a-z0-9]+(?:-[a-z0-9]+)*?)-(?:git|anthropic|openai|typesafe|qa|refresh)\b"),
         lambda m, a: m.group(1) in a.tenants or m.group(1) in a.tenant_slugs),
)

#: C11 (section 1.2): a reference that will point at a different issue once the
#: fresh repository exists. Off unless asked (`--archive-refs`): before the
#: cutover every such reference is right, and the cutover's own sweep (steps C3
#: and C9) and lanes 9 and 10 ask for it.
ARCHIVE_RULE = Rule("archive-ref", "C11", re.compile(r"(?:(?<![\w&/])#\d{1,6}\b|/(?:issues|pull)/\d+\b)"),
                    lambda m, a: False)


def tracked_file_rule(path: str) -> bool:
    """C10: a tracked deployment file (section 1.2)."""
    name = path.rsplit("/", 1)[-1]
    if name.endswith((".tfvars", ".tfvars.json")):
        return not path.startswith("tests/terraform/")
    if name == ".env" or (name.startswith(".env.") and name not in (".env.example", ".env.sample")):
        return True
    return name.endswith((".tfstate", ".tfstate.backup", ".sarif")) or name == ".coverage"


# ---------------------------------------------------------------------------
# The name layer: the pattern list
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    line: int
    category: str
    pattern: re.Pattern[str]


@dataclass
class PatternList:
    digest: str
    entries: list[Entry] = field(default_factory=list)


def literal_pattern(value: str) -> re.Pattern[str]:
    """Case-insensitive and word-bounded, with `-`, `_` and `.` interchangeable."""
    parts = [f"[{re.escape(_SEPARATORS)}]" if ch in _SEPARATORS else re.escape(ch) for ch in value]
    return re.compile(r"(?<![A-Za-z0-9])" + "".join(parts) + r"(?![A-Za-z0-9])", re.IGNORECASE)


def parse_entries(body: str, *, first_line: int) -> list[Entry]:
    entries: list[Entry] = []
    for n, raw in enumerate(body.splitlines(), first_line):
        line = raw.rstrip("\r")
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t", 2)
        if len(fields) != 3 or not CATEGORY.match(fields[0]) or fields[1] not in ("literal", "regex") or not fields[2]:
            raise ListError(f"pattern list line {n}: malformed (expected <category>\\t<kind>\\t<value>, "
                            "category C1-C11, kind literal or regex)")
        if fields[1] == "literal":
            pattern = literal_pattern(fields[2])
        else:
            try:
                pattern = re.compile(fields[2])
            except re.error:
                raise ListError(f"pattern list line {n}: the regex does not compile") from None
        entries.append(Entry(n, fields[0], pattern))
    return entries


def split_digest(text: str) -> tuple[str | None, str]:
    """The digest line's digest (or None) and the rest."""
    first, _, rest = text.partition("\n")
    m = DIGEST_LINE.match(first.rstrip("\r"))
    return (m.group(1), rest) if m else (None, text)


def digest_of(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def parse_list(text: str) -> PatternList:
    digest, body = split_digest(text)
    if digest is None:
        raise ListError("pattern list has no '# sha256 <digest>' first line")
    # A store that drops the final newline still holds the same list.
    if digest not in (digest_of(body), digest_of(body + "\n")):
        raise ListError(f"pattern list digest mismatch: its first line says {digest}, its content is "
                        f"{digest_of(body)}")
    return PatternList(digest, parse_entries(body, first_line=2))


def stamp(text: str) -> tuple[str, str]:
    """The source's entries under a fresh digest line (sync-patterns.sh)."""
    _, body = split_digest(text)
    parse_entries(body, first_line=1)
    digest = digest_of(body)
    return digest, f"# sha256 {digest}\n{body}"


def _in_ci() -> bool:
    return os.environ.get("CI", "").strip().lower() not in ("", "0", "false")


def read_list(args: argparse.Namespace) -> str | None:
    """The list's text, or None when none was given. Raises ListError when a
    source was named but cannot be read."""
    if args.patterns_secret:
        project = os.environ.get("PROJECT_ID", "")
        if not project:
            raise ListError(f"the pattern list slot {SECRET_SLOT} cannot be read: PROJECT_ID is not set")
        try:
            done = subprocess.run(
                [os.environ.get("SWARM_GCLOUD") or "gcloud", "secrets", "versions", "access", "latest",
                 f"--secret={SECRET_SLOT}", f"--project={project}"],
                capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            done = None
        if done is None or done.returncode != 0 or not done.stdout.strip():
            raise ListError(f"the pattern list slot {SECRET_SLOT} in project {project} is not readable")
        return done.stdout
    path = args.patterns_file or os.environ.get(LIST_FILE_VARIABLE)
    if path:
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError:
            raise ListError(f"the pattern list file {path} is not readable") from None
    text = os.environ.get(LIST_VARIABLE)
    return text if text and text.strip() else None


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass
class Source:
    """One file's text to scan: every line, or only the given line numbers."""

    path: str
    text: str
    lines: set[int] | None = None
    #: Whether the file itself is new to the scan (all of --tree; added in --diff).
    whole_file: bool = True


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=cwd, check=True,
                          capture_output=True, text=True, errors="replace").stdout


def _readable(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def tree_sources(root: Path) -> Iterator[Source]:
    for rel in _git("ls-files", "-z", cwd=root).split("\0"):
        if rel:
            text = _readable(root / rel)
            yield Source(rel, "" if text is None else text)


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def diff_sources(root: Path, base: str) -> Iterator[Source]:
    """Only the lines added since the merge base with `base`, from the files at HEAD."""
    added = set(_git("diff", "--no-renames", "--name-only", "--diff-filter=A", "-z", f"{base}...HEAD",
                     cwd=root).split("\0")) - {""}
    patch = _git("diff", "--no-color", "--no-renames", "--no-ext-diff", "--unified=0", f"{base}...HEAD", cwd=root)
    wanted: dict[str, set[int]] = {}
    current: str | None = None
    for line in patch.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
            if current is not None:
                wanted.setdefault(current, set())
        elif current is not None and (m := _HUNK.match(line)):
            start, count = int(m.group(1)), int(m.group(2) or "1")
            wanted[current].update(range(start, start + count))
    for rel in sorted(set(wanted) | added):
        text = _readable(root / rel)
        yield Source(rel, "" if text is None else text, wanted.get(rel, set()), whole_file=rel in added)


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    category: str
    rule_id: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} {self.category} {self.rule_id}"


def repository_address(root: Path) -> str | None:
    """The repository's own `owner/name` (section 2.5), from configuration."""
    if os.environ.get("GITHUB_REPOSITORY"):
        return os.environ["GITHUB_REPOSITORY"]
    try:
        url = _git("remote", "get-url", "origin", cwd=root).strip()
    except (subprocess.CalledProcessError, OSError):
        return None
    m = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else None


def _mask_address(source: Source, address: str | None, allow: Allow) -> str:
    if not address or not (source.path in ADDRESS_FILES or source.path.startswith(ADDRESS_PREFIXES)):
        return source.text
    return re.sub(re.escape(address), allow.repository, source.text, flags=re.IGNORECASE)


def scan(sources: Iterable[Source], allow: Allow, entries: list[Entry], *, archive_refs: bool,
         address: str | None) -> list[Finding]:
    rules = SHAPE_RULES + ((ARCHIVE_RULE,) if archive_refs else ())
    found: set[Finding] = set()
    for source in sources:
        if source.whole_file and tracked_file_rule(source.path):
            found.add(Finding(source.path, 1, "C10", "tracked-file"))
        if not source.text or source.lines == set():
            continue
        starts = [0] + [m.end() for m in re.finditer("\n", source.text)]

        def line_of(offset: int) -> int:
            return bisect.bisect_right(starts, offset)

        def keep(line: int) -> bool:
            return source.lines is None or line in source.lines

        for rule in rules:
            for m in rule.pattern.finditer(source.text):
                line = line_of(m.start())
                if keep(line) and not rule.allowed(m, allow):
                    found.add(Finding(source.path, line, rule.category, rule.rule_id))
        named = _mask_address(source, address, allow)
        if named is not source.text:
            starts = [0] + [m.end() for m in re.finditer("\n", named)]
        for entry in entries:
            for m in entry.pattern.finditer(named):
                line = bisect.bisect_right(starts, m.start())
                if keep(line):
                    found.add(Finding(source.path, line, entry.category, f"list:{entry.line}"))
    return sorted(f for f in found if (f.path, f.line) not in allow.exempt)


def directory_of(path: str, depth: int) -> str:
    parts = path.split("/")[:-1]
    return "/".join(parts[:depth]) if parts else "."


# ---------------------------------------------------------------------------
# Candidates for the first list (section 1.5, step 4)
# ---------------------------------------------------------------------------

_PROSE = (".md", ".txt", ".rst")
_WORD = re.compile(r"\b[A-Z][a-zA-Z0-9]{2,}\b")


def candidates(root: Path, allow: Allow) -> dict[str, Counter[str]]:
    """Every token the shape layer lets through only because it is unknown."""
    by_id = {rule.rule_id: rule for rule in SHAPE_RULES}
    email, host_rule = by_id["email"], by_id["url-host"]
    domains: Counter[str] = Counter()
    hosts: Counter[str] = Counter()
    capitalised: Counter[str] = Counter()
    lower_words: set[str] = set()
    for source in tree_sources(root):
        for m in email.pattern.finditer(source.text):
            domain = m.group(2).lower()
            if not (_under(domain, allow.domains) or _reserved(domain, allow) or domain.endswith("gserviceaccount.com")):
                domains[domain] += 1
        for m in host_rule.pattern.finditer(source.text):
            host = m.group(1).lower()
            if not (_under(host, allow.domains) or _reserved(host, allow)):
                hosts[host] += 1
        if source.path.endswith(_PROSE):
            capitalised.update(_WORD.findall(source.text))
            lower_words.update(re.findall(r"\b[a-z][a-z0-9]{2,}\b", source.text))
    english: set[str] = set()
    dictionary = Path("/usr/share/dict/words")
    if dictionary.is_file():
        english = {w.strip().lower() for w in dictionary.read_text(encoding="utf-8", errors="replace").splitlines()}
    words = Counter({w: n for w, n in capitalised.items()
                     if n > 5 and w.lower() not in lower_words and w.lower() not in english})
    return {"email-domain": domains, "host": hosts, "word": words}


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def _say(message: str) -> None:
    print(f"deployment-values: {message}", file=sys.stderr)


def _toplevel() -> Path:
    try:
        return Path(_git("rev-parse", "--show-toplevel", cwd=Path.cwd()).strip())
    except (subprocess.CalledProcessError, OSError):
        return Path.cwd()


def check(args: argparse.Namespace) -> int:
    allow = Allow.load()
    root = _toplevel()
    closed: str | None = None
    entries: list[Entry] = []
    try:
        text = read_list(args)
    except ListError as exc:
        text, closed = None, str(exc)
    no_list = False
    if text is not None:
        try:
            parsed = parse_list(text)
            entries = parsed.entries
            allow = allow.screened(entries)
            _say(f"pattern list sha256 {parsed.digest} ({len(entries)} entries)")
        except ListError as exc:
            closed = str(exc)
    elif closed is None:
        if _in_ci():
            closed = "pattern list unavailable: DEPLOYMENT_PATTERNS is unset or empty"
        else:
            no_list = True
            _say("skipped: no pattern list (the name layer did not run; set DEPLOYMENT_PATTERNS, "
                 "--patterns-file or --patterns-secret)")
    if args.tree:
        sources: Iterable[Source] = tree_sources(root)
    elif args.diff:
        sources = diff_sources(root, args.diff)
    else:
        path = Path(args.text)
        content = _readable(path)
        if content is None:
            _say(f"--text {args.text} is not a readable text file")
            return EXIT_CLOSED
        sources = [Source(args.text, content, whole_file=False)]
    found = scan(sources, allow, entries, archive_refs=args.archive_refs, address=repository_address(root))
    if args.mode == "enforce":
        for finding in found:
            print(finding)
    elif args.mode == "summary":
        print(f"{'fail' if found else 'pass'}: {len(found)} findings")
    else:
        per_dir = Counter(directory_of(f.path, args.depth) for f in found)
        for directory in sorted(per_dir):
            print(f"{directory} {per_dir[directory]}")
        print(f"total {len(found)}")
    if closed:
        _say(closed)
        return EXIT_CLOSED
    if found and args.mode != "report":
        return EXIT_FINDINGS
    return EXIT_NO_LIST if no_list else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deployment-values.sh", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command")

    stamp_cmd = sub.add_parser("stamp", help="validate a list source and write it under a fresh digest line")
    stamp_cmd.add_argument("--in", dest="source", required=True)
    stamp_cmd.add_argument("--out", required=True)

    cand = sub.add_parser("candidates", help="write candidate entries for the list to a file")
    cand.add_argument("--out", required=True)

    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] in ("stamp", "candidates"):
        args = parser.parse_args(argv)
        if args.command == "stamp":
            try:
                digest, stamped = stamp(Path(args.source).read_text(encoding="utf-8"))
            except ListError as exc:
                _say(str(exc))
                return EXIT_CLOSED
            Path(args.out).write_text(stamped, encoding="utf-8")
            print(digest)
            return 0
        found = candidates(_toplevel(), Allow.load())
        with open(args.out, "w", encoding="utf-8") as out:
            for kind, counts in found.items():
                for value, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
                    out.write(f"{kind}\t{n}\t{value}\n")
        print(" ".join(f"{kind} {len(counts)}" for kind, counts in found.items()))
        return 0

    check_parser = argparse.ArgumentParser(prog="deployment-values.sh")
    what = check_parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--tree", action="store_true", help="every tracked file")
    what.add_argument("--diff", metavar="BASE", help="only the lines added since the merge base with BASE")
    what.add_argument("--text", metavar="FILE", help="one file, e.g. an issue's or a pull request's text")
    check_parser.add_argument("--mode", choices=MODES, default=DEFAULT_MODE)
    check_parser.add_argument("--patterns-file", metavar="FILE")
    check_parser.add_argument("--patterns-secret", action="store_true",
                              help=f"read the list from Secret Manager ({SECRET_SLOT} in $PROJECT_ID)")
    check_parser.add_argument("--archive-refs", action="store_true", help="also find C11, old issue references")
    check_parser.add_argument("--depth", type=int, default=2, help="report mode: directory depth of the counts")
    return check(check_parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
