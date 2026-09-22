"""Which address the scripts talk to, and which credential they present.

THE BUG THIS FILE EXISTS FOR
----------------------------
`scripts/lib/common.sh:api_url()` resolved the control-plane address by asking
`gcloud run services describe swarm-api --format='value(status.url)'`, which
answers with the `*.run.app` hostname. swarm-api's ingress is
`internal-and-cloud-load-balancing`, so that hostname does not serve anyone
outside the VPC: Google's frontend refuses the request before it reaches the
container and renders the refusal as HTTP 404.

Measured against the live deployment on 2026-09-22:

    curl https://swarm-api-tonstldhta-uc.a.run.app/readyz                 -> 404
    curl -H 'Authorization: Bearer <id token>'  .../readyz                -> 404
    curl .../v1/workflows                                                 -> 404
    curl .../definitely-not-a-route                                       -> 404

all four byte-identical (272 bytes of Google's HTML 404 page). swarm-api's own
404 is FastAPI JSON, so none of those requests reached the application. The
same routes through the load balancer answered 302/401 from IAP -- i.e. they
exist and are served.

404 is the expensive part. `scripts/e2e-test.sh` reported "the API ... answered
HTTP 404, not 2xx. Rule out auth/IAM before running 'make deploy'", which sends
an operator to inspect IAM and then redeploy a control plane that is healthy.
The address was wrong, not the service.

THE SECOND-ORDER PROBLEM
------------------------
Pointing the scripts at the load balancer is not enough, because the load
balancer is behind IAP. Also measured 2026-09-22:

    user ID token                       -> 401 Invalid IAP credentials:
                                           Invalid bearer token.
                                           Invalid JWT audience.
    impersonated SA ID token minted for
      the client id in IAP's redirect   -> 401, the same message
    user ACCESS token                   -> 401, IAP error code 900
    impersonated SA ACCESS token        -> 403 "Access denied. For user
                                           swarm-verify@..."

Only the last got past authentication. `iap.oauth2ClientId` is empty on
`swarm-ui-backend`, so IAP is using a Google-managed OAuth client and there is
no client id an ID-token audience could name. The front door takes an OAuth
ACCESS token; swarm-api still identifies the caller, from the
`X-Goog-IAP-JWT-Assertion` IAP adds (apps/swarm-api/swarm_api/deps.py).

WHAT IS ASSERTED HERE
---------------------
Offline, with a fake `gcloud` on PATH. Nothing is created and no credential is
used. The four cases are the four answers that have to stay distinguishable:
the front door when there is one, the explicit override, the run.app address
when it really can serve, and a REFUSAL with the real reason when it cannot.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
COMMON = REPO / "scripts" / "lib" / "common.sh"

pytestmark = pytest.mark.skipif(
    not COMMON.exists() or shutil.which("bash") is None,
    reason="scripts/lib/common.sh and bash are both required",
)

PROJECT = "swarm-test-project"
RUN_URL = "https://swarm-api-tonstldhta-uc.a.run.app"

#: The live shape: ingress restricted to the load balancer. `describe` is asked
#: for the url and the ingress annotation in one call, separated by `|`.
GCLOUD_INTERNAL_LB = f"""#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  *"run services describe"*) echo "{RUN_URL}|internal-and-cloud-load-balancing" ;;
  *) : ;;
esac
exit 0
"""

#: A service that really is public. The run.app address serves, and refusing it
#: would be just as wrong as handing out an unreachable one.
GCLOUD_INGRESS_ALL = f"""#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  *"run services describe"*) echo "{RUN_URL}|all" ;;
  *) : ;;
esac
exit 0
"""


def _run(tmp: Path, snippet: str, *, fake_gcloud: str, env_extra: dict[str, str] | None = None,
         tfvars: str | None = None) -> subprocess.CompletedProcess:
    """Source common.sh with a fake gcloud and run one snippet against it."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir(parents=True)
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(fake_gcloud)
    gcloud.chmod(0o755)

    # common.sh refuses to source a group- or world-writable env file, and it is
    # right to: it sources it as shell.
    env_file = tmp / "env"
    env_file.write_text(
        f"PROJECT_ID={PROJECT}\nREGION=us-central1\nENVIRONMENT=itest\n"
    )
    env_file.chmod(0o600)

    # A private terraform environment, so the assertions do not move when Track C
    # edits dev.tfvars -- and so the "no front door" case can exist at all.
    if tfvars is not None:
        env_dir = REPO / "terraform" / "environments" / "itest"
        env_dir.mkdir(parents=True, exist_ok=True)
        (env_dir / "itest.tfvars").write_text(tfvars)

    script = tmp / "probe.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'. "{COMMON}"\n'
        f"{snippet}\n"
    )
    script.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["SWARM_ENV_FILE"] = str(env_file)
    env["NO_COLOR"] = "1"
    for key in ("API_URL", "API_HOST", "SWARM_ID_TOKEN", "SWARM_IMPERSONATE_SA"):
        env.pop(key, None)
    env.update(env_extra or {})

    return subprocess.run(
        ["bash", str(script)], cwd=REPO, env=env,
        capture_output=True, text=True, timeout=120,
    )


