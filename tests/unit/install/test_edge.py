"""Stage 2, edge and identity prerequisites (DAYONE-ENGINE-2; B02, design R6, lesson 17).

On a fake Google Cloud with no credentials: the OAuth brand has no API, so its
step opens the console and takes the Desktop client's id as its proof; the certificate
wait never starts before the hostname resolves to the reserved address, and is
bounded at 60 min; the IAP service agent is created by the engine.
"""

from __future__ import annotations

from pathlib import Path

from swarm_install.exitcodes import ExitCode
from swarm_install.steps import edge
from swarm_install.waits import CERTIFICATE

from .test_edge_probes import FakeEdge
from .test_preflight import NUMBER, PROJECT, Answers, FakeGcp, Rig

HOST = "swarm.example.com"
IP = "203.0.113.7"


def _rig(tmp_path: Path) -> Rig:
    rig = Rig(tmp_path)
    rig.store.update(lambda s: (setattr(s, "hostname", HOST),
                                s.outputs.update({"project_number": NUMBER, "global_ip": IP})))
    return rig


def test_brand_has_no_api_so_the_desktop_client_proves_it(tmp_path: Path) -> None:
    """B02: no API creates or reads the brand (the IAP OAuth Admin API is shut
    down). The step makes no Google call that writes, asks the Desktop
    client's id, and is met once it is recorded."""
    gcp = FakeGcp()
    rig = _rig(tmp_path)
    step = edge.BrandStep(lambda: gcp, Answers(edge_brand=["123456789012-abc.apps.googleusercontent.com"]),
                          lambda: FakeEdge())
    assert step.kind == "engine" and not hasattr(step, "wait")

    result = rig.engine([step]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert gcp.mutations == []
    plan = rig.of_kind("step_plan", "edge.brand")[0]
    assert any("console.cloud.google.com/auth/branding" in a for a in plan["actions"])


def test_dns_must_resolve_to_reserved_ip_before_cert_wait(tmp_path: Path) -> None:
    """Design R6: the certificate stays PROVISIONING while DNS points elsewhere,
    so its wait does not start until the hostname resolves to the reserved address."""
    gcp = FakeGcp()
    dns = FakeEdge()
    dns.answers[HOST] = {"198.51.100.1"}
    rig = _rig(tmp_path)
    result = rig.engine([edge.CertificateStep(lambda: gcp, Answers(), lambda: dns)]).run()
    assert result.exit_code == ExitCode.FAIL
    assert result.step == "edge.certificate"
    assert IP in result.message and "198.51.100.1" in result.message
    assert rig.of_kind("wait_countdown", "edge.certificate") == []
    assert gcp.called("ssl_certificate") == []

    dns.answers[HOST] = {IP}
    gcp.certs[edge.CERT] = {"managed": {"status": "ACTIVE"}, "inUseBy": ["proxy"]}
    rig = _rig(tmp_path / "resolves")
    assert rig.engine([edge.CertificateStep(lambda: gcp, Answers(), lambda: dns)]).run().exit_code == ExitCode.OK


def test_cert_wait_bound_60_min(tmp_path: Path) -> None:
    """The certificate wait is bounded at 60 min; past it the install pauses (exit 4)."""
    dns = FakeEdge()
    step = edge.CertificateStep(lambda: gcp, Answers(), lambda: dns)
    assert step.kind == "wait" and step.wait is CERTIFICATE and step.wait.bound_s == 60 * 60

    gcp = FakeGcp()
    dns.answers[HOST] = {IP}
    gcp.certs[edge.CERT] = {"managed": {"status": "PROVISIONING"}, "inUseBy": ["https-proxy"]}
    rig = _rig(tmp_path)
    started = rig.clock.now()
    result = rig.engine([step]).run()
    assert result.exit_code == ExitCode.PAUSED
    gave_up = rig.of_kind("wait_gave_up", "edge.certificate")
    assert gave_up and gave_up[0]["bound_s"] == 3600
    assert 3600 <= rig.clock.now() - started < 3600 + CERTIFICATE.interval_s + 1


def test_iap_service_agent_created(tmp_path: Path) -> None:
    """Lesson 17: without IAP's service agent the load balancer answers 'The IAP
    service account is not provisioned'; the engine creates it."""
    gcp = FakeGcp()
    fake = FakeEdge()
    rig = _rig(tmp_path)
    result = rig.engine([edge.IapAgentStep(lambda: gcp, Answers(), lambda: fake)]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert fake.writes == [("generate_service_identity", (PROJECT, "iap.googleapis.com"))]
    assert rig.store.load().outputs["iap_service_agent"] == f"service-{NUMBER}@gcp-sa-iap.iam.gserviceaccount.com"

    again = rig.engine([edge.IapAgentStep(lambda: gcp, Answers(), lambda: fake)]).run()
    assert again.exit_code == ExitCode.OK
    assert len(fake.writes) == 1  # idempotent: a met check never re-runs apply
