"""Which SwarmCloud a command talks to -- chosen by its USER, the way kubectl's is.

THE DEFECT THIS REPLACES. Until 2026-09-25 the bridge found its deployment by
reading `terraform/environments/<env>/<env>.tfvars` out of whatever checkout it
was installed from, so every person who installed the `sc` plugin was pointed at
https://swarm.saga.xyz -- the owner's deployment -- and anyone outside a
checkout was pointed at nothing. The owner's words: "the cluster deployment is
now set to my account but could be different for other deployments. Shouldn't
the plugin be configured by the user with the urls and other credentials
needed?"

So the bridge is now a deployment-agnostic CLIENT, shaped like `gh` and
`kubectl`: named CONTEXTS in a per-user config file, one of them current.

    {
      "current_context": "saga-dev",
      "contexts": {
        "saga-dev": {"url": "https://swarm.saga.xyz",
                     "oauth_client_id": "123-abc.apps.googleusercontent.com"}
      }
    }

THE FILE HOLDS NO SECRET, EVER. It is plain JSON a person may paste into an
issue. The OAuth client secret and each person's refresh token live in the
credential store (`credentials.py`: the macOS Keychain, the Secret Service, or
a 0600 file where neither exists), keyed by context name. `save` writes only
the two keys above, whatever a caller hands it.

WHICH DEPLOYMENT WINS, most explicit first -- and `resolve` implements exactly
this list, so `sc whoami` can print which layer answered:

    1. --context NAME             this command line
    2. SWARM_URL                  CI. Legacy spellings in the same slot, in
       SWARM_API_URL, API_URL     order: an exact API address (verify.tf sets
       SWARM_API_HOST, API_HOST   API_URL), then a front-door host
    3. SWARM_CONTEXT              a named context, from the environment
    4. SWARM_MCP_CONFIG_FROM=repo developer mode: this checkout's tfvars
    5. the plugin's userConfig    what /plugin install prompted for
    6. current_context            what `sc context use` last chose

SWARM_OAUTH_CLIENT_ID and SWARM_OAUTH_CLIENT_SECRET override the client of
whichever layer answered, so CI can supply a secret without a file.

THE REPOSITORY IS READ ONLY IN DEVELOPER MODE. Layer 4 exists for the owner
working inside this checkout, and is off unless SWARM_MCP_CONFIG_FROM=repo is
set -- `tests/unit/mcp/test_contexts.py` watches every file the process opens
to hold that line, because a tfvars read that silently fails looks exactly like
one that never happened.

THE PLUGIN OVERRIDES, BUT NEVER STEALS `current`. Inside the MCP server the
plugin's deployment is the one used (layer 5 beats layer 6) -- that is what
configuring the plugin means. It is also SEEDED into the file, so `sc login` in
an ordinary terminal, which never sees the plugin's environment, can find it.
Seeding makes it current only when nothing is current: a background process
moving the terminal's deployment is how a dispatch lands on the wrong cluster.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .client import SwarmError

# -- the environment's vocabulary -------------------------------------------

ENV_URL = "SWARM_URL"
ENV_CONTEXT = "SWARM_CONTEXT"
ENV_CLIENT_ID = "SWARM_OAUTH_CLIENT_ID"
ENV_CLIENT_SECRET = "SWARM_OAUTH_CLIENT_SECRET"
ENV_CONFIG_DIR = "SWARM_CONFIG_DIR"
ENV_CONFIG_FROM = "SWARM_MCP_CONFIG_FROM"
REPO_MODE = "repo"

#: The legacy address variables, in the order the bridge has always honoured
#: them. The first two name an exact API address; the last two name the IAP
#: front door's host. `scripts/lib/common.sh` reads the same four.
LEGACY_URL_VARS = ("SWARM_API_URL", "API_URL")
LEGACY_HOST_VARS = ("SWARM_API_HOST", "API_HOST")

#: The plugin's `userConfig` keys, in `plugin/.claude-plugin/plugin.json`,
#: each spelled ONCE, here, by the role it plays. `_plugin` reads through these
#: names and nothing else; it used to spell each key again as a literal, so a
#: rename made here and in the manifest together passed every test that
#: compared the two lists while the bridge went on reading the old name.
#: `tests/unit/mcp/test_plugin_user_config.py` now substitutes an answer into
#: the manifest's own env mapping for each key, the way Claude Code does, and
#: asks `resolve` where each one landed.
PLUGIN_URL = "deployment_url"
PLUGIN_CLIENT_ID = "oauth_client_id"
PLUGIN_CLIENT_SECRET = "oauth_client_secret"
PLUGIN_KEYS = (PLUGIN_URL, PLUGIN_CLIENT_ID, PLUGIN_CLIENT_SECRET)


def plugin_env(key: str) -> str:
    """The environment variable a plugin option reaches the MCP server under.

    DERIVED, not listed: `deployment_url` -> `SWARM_PLUGIN_DEPLOYMENT_URL`.
    plugin.json's `mcpServers.swarmcloud.env` maps each to
    `${user_config.<key>}`, which Claude Code substitutes when it starts the
    server (code.claude.com/docs/en/plugins-reference, "Reference a saved
    value").
    """
    return f"SWARM_PLUGIN_{key.upper()}"


def is_unset(value: str | None) -> bool:
    """Empty, or a `${user_config.KEY}` reference Claude Code left as written.

    Claude Code's documentation does not say what an UNSET option becomes
    inside an MCP server's `env` -- an empty string, or the reference verbatim.
    Both are read as "not set" here; the alternative is a literal
    `${user_config.oauth_client_id}` presented to Google as a client id.
    """
    text = (value or "").strip()
    return not text or "${user_config." in text


def _env(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "")
    return "" if is_unset(value) else value.strip()


def repo_mode(environ: Mapping[str, str] | None = None) -> bool:
    """Is developer mode on? The ONLY switch under which tfvars are read."""
    environ = os.environ if environ is None else environ
    return environ.get(ENV_CONFIG_FROM, "").strip().lower() == REPO_MODE


# -- where the file lives ----------------------------------------------------


def config_dir(environ: Mapping[str, str] | None = None) -> Path:
    """The per-user directory: never the working directory, never the repo.

    `SWARM_CONFIG_DIR` first (tests, and anyone who wants it elsewhere), then
    `$XDG_CONFIG_HOME/swarmcloud` on ANY platform -- a macOS user who set XDG
    meant it -- then `~/Library/Application Support/SwarmCloud` on macOS, which
    is where that platform keeps per-user application state, then
    `~/.config/swarmcloud`.
    """
    environ = os.environ if environ is None else environ
    explicit = environ.get(ENV_CONFIG_DIR, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    home = Path(environ.get("HOME") or Path.home())
    xdg = environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser() / "swarmcloud"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "SwarmCloud"
    return home / ".config" / "swarmcloud"


def config_path(environ: Mapping[str, str] | None = None) -> Path:
    return config_dir(environ) / "config.json"


# -- the shapes --------------------------------------------------------------

#: The context name keys the credential store (`<name>/refresh-token`), so it
#: may hold nothing a store key or a command line would read differently: no
#: slash, no leading dash, no whitespace.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")

#: The only keys `save` ever writes for a context. Anything else a caller puts
#: in a Context is dropped, which is what makes "the file holds no secret" a
#: property of this module rather than of every caller's care.
_CONTEXT_KEYS = ("url", "oauth_client_id")

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


@dataclass
class Context:
    name: str
    url: str
    oauth_client_id: str = ""


@dataclass
class Config:
    path: Path
    current: str | None = None
    contexts: dict[str, Context] = field(default_factory=dict)


@dataclass(frozen=True)
class Deployment:
    """One resolved answer to "which SwarmCloud, and how do I sign in to it".

    `context` is the name credentials are keyed by -- a stored context's name,
    or one derived from the host when the address came from the environment
    and no stored context has it. `source` says which layer answered, for
    `sc whoami` and `swarm doctor`. `client_secret` is only ever one supplied
    to THIS process (the environment, or the plugin); a stored secret is read
    from the credential store when it is needed. It is kept out of `repr` so a
    Deployment logged by mistake does not carry it.
    """

    context: str
    url: str
    client_id: str
    source: str
    front_door: bool
    current: bool = False
    client_secret: str = field(default="", repr=False)


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME.match(name):
        raise SwarmError(
            f"{name!r} is not a usable context name: letters, digits, '.', '_' and "
            "'-', starting with a letter or digit, at most 63 characters. It keys "
            "the credential store, so it cannot hold a slash or start with a dash"
        )
    return name


def normalise_url(url: str, *, strict: bool = True) -> str:
    """https://host[:port][/prefix], no trailing slash, no query, no fragment.

    HTTPS ONLY, except to this machine. The credential this bridge sends is a
    bearer token; over plain http it is on the wire for anyone between here
    and the host. Loopback http is allowed, for a swarm-api run locally.

    `strict=False` is for the LEGACY address variables only (SWARM_API_URL,
    API_URL), which have always been taken as given -- an in-VPC caller may
    name an internal address this module has no business refusing.
    """
    raw = (url or "").strip()
    parsed = urllib.parse.urlsplit(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("https", "http") or not host:
        raise SwarmError(
            f"{raw!r} is not a deployment URL; give the full address, e.g. "
            "https://swarm.example.com"
        )
    if not strict:
        return raw.rstrip("/")
    if parsed.scheme == "http" and host not in _LOOPBACK:
        raise SwarmError(
            f"{raw!r} is plain http. The bridge sends a bearer credential to this "
            "address, so it must be https (http is accepted only for 127.0.0.1)"
        )
    if parsed.query or parsed.fragment:
        raise SwarmError(f"{raw!r} carries a query or fragment; give the bare address")
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{netloc}{path}"


def is_run_app(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host.endswith(".run.app")


def context_name_for(url: str) -> str:
    """A context name for an address nobody named: its host (and port, and path).

    `https://swarm.saga.xyz` -> `swarm.saga.xyz`. Readable, stable, and one per
    deployment, which is all a name has to be.
    """
    parsed = urllib.parse.urlsplit(url)
    name = (parsed.netloc or "deployment").lower().replace(":", "-")
    path = parsed.path.strip("/").replace("/", "-")
    if path:
        name = f"{name}-{path}"
    name = re.sub(r"[^A-Za-z0-9._-]", "-", name).strip("-.") or "deployment"
    return name[:63]


def _front_door_for_configured(url: str, client_id: str) -> bool:
    """Is a deployment someone CONFIGURED behind IAP?

    A context, the plugin and SWARM_URL all name the address a person reaches
    from outside the VPC. For a `team` deployment that is only ever its load
    balancer, and this platform's load balancer always has IAP on
    (`terraform/modules/frontend` sets `iap { enabled = true }` on every
    backend); for a `solo` deployment it is the *.run.app address, where Cloud
    Run's own IAM is the gate. So: a client id, or any host that is not
    run.app, is an IAP front door.

    The LEGACY address variables do not go through this rule -- see
    `client.is_front_door` -- because SWARM_API_URL is how an in-VPC caller
    names the run.app address on purpose, and it has always been decided there.
    """
    return bool(client_id) or not is_run_app(url)


# -- the file ----------------------------------------------------------------


def load(environ: Mapping[str, str] | None = None) -> Config:
    """The config file, or an empty Config when there is none.

    A file that exists and cannot be parsed is an ERROR, not an empty config:
    "you have no contexts" and "your contexts could not be read" are different
    facts, and the second read as the first would send every call nowhere with
    a message about configuring something already configured.
    """
    path = config_path(environ)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return Config(path=path)
    except OSError as exc:
        raise SwarmError(f"could not read {path}: {exc.strerror or exc}") from exc
    try:
        body = json.loads(text)
    except json.JSONDecodeError as exc:
        # Spelled for this install, as every command the bridge hands back is
        # (#189). Imported where it is used, the way `client` imports this
        # module, so reading a config never loads the install probe.
        from .invocation import terminal_command

        raise SwarmError(
            f"{path} is not valid JSON ({exc.msg}, line {exc.lineno}). Fix or "
            f"delete it; `{terminal_command('sc context add')}` writes a fresh one"
        ) from exc
    if not isinstance(body, dict):
        raise SwarmError(f"{path} does not hold a JSON object")
    contexts: dict[str, Context] = {}
    for name, entry in (body.get("contexts") or {}).items():
        if not isinstance(entry, dict) or not isinstance(entry.get("url"), str):
            raise SwarmError(f"{path}: context {name!r} has no url")
        contexts[name] = Context(
            name=name,
            url=entry["url"],
            oauth_client_id=str(entry.get("oauth_client_id") or ""),
        )
    current = body.get("current_context")
    return Config(
        path=path,
        current=current if isinstance(current, str) and current in contexts else None,
        contexts=contexts,
    )


def save(config: Config) -> None:
    """Write atomically, 0600, in a 0700 directory, with the allow-listed keys only.

    Atomic because a half-written config is a corrupt one, and `load` refuses
    corrupt files rather than guessing. 0600 although nothing secret is in it:
    the file still says which deployments this person reaches, and nothing is
    lost by keeping it theirs.
    """
    directory = config.path.parent
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    body: dict[str, Any] = {
        "current_context": config.current,
        "contexts": {
            name: {
                key: value
                for key, value in (("url", c.url), ("oauth_client_id", c.oauth_client_id))
                if key in _CONTEXT_KEYS and value
            }
            for name, c in sorted(config.contexts.items())
        },
    }
    fd, temporary = tempfile.mkstemp(prefix=".config-", dir=directory)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(body, handle, indent=2)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, config.path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def add_context(
    name: str,
    url: str,
    *,
    client_id: str = "",
    environ: Mapping[str, str] | None = None,
) -> tuple[Context, bool]:
    """Add or update a context. Returns it, and whether it was new.

    The FIRST context becomes current: a person with exactly one deployment
    should not need a second command before the first one works. After that,
    `current` moves only when they say so.
    """
    name = validate_name(name)
    context = Context(name=name, url=normalise_url(url), oauth_client_id=(client_id or "").strip())
    config = load(environ)
    created = name not in config.contexts
    config.contexts[name] = context
    if config.current is None:
        config.current = name
    save(config)
    return context, created


def use_context(name: str, environ: Mapping[str, str] | None = None) -> Context:
    config = load(environ)
    if name not in config.contexts:
        raise SwarmError(_unknown(name, config))
    config.current = name
    save(config)
    return config.contexts[name]


def remove_context(name: str, environ: Mapping[str, str] | None = None, *, store: Any = None) -> Context:
    """Remove a context AND the credentials keyed by it.

    Leaving the refresh token behind would leave a working credential for a
    deployment the person has said they no longer use, under a name the next
    context with that name would silently inherit.
    """
    from . import credentials

    config = load(environ)
    if name not in config.contexts:
        raise SwarmError(_unknown(name, config))
    removed = config.contexts.pop(name)
    if config.current == name:
        config.current = None
    save(config)
    chosen = store if store is not None else credentials.store()
    for kind in (credentials.REFRESH_TOKEN, credentials.CLIENT_SECRET):
        chosen.delete(credentials.key(name, kind))
    return removed


def _unknown(name: str, config: Config) -> str:
    known = ", ".join(sorted(config.contexts)) or "none"
    return f"no context named {name!r} in {config.path} (known: {known})"


def _by_url(config: Config, url: str) -> Context | None:
    for context in config.contexts.values():
        if context.url == url:
            return context
    return None


# -- resolution --------------------------------------------------------------


def resolve(
    environ: Mapping[str, str] | None = None,
    *,
    context: str | None = None,
) -> Deployment | None:
    """Which deployment, by the six layers in the module docstring. None if none.

    None is an answer, not an error: the caller decides whether the solo path
    (PROJECT_ID, and Cloud Run asked directly) applies, and says what to
    configure when it does not.
    """
    environ = os.environ if environ is None else environ
    config = load(environ)
    env_id = _env(environ, ENV_CLIENT_ID)
    env_secret = _env(environ, ENV_CLIENT_SECRET)

    def _stored(stored: Context, source: str) -> Deployment:
        chosen_id = env_id or stored.oauth_client_id
        return Deployment(
            context=stored.name,
            url=stored.url,
            client_id=chosen_id,
            source=source,
            front_door=_front_door_for_configured(stored.url, chosen_id),
            current=stored.name == config.current,
            client_secret=env_secret,
        )

    def _address(
        url: str, source: str, *, front_door: bool | None, strict: bool = True
    ) -> Deployment:
        url = normalise_url(url, strict=strict)
        known = _by_url(config, url)
        chosen_id = env_id or (known.oauth_client_id if known else "")
        return Deployment(
            context=known.name if known else context_name_for(url),
            url=url,
            client_id=chosen_id,
            source=source,
            # A known sign-in client makes any address an IAP front door: IAP
            # is the only thing that takes the token that client mints.
            front_door=(
                _front_door_for_configured(url, chosen_id)
                if front_door is None
                else front_door or bool(chosen_id)
            ),
            current=bool(known) and known.name == config.current,
            client_secret=env_secret,
        )

    # 1. --context
    if context:
        if context not in config.contexts:
            raise SwarmError(_unknown(context, config))
        return _stored(config.contexts[context], f"context {context!r} (--context)")

    # 2. the environment's addresses: SWARM_URL, then the legacy four.
    url = _env(environ, ENV_URL)
    if url:
        return _address(url, ENV_URL, front_door=None)
    for name in LEGACY_URL_VARS:
        url = _env(environ, name)
        if url:
            from .client import is_front_door

            normalised = normalise_url(url, strict=False)
            return _address(normalised, name, front_door=is_front_door(normalised), strict=False)
    for name in LEGACY_HOST_VARS:
        host = _env(environ, name)
        if host:
            bare = host.removeprefix("https://").removeprefix("http://").rstrip("/")
            return _address(f"https://{bare}", name, front_door=True)

    # 3. SWARM_CONTEXT
    named = _env(environ, ENV_CONTEXT)
    if named:
        if named not in config.contexts:
            raise SwarmError(f"{ENV_CONTEXT}: " + _unknown(named, config))
        return _stored(config.contexts[named], f"context {named!r} ({ENV_CONTEXT})")

    # 4. developer mode: this checkout's tfvars, and nothing else reads them.
    if repo_mode(environ):
        from .client import front_door_host

        host = front_door_host()
        if host:
            return _address(
                f"https://{host}",
                f"this repository's tfvars ({ENV_CONFIG_FROM}={REPO_MODE})",
                front_door=True,
            )

    # 5. the plugin's userConfig
    plugin = _plugin(environ)
    if plugin is not None:
        return plugin_deployment(plugin, config, env_id=env_id, env_secret=env_secret)

    # 6. the current context
    if config.current:
        return _stored(config.contexts[config.current], f"current context in {config.path}")
    return None


@dataclass(frozen=True)
class _Plugin:
    url: str
    client_id: str
    client_secret: str = field(default="", repr=False)


def _plugin(environ: Mapping[str, str]) -> _Plugin | None:
    url = _env(environ, plugin_env(PLUGIN_URL))
    if not url:
        return None
    return _Plugin(
        url=normalise_url(url),
        client_id=_env(environ, plugin_env(PLUGIN_CLIENT_ID)),
        client_secret=_env(environ, plugin_env(PLUGIN_CLIENT_SECRET)),
    )


def plugin_deployment(
    plugin: _Plugin, config: Config, *, env_id: str = "", env_secret: str = ""
) -> Deployment:
    known = _by_url(config, plugin.url)
    client_id = env_id or plugin.client_id or (known.oauth_client_id if known else "")
    return Deployment(
        context=known.name if known else context_name_for(plugin.url),
        url=plugin.url,
        client_id=client_id,
        source="the sc plugin's configuration (/plugin configure)",
        front_door=_front_door_for_configured(plugin.url, client_id),
        current=bool(known) and known.name == config.current,
        client_secret=env_secret or plugin.client_secret,
    )


def seed_from_plugin(environ: Mapping[str, str] | None = None, *, store: Any = None) -> Deployment | None:
    """Write the plugin's deployment where a terminal can find it.

    Called by the MCP server at start-up. The non-secret half goes into the
    config file -- as an update to the context that already has this URL, or
    as a new context named after its host -- and the client secret, when the
    plugin was given one, into the credential store under that context. The
    plugin's context becomes current only when NOTHING is current (see the
    module docstring for why it never takes `current` from another).

    Returns the deployment as the plugin sees it, or None when the plugin was
    given no URL. Failing to write is the caller's to report and not fatal:
    the MCP server reads the plugin's values from its own environment whether
    or not the file could be written.
    """
    from . import credentials

    environ = os.environ if environ is None else environ
    plugin = _plugin(environ)
    if plugin is None:
        return None
    config = load(environ)
    known = _by_url(config, plugin.url)
    name = known.name if known else context_name_for(plugin.url)
    if known is None and name in config.contexts:
        # A different deployment already holds the derived name; keep both.
        name = validate_name(f"{name}-plugin")
    existing = config.contexts.get(name)
    client_id = plugin.client_id or (existing.oauth_client_id if existing else "")
    config.contexts[name] = Context(name=name, url=plugin.url, oauth_client_id=client_id)
    if config.current is None:
        config.current = name
    save(config)
    if plugin.client_secret:
        chosen = store if store is not None else credentials.store()
        slot = credentials.key(name, credentials.CLIENT_SECRET)
        if chosen.get(slot) != plugin.client_secret:
            chosen.set(slot, plugin.client_secret)
    return plugin_deployment(plugin, load(environ))