@pytest.fixture(autouse=True)
def _clean_itest_environment():
    """The private tfvars directory is this file's, and does not outlive it."""
    yield
    env_dir = REPO / "terraform" / "environments" / "itest"
    if env_dir.exists():
        shutil.rmtree(env_dir)


def test_the_front_door_is_preferred_over_the_cloud_run_url(tmp_path) -> None:
    """The regression. A deployed load balancer is the address, not run.app."""
    proc = _run(
        tmp_path, 'api_url; echo',
        fake_gcloud=GCLOUD_INTERNAL_LB,
        tfvars='enable_frontend   = true\nfrontend_hostname = "swarm.example.test"\n',
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "https://swarm.example.test", proc.stdout + proc.stderr
    assert RUN_URL not in proc.stdout, (
        "the run.app address answers 404 to every path from out here:\n" + proc.stdout
    )


def test_an_unreachable_cloud_run_url_is_refused_and_the_reason_is_the_ingress(tmp_path) -> None:
    """With no front door configured, the old code handed out a 404 machine.

    Refusing is the whole point: a returned URL becomes a 404 several layers
    later, where it reads as a missing route on a broken deployment.
    """
    proc = _run(
        tmp_path, 'api_url; echo',
        fake_gcloud=GCLOUD_INTERNAL_LB,
        tfvars="enable_frontend = false\n",
    )
    transcript = proc.stdout + proc.stderr

    assert proc.returncode != 0, transcript
    assert "internal-and-cloud-load-balancing" in transcript, (
        "the ingress is the reason and has to be named, or the next person "
        "debugs the 404 as a routing problem:\n" + transcript
    )
    assert "404" in transcript, transcript
    assert "API_HOST" in transcript, "the message has to say what to set:\n" + transcript
    assert proc.stdout.strip() == "", (
        "nothing may be printed on stdout: a caller doing url=$(api_url) would "
        "take it as the address:\n" + proc.stdout
    )


def test_a_genuinely_public_service_still_resolves_to_its_run_app_url(tmp_path) -> None:
    """The ingress check must not refuse an address that works."""
    proc = _run(
        tmp_path, 'api_url; echo',
        fake_gcloud=GCLOUD_INGRESS_ALL,
        tfvars="enable_frontend = false\n",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == RUN_URL, proc.stdout + proc.stderr


def test_api_url_still_wins_because_the_in_vpc_job_depends_on_it(tmp_path) -> None:
    """terraform/infra/verify.tf sets API_URL to the run.app address on purpose.

    From inside the VPC that address IS reachable, and the image has no gcloud
    to look anything up with. An override that the front door could beat would
    break the one verification target that runs in there.
    """
    proc = _run(
        tmp_path, 'api_url; echo',
        fake_gcloud=GCLOUD_INTERNAL_LB,
        env_extra={"API_URL": RUN_URL},
        tfvars='enable_frontend   = true\nfrontend_hostname = "swarm.example.test"\n',
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == RUN_URL, proc.stdout + proc.stderr


def test_the_front_door_is_given_an_access_token_and_cloud_run_an_id_token(tmp_path) -> None:
    """IAP refuses an ID token whatever its audience; Cloud Run requires one.

    Both token functions are stubbed, so nothing here mints a credential. What
    is under test is only which of the two api_credential reaches for.
    """
    stub = (
        "id_token() { printf %s ID-TOKEN; }\n"
        "access_token() { printf %s ACCESS-TOKEN; }\n"
        "api_credential; echo"
    )

    front = _run(
        tmp_path / "front", stub,
        fake_gcloud=GCLOUD_INTERNAL_LB,
        tfvars='enable_frontend   = true\nfrontend_hostname = "swarm.example.test"\n',
    )
    assert front.returncode == 0, front.stdout + front.stderr
    assert front.stdout.strip() == "ACCESS-TOKEN", (
        "IAP answered an ID token 401 'Invalid JWT audience' on 2026-09-22, "
        "including one impersonated for the client id in its own redirect:\n"
        + front.stdout + front.stderr
    )

    direct = _run(
        tmp_path / "direct", stub,
        fake_gcloud=GCLOUD_INGRESS_ALL,
        tfvars="enable_frontend = false\n",
    )
    assert direct.returncode == 0, direct.stdout + direct.stderr
    assert direct.stdout.strip() == "ID-TOKEN", direct.stdout + direct.stderr


def test_an_iap_refusal_is_not_reported_as_the_api_rejecting_the_caller(tmp_path) -> None:
    """A 401 from IAP and a 401 from swarm-api send people to different places.

    Untranslated, IAP's is read as a tenant or ALLOWED_DOMAINS problem in an
    application the request never reached.
    """
    proc = _run(
        tmp_path,
        "_api_explain_iap 401 'Invalid IAP credentials: Invalid bearer token. "
        "Invalid JWT audience.'",
        fake_gcloud=GCLOUD_INTERNAL_LB,
        tfvars="enable_frontend = false\n",
    )
    transcript = proc.stdout + proc.stderr
    assert proc.returncode == 0, transcript
    assert "IAP" in transcript, transcript
    assert "never reached the application" in transcript, transcript
    assert "roles/iap.httpsResourceAccessor" in transcript, (
        "the fix is a grant, and naming it is the difference between a minute "
        "and an afternoon:\n" + transcript
    )
    assert "SWARM_IMPERSONATE_SA" in transcript, (
        "an unauthenticated caller needs the variable to set, not only the role "
        "name:\n" + transcript
    )


def test_an_iap_authorisation_failure_names_the_list_the_principal_is_missing_from(
    tmp_path,
) -> None:
    """IAP's 403 is a DIFFERENT problem from its 401, and a different fix.

    403 means the credential was accepted and the principal is not on the
    access list -- which is `frontend_iap_members`, a Track C input. Collapsing
    it into the 401 advice sends someone to re-mint a token that was already
    fine. This branch is only reachable once the credential is right, so it is
    exactly the one a test would otherwise never enter.
    """
    proc = _run(
        tmp_path,
        "_api_explain_iap 403 'Access denied. For user "
        "swarm-verify@saga-agents-staging.iam.gserviceaccount.com.'",
        fake_gcloud=GCLOUD_INTERNAL_LB,
        tfvars="enable_frontend = false\n",
    )
    transcript = proc.stdout + proc.stderr
    assert proc.returncode == 0, transcript
    assert "roles/iap.httpsResourceAccessor" in transcript, transcript
    assert "frontend_iap_members" in transcript, (
        "the grant lives in Track C's tfvars; without the name nobody can find "
        "it:\n" + transcript
    )
    assert "SWARM_IMPERSONATE_SA" not in transcript, (
        "the credential was already accepted -- telling them to change it is "
        "the wrong afternoon:\n" + transcript
    )


#: curl, replaced. `-w '%{http_code}'` is all `require_platform` reads.
#:
#: The config arriving on stdin is KEPT. `-K -` is how the bearer is passed, so
#: this file is the only place a test can see which credential the gate chose --
#: and choosing the wrong one is invisible from the status code alone.
def _fake_curl(status: str) -> str:
    return f"""#!/usr/bin/env bash
set -euo pipefail
cat >"${{SWARM_FAKE_CURL_STDIN}}" 2>/dev/null || true
printf '%s' "{status}"
exit 0
"""


def _require_platform(tmp: Path, http_status: str) -> subprocess.CompletedProcess:
    """Drive scripts/lib/testlib.sh:require_platform against a fake curl."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir(parents=True)
    curl = bin_dir / "curl"
    curl.write_text(_fake_curl(http_status))
    curl.chmod(0o755)
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(GCLOUD_INTERNAL_LB)
    gcloud.chmod(0o755)

    env_file = tmp / "env"
    env_file.write_text(f"PROJECT_ID={PROJECT}\nREGION=us-central1\nENVIRONMENT=itest\n")
    env_file.chmod(0o600)

    script = tmp / "probe.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        # testlib.sh does not source common.sh; the suites source both, in this
        # order, and require_platform uses helpers from each.
        f'. "{COMMON}"\n'
        f'. "{REPO / "scripts" / "lib" / "testlib.sh"}"\n'
        # Everything require_platform needs that is not under test, stubbed --
        # no Firestore, no address lookup. BOTH credential functions are
        # stubbed, distinguishably: the gate must reach for api_credential, and
        # an ID token at an IAP front door is a 401 nobody can debug from the
        # status code.
        "require_fs_database() { :; }\n"
        "api_credential() { printf %s CREDENTIAL-VIA-API-CREDENTIAL; }\n"
        "id_token() { printf %s ID-TOKEN-WHICH-IAP-REFUSES; }\n"
        f'api_url() {{ printf %s "{RUN_URL}"; }}\n'
        "require_platform\n"
    )
    script.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["SWARM_ENV_FILE"] = str(env_file)
    env["NO_COLOR"] = "1"
    env["SWARM_FAKE_CURL_STDIN"] = str(tmp / "curl-stdin")
    for key in ("API_URL", "API_HOST"):
        env.pop(key, None)
    proc = subprocess.run(
        ["bash", str(script)], cwd=REPO, env=env,
        capture_output=True, text=True, timeout=120,
    )
    proc.curl_stdin = (  # type: ignore[attr-defined]
        (tmp / "curl-stdin").read_text() if (tmp / "curl-stdin").exists() else ""
    )
    return proc


def test_a_404_from_the_readiness_gate_does_not_send_anyone_to_make_deploy(tmp_path) -> None:
    """The sentence scripts/e2e-test.sh actually printed, and why it was wrong.

        fail the API at https://swarm-api-...run.app/readyz answered HTTP 404,
        not 2xx. Rule out auth/IAM before running 'make deploy'

    Nothing about that is actionable. The request never reached swarm-api: the
    edge refused it because the service's ingress is not 'all'. Checking IAM
    finds nothing wrong and `make deploy` redeploys a healthy control plane.
    `scripts/lib/deploy.sh` already knew this and said so at its own 404 branch;
    the readiness gate did not.
    """
    proc = _require_platform(tmp_path, "404")
    transcript = proc.stdout + proc.stderr

    assert proc.returncode != 0, transcript
    assert "ingress" in transcript, (
        "the ingress is the cause and has to appear, or the reader debugs a "
        "routing problem that does not exist:\n" + transcript
    )
    assert "wrong ADDRESS" in transcript, transcript
    assert "Rule out auth/IAM" not in transcript, (
        "that advice is what sent an operator at IAM and then at 'make deploy' "
        "for a healthy deployment:\n" + transcript
    )
    assert "nothing about the deployment is wrong" in transcript, transcript


def test_a_403_from_the_readiness_gate_is_still_reported_as_auth(tmp_path) -> None:
    """Separating 404 out must not disarm the case that really is IAM."""
    proc = _require_platform(tmp_path, "403")
    transcript = proc.stdout + proc.stderr

    assert proc.returncode != 0, transcript
    assert "auth/IAM problem" in transcript, transcript
    assert "wrong ADDRESS" not in transcript, transcript


def test_the_readiness_gate_passes_on_2xx(tmp_path) -> None:
    """And the whole gate still lets a healthy platform through."""
    proc = _require_platform(tmp_path, "200")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_readiness_gate_presents_the_credential_the_address_accepts(tmp_path) -> None:
    """An ID token at the front door is a 401 that looks like a tenant problem.

    The status code cannot show this, so the assertion is on what curl was
    actually handed. It also pins the OTHER property of that line: the bearer
    arrives through `-K -`, never in argv, where ps would show it.
    """
    proc = _require_platform(tmp_path, "200")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "CREDENTIAL-VIA-API-CREDENTIAL" in proc.curl_stdin, (
        "the gate must ask api_credential, which picks an access token at the "
        f"IAP front door and an ID token at Cloud Run:\n{proc.curl_stdin!r}"
    )
    assert "ID-TOKEN-WHICH-IAP-REFUSES" not in proc.curl_stdin, proc.curl_stdin
    assert proc.curl_stdin.startswith("header = "), (
        "the token must arrive as a curl config on stdin, not in argv:\n"
        f"{proc.curl_stdin!r}"
    )


def test_no_script_reaches_for_the_cloud_run_url_behind_api_urls_back() -> None:
    """One resolver, or the fix is one script deep.

    `gcloud run services describe ... status.url` inside scripts/ is how the
    unreachable address gets back into circulation. common.sh is the one place
    that may ask, because it is the one place that also checks the ingress.
    """
    offenders = []
    for path in sorted((REPO / "scripts").rglob("*.sh")):
        if path == COMMON:
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if "status.url" in line and "run services describe" in line:
                offenders.append(f"{path.relative_to(REPO)}:{number}: {line.strip()}")
    assert not offenders, (
        "these resolve the control-plane address themselves and so skip the "
        "ingress check in common.sh:\n  " + "\n  ".join(offenders)
    )
