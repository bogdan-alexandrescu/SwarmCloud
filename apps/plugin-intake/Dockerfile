# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
#
# swarm-plugin-intake -- the isolated Cloud Run job that checks one quarantined
# plugin archive and writes its pinned copy and report
# (docs/design/managed-plugins.md §2.2-§2.3, lane 4 of §17, part of #2688).
#
# Build from the REPOSITORY ROOT, as apps/swarm-graph/Dockerfile is:
#
#     docker build -f apps/plugin-intake/Dockerfile \
#         -t us-central1-docker.pkg.dev/$PROJECT/swarm-images/plugin-intake:$TAG .
#
# THE SAME POSTURE AS THE SERVICE IMAGES, AND SMALLER: every FROM carries a
# digest, and the program is the standard library only, so there is no
# wheelhouse, no lock and nothing resolved at build time. The job parses
# untrusted archives, so the image holds nothing an archive could use: no
# compiler, no pip, no shell tools beyond the base, no credential and one
# network client, plugin_intake/bucket.py: the standard library's http.client
# to the metadata server and Cloud Storage only, every other socket refused
# (tests/test_intake_pinned.py holds the imports). It runs as uid 10001 with a
# read-only root filesystem: it writes only to the plugins bucket, through the
# Cloud Storage API.
# The same base every service image pins (apps/swarm-graph/Dockerfile).
ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84
# Pulled through this project's Artifact Registry mirror; no default, so this
# file names no project (scripts/lib/build-mirror.sh mirror_build_args).
ARG MIRROR_DOCKER_HUB=

FROM ${MIRROR_DOCKER_HUB}/library/${PYTHON_IMAGE} AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/plugin-intake \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PATH="/usr/local/bin:/usr/bin:/bin"

# `apt-get upgrade` because a pinned base is a frozen set of CVEs, not a safe
# one (images/swarm-reconciler/Dockerfile). Nothing is installed: the base's
# CA bundle is all the job's one TLS peer (Cloud Storage) needs, and the build
# check below proves it is there.
RUN apt-get update \
 && apt-get upgrade --no-install-recommends -y \
 && rm -rf /var/lib/apt/lists/*

# Build tooling the base ships; the job imports none of it.
RUN /usr/local/bin/python3 -m pip uninstall --yes setuptools wheel \
 && /usr/local/bin/python3 -m pip uninstall --yes pip \
 && ! /usr/local/bin/python3 -c "import setuptools" 2>/dev/null \
 && ! /usr/local/bin/python3 -c "import pip" 2>/dev/null

COPY apps/plugin-intake/plugin_intake/ /opt/plugin-intake/plugin_intake/

# ABSOLUTE PATHS ON PURPOSE: the runtime PATH omits /usr/sbin.
RUN /usr/sbin/groupadd --gid 10001 swarm \
 && /usr/sbin/useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin swarm

USER 10001:10001
WORKDIR /opt/plugin-intake

# The build proves the job imports and refuses a non-archive without the
# bucket, and that TLS to Cloud Storage has the CA bundle to verify against.
RUN python -c "from plugin_intake.intake import run_intake; r, t = run_intake(b'not an archive', intake_id='0' * 32, scope='platform', tenant_id=None); assert r['refusal']['code'] == 'archive_format_unknown' and t is None; print('plugin-intake ok')" \
 && python -c "import ssl; assert ssl.create_default_context().cert_store_stats()['x509_ca'] > 0; print('ca bundle ok')"

ARG GIT_SHA=""
ARG BUILD_TIME=""
ENV GIT_SHA=${GIT_SHA} \
    BUILD_TIME=${BUILD_TIME}

# One execution, one intake: SWARM_INTAKE_ID and the rest come from the
# execution's environment, the bucket's name from PLUGINS_BUCKET on the job
# (terraform/infra/plugins.tf); the job reads and writes it through the Cloud
# Storage API over Private Google Access (plugin_intake/bucket.py).
ENTRYPOINT ["python", "-m", "plugin_intake"]
