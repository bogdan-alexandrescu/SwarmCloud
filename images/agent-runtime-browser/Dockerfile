# syntax=us-central1-docker.pkg.dev/saga-agents-staging/swarm-mirror-dockerhub/docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
#
# agent-runtime-browser -- agent-runtime-base plus Playwright and Chromium.
#
# Build the base first, then:
#
#     docker build -f images/agent-runtime-browser/Dockerfile \
#         --build-arg BASE_IMAGE=<base image ref, ideally by digest> \
#         -t us-central1-docker.pkg.dev/$PROJECT/swarm-images/agent-runtime-browser:$TAG .
#
# BASE_IMAGE is supplied by digest by the checked-in
# `images/agent-runtime-browser/cloudbuild.yaml`, which derives the base ref
# from the image being built, pulls it, and reads the digest back. The default
# below is a LOCAL convenience only -- in a Cloud Build worker there is no local
# `agent-runtime-base:latest`, so a build that falls through to it fails with a
# manifest-unknown pull error rather than silently using something else.
#
# This profile is the only one that does not run on Cloud Run Jobs. Chromium
# needs a large /dev/shm and Cloud Run does not let us size it, so the frozen
# catalogue pins `browser` to GKE Autopilot, where the Job template mounts 2Gi
# of tmpfs at /dev/shm -- which is also why the `browser` resource class is
# 16 GiB rather than 8: that tmpfs counts against the container's memory limit
# before Chromium has allocated a single tab.
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

# THE LIVE BROWSER'S DISPLAY SIDECAR (docs/design/live-browser.md §3.1, §5.6;
# lane L5, part of #1030). A `claude-code-browser` pod runs this same image
# twice: once as the worker (uid 10001, the ENTRYPOINT below) and once as the
# `display` native sidecar, which the dispatcher starts with its own command
# (`python -m agent_worker.livegate`) under uid 10002. It needs:
#
#   * Xvfb -- the virtual display Chromium draws on. The sidecar starts it with
#     `-nolisten tcp`, so its only socket is in the sidecar's own /tmp.
#   * the `swarm-display` user, uid/gid 10002, DISPLAY_UID in
#     apps/scheduler/scheduler/dispatch.py. The pod spec names the number so
#     the cluster enforces it; the image names it so the process has a passwd
#     entry (Chromium and Xvfb both look themselves up). No home and no login
#     shell: HOME is the sidecar's memory /tmp.
#   * the enterprise policy of §5.6, read by Chromium at start. Installed in
#     BOTH managed-policy directories: /etc/opt/chrome/ is Google Chrome's,
#     the one the design names, and /etc/chromium/ is Chromium's -- which is
#     what Playwright's bundled build is. A policy in the wrong one of the two
#     is silently ignored, so it goes in both. Root-owned and read-only to
#     every uid: a person in control, or the agent, must not edit it.
#     Two entries of the design's list are written in Chromium's URL-filter
#     syntax: `*.svc.cluster.local` is `svc.cluster.local` (a bare host
#     already covers its subdomains; a leading `*.` is not a valid filter),
#     and `cluster.local` is added for the rest of the cluster's names.
#     `chrome://newtab` is allowed back by URLAllowlist, which wins over the
#     blocklist.
RUN apt-get update \
 && apt-get install --no-install-recommends -y xvfb \
 && rm -rf /var/lib/apt/lists/* \
 && /usr/sbin/groupadd --gid 10002 swarm-display \
 && /usr/sbin/useradd --uid 10002 --gid 10002 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin swarm-display \
 && install -d -m 0755 /etc/opt/chrome/policies/managed/ /etc/chromium/policies/managed/
COPY --chmod=0444 images/agent-runtime-browser/chromium-policy.json /etc/opt/chrome/policies/managed/swarm.json
COPY --chmod=0444 images/agent-runtime-browser/chromium-policy.json /etc/chromium/policies/managed/swarm.json

# The agent's `swarm-handoff` (docs/design/live-browser.md §4.1; lane L4, part
# of #1030): it writes a request into the hand-off spool and waits for the
# worker's answer. Files only, no socket. Root-owned, executable by every uid,
# writable by none: the agent runs it and cannot change what it does.
COPY --chmod=0555 images/agent-runtime-browser/swarm-handoff /usr/local/bin/swarm-handoff

USER swarm:swarm
# NO `WORKDIR /workspace` HERE: the base sets it and this image inherits it.
# Restating it wrote BuildKit's empty layer (sha256:4f4fb700ef54...) a second
# time, and GKE does not stream an image with a duplicate layer, so every cold
# browser pod pulled all 1.25 GB first: 42 s of an 88 s start, measured
# 2026-10-13 (docs/browser-startup-speed.md §2.4). The release refuses such an
# image (scripts/image-sizes.sh --duplicate-layers);
# tests/unit/scripts/test_image_streaming.py holds this file to it.

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

# And that the display sidecar's pieces are where the dispatcher expects them:
# the uid it runs the sidecar as, Xvfb, and a policy Chromium can parse.
RUN test "$(id -u swarm-display)" = 10002 \
 && test -x /usr/bin/Xvfb \
 && python -c 'import json, sys; [json.load(open(p)) for p in sys.argv[1:]]' \
      /etc/opt/chrome/policies/managed/swarm.json /etc/chromium/policies/managed/swarm.json

ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "agent_worker"]
