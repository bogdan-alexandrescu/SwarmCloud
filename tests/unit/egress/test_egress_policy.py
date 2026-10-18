"""What swarm-egress will carry to the vendor, and what it will not
(docs/browser-egress.md §2.3, §2.4, §3.1; lane EGRESS-SVC).

  * the bypass list: SwarmCloud's own hosts, GitHub and the rest go direct,
    so the vendor never sees them and we never pay per GB for them. The gate
    gives Chromium the same list as `--proxy-bypass-list`; swarm-egress
    refuses a CONNECT to any of them, so a gate that forgot the list fails
    closed instead of paying;
  * internal names and private addresses never reach the vendor;
  * HTTPS only: a CONNECT to any port but 443 is refused;
  * the sticky session id is opaque, stable for the attempt (or the task, on
    an ISP zone) and never shared by two tenants;
  * the vendor credential's password never appears in its repr.

Every password here is built at runtime: nothing is a credential.
"""

from __future__ import annotations

import base64
import json
import secrets

import pytest

from swarm_egress.config import ConfigInvalid, EgressConfig, load_config, parse_config
from swarm_egress.policy import TargetRefused, check_target, chromium_bypass_list
from swarm_egress.vendor import CredentialInvalid, parse_credential, session_id

BYPASS = ("github.com", ".github.com", ".githubusercontent.com", "console.swarm.example")


def _check(target: str, *, bypass=BYPASS, ports=frozenset({443})):
    return check_target(target, bypass_hosts=bypass, allowed_ports=ports)


def test_an_ordinary_https_host_is_carried():
    assert _check("www.google.com:443") == ("www.google.com", 443)
    assert _check("WWW.Google.COM:443") == ("www.google.com", 443)


@pytest.mark.parametrize("target", [
    "github.com:443",
    "api.github.com:443",
    "objects.githubusercontent.com:443",
    "console.swarm.example:443",
])
def test_a_bypassed_host_is_refused_so_it_goes_direct(target):
    with pytest.raises(TargetRefused) as refused:
        _check(target)
    assert refused.value.cause == "bypass"


def test_a_bypass_entry_without_a_dot_is_exact_and_not_a_suffix():
    assert _check("notgithub.com:443") == ("notgithub.com", 443)
    assert _check("github.com.evil.example:443") == ("github.com.evil.example", 443)


@pytest.mark.parametrize("target", [
    "localhost:443",
    "swarm-live:443",
    "swarm-api.swarm-system.svc.cluster.local:443",
    "metadata.google.internal:443",
    "printer.local:443",
    "10.0.0.5:443",
    "127.0.0.1:443",
    "169.254.169.254:443",
    "192.168.1.1:443",
    "[::1]:443",
    "[fd00::1]:443",
])
def test_an_internal_name_or_private_address_never_reaches_the_vendor(target):
    with pytest.raises(TargetRefused) as refused:
        _check(target)
    assert refused.value.cause == "internal"


@pytest.mark.parametrize("target", ["www.google.com:80", "www.google.com:22", "www.google.com:8443"])
def test_only_https_is_carried(target):
    with pytest.raises(TargetRefused) as refused:
        _check(target)
    assert refused.value.cause == "port"


@pytest.mark.parametrize("target", ["", "www.google.com", "www.google.com:", ":443",
                                    "a b.com:443", "www.google.com:99999", "bad_host!:443",
                                    "x" * 300 + ".com:443", "http://www.google.com/:443"])
def test_a_malformed_target_is_refused(target):
    with pytest.raises(TargetRefused) as refused:
        _check(target)
    assert refused.value.cause == "target"


def test_chromium_gets_the_same_list_as_its_bypass_flag():
    rendered = chromium_bypass_list(BYPASS)
    assert rendered.split(";") == ["<local>", "github.com", "*.github.com",
                                   "*.githubusercontent.com", "console.swarm.example"]


# -- the sticky session --------------------------------------------------


def test_the_session_id_is_stable_for_an_attempt_and_opaque():
    one = session_id("eng", "task_1", "att_1", sticky="attempt")
    assert one == session_id("eng", "task_1", "att_1", sticky="attempt")
    assert one != session_id("eng", "task_1", "att_2", sticky="attempt")
    assert one.isalnum() and len(one) <= 24
    for name in ("eng", "task_1", "att_1"):
        assert name not in one


def test_an_isp_zone_keeps_one_session_per_task_across_its_attempts():
    assert session_id("eng", "task_1", "att_1", sticky="task") == session_id(
        "eng", "task_1", "att_2", sticky="task")


