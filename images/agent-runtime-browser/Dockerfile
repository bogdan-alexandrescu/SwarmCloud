# syntax=docker/dockerfile:1.7
#
# agent-runtime-browser -- agent-runtime-base plus Playwright and Chromium.
#
# Build the base first, then:
#
#     docker build -f images/agent-runtime-browser/Dockerfile \
#         --build-arg BASE_IMAGE=<base image ref, ideally by digest> \
#         -t us-central1-docker.pkg.dev/$PROJECT/swarm-images/agent-runtime-browser:$TAG .
#
# CI passes BASE_IMAGE by digest, so a browser image always names the exact base
# it was built on -- the same pinning rule the base applies to Debian.
#
# This profile is the only one that does not run on Cloud Run Jobs. Chromium
# needs a large /dev/shm and Cloud Run does not let us size it, so the frozen
# catalogue pins `browser` to GKE Autopilot, where the Job template mounts 1Gi
# of tmpfs at /dev/shm.
#
# Chromium's own sandbox stays OFF: it needs user namespaces and CAP_SYS_ADMIN,
# which the pod security policy refuses. The isolation here is the pod --
# non-root, no privilege escalation, seccomp RuntimeDefault, every capability
# dropped, a per-tenant namespace with default-deny egress -- and handing back a
# capability to gain Chromium's inner sandbox would be a bad trade.
ARG BASE_IMAGE=agent-runtime-base:latest
FROM ${BASE_IMAGE} AS runtime

# Pinned on 2026-09-15 against PyPI.
ARG PLAYWRIGHT_VERSION=1.63.0

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    PLAYWRIGHT_SKIP_BROWSER_GC=1

USER root

# Chromium and its system libraries. `--with-deps` is what installs the ~100
# shared objects Chromium needs; doing it here, once, keeps it out of the
# attempt's critical path.
RUN pip install --no-cache-dir "playwright==${PLAYWRIGHT_VERSION}" \
 && mkdir -p "${PLAYWRIGHT_BROWSERS_PATH}" \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* \
 && chmod -R a+rX "${PLAYWRIGHT_BROWSERS_PATH}"

USER swarm:swarm
WORKDIR /workspace

# Prove the browser actually launches in this image rather than discovering it
# at 3am in a tenant's attempt.
RUN python - <<'PY'
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, chromium_sandbox=False,
                                args=["--disable-dev-shm-usage", "--no-zygote"])
    page = browser.new_page()
    page.set_content("<h1>swarm browser runtime ok</h1>")
    assert "swarm browser runtime ok" in page.inner_text("h1")
    browser.close()
print("chromium launches headless as a non-root user")
PY

ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "agent_worker"]