def test_two_tenants_never_share_a_session():
    for sticky in ("attempt", "task"):
        assert session_id("eng", "t", "a", sticky=sticky) != session_id(
            "research", "t", "a", sticky=sticky)


# -- the vendor credential --------------------------------------------------


def _secret(**over) -> bytes:
    password = secrets.token_hex(12)
    body = {
        "vendor": "brightdata",
        "host": "brd.superproxy.io",
        "port": 44445,
        "tls": False,
        "zones": {
            "residential": {"username": "brd-customer-c1-zone-res", "password": password,
                            "sticky": "attempt"},
            "isp": {"username": "brd-customer-c1-zone-isp", "password": password,
                    "sticky": "task"},
        },
    }
    body.update(over)
    return json.dumps(body).encode()


def test_the_bright_data_username_carries_the_us_exit_and_the_session():
    credential = parse_credential(_secret())
    header = credential.proxy_authorization("residential", country="us", session="abc123")
    scheme, encoded = header.split(" ", 1)
    assert scheme == "Basic"
    user, password = base64.b64decode(encoded).decode().split(":", 1)
    assert user == "brd-customer-c1-zone-res-country-us-session-abc123"
    assert password == credential.zones["residential"].password


@pytest.mark.parametrize("port", [44445, 33335, 22225])
def test_the_vendor_endpoint_is_the_credentials_not_a_constant(port):
    # The owner's zone answers on brd.superproxy.io:44445 (2026-10-14); Bright
    # Data's other documented ports are 33335 and 22225. Whichever the
    # credential names is the one dialled: nothing here, in policy.py or in
    # the swarm-system manifests pins one (lane EGRESS-DISPATCH checked).
    credential = parse_credential(_secret(port=port))
    assert (credential.host, credential.port) == ("brd.superproxy.io", port)


def test_the_credential_never_shows_its_password_or_username():
    credential = parse_credential(_secret())
    for shown in (repr(credential), str(credential), repr(credential.zones["isp"])):
        assert credential.zones["isp"].password not in shown
        assert "brd-customer" not in shown


@pytest.mark.parametrize("over", [
    {"vendor": "someone-else"},
    {"host": ""},
    {"port": 0},
    {"zones": {}},
    {"zones": {"residential": {"username": "u", "password": "", "sticky": "attempt"}}},
    {"zones": {"residential": {"username": "u", "password": "p", "sticky": "forever"}}},
])
def test_a_credential_that_cannot_be_used_is_refused_without_quoting_it(over):
    raw = _secret(**over)
    with pytest.raises(CredentialInvalid) as refused:
        parse_credential(raw)
    assert raw.decode() not in str(refused.value)


def test_a_credential_that_is_not_json_is_refused_without_quoting_it():
    raw = secrets.token_hex(16).encode()
    with pytest.raises(CredentialInvalid) as refused:
        parse_credential(raw)
    assert raw.decode() not in str(refused.value)


# -- the platform configuration ------------------------------------------------


def _config(**over) -> dict:
    body = {
        "enabled": True,
        "country": "us",
        "tenants": ["eng"],
        "default_zone": "residential",
        "tenant_zones": {"research": "isp"},
        "monthly_cap_bytes": 18 * 10**9,
        "tenant_monthly_cap_bytes": 18 * 10**9,
        "attempt_cap_bytes": 10**9,
        "connects_per_minute": 600,
        "max_tunnels_per_attempt": 128,
        "bypass_hosts": list(BYPASS),
    }
    body.update(over)
    return body


def test_the_configuration_is_read_whole():
    config = parse_config(_config())
    assert config.enabled and config.country == "us"
    assert config.tenants == frozenset({"eng"})
    assert config.zone_for("research") == "isp" and config.zone_for("eng") == "residential"
    assert config.allowed_ports == frozenset({443})


def test_the_default_configuration_is_off():
    assert EgressConfig().enabled is False


@pytest.mark.parametrize("over", [
    {"country": "usa"},
    {"monthly_cap_bytes": 0},
    {"attempt_cap_bytes": -1},
    {"enabled": "yes"},
    {"tenants": "eng"},
    {"surprise": 1},
])
def test_a_configuration_that_does_not_parse_is_refused(over):
    with pytest.raises(ConfigInvalid):
        parse_config(_config(**over))


def test_an_unreadable_or_invalid_configuration_fails_closed(tmp_path):
    missing = load_config(tmp_path / "absent.json")
    assert missing.enabled is False and missing.cause == "config_unreadable"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_config(country="nowhere")))
    invalid = load_config(bad)
    assert invalid.enabled is False and invalid.cause == "config_invalid"
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_config()))
    assert load_config(good).enabled is True
