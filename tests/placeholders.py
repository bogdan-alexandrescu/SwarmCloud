"""The one placeholder set for tests and docs, read from scripts/guards/placeholders.json.

docs/design/generic-repository.md section 3.1: the deployment guard
(scripts/guards/deployment_values.py) allowlists exactly these values, so a
test that uses them is never a finding, and a test that invents a value of
its own is. Import the names below instead of writing a value out: a test
that needs two projects uses PROJECT and SHARED_PROJECT, never a made-up
third. Credential-shaped values are not here; build those at run time
(`secrets.token_hex`).

Load it by path; `tests/` is not a package:

    spec = importlib.util.spec_from_file_location("placeholders", ROOT / "tests" / "placeholders.py")
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "guards" / "placeholders.json"

#: The whole file, as the guard reads it.
ALL: dict[str, Any] = json.loads(SOURCE.read_text(encoding="utf-8"))

PROJECT: str = ALL["projects"]["deployment"]
SHARED_PROJECT: str = ALL["projects"]["shared"]
PROJECT_NUMBER: str = ALL["project_numbers"]["deployment"]
SHARED_PROJECT_NUMBER: str = ALL["project_numbers"]["shared"]
REGION: str = ALL["region"]
ZONE: str = ALL["zone"]
ORG_DOMAIN: str = ALL["domains"]["org"]
OUTSIDE_DOMAIN: str = ALL["domains"]["outside"]
CONSOLE_HOST: str = ALL["hosts"]["console"]
API_HOST: str = ALL["hosts"]["api"]
OWNER: str = ALL["people"]["owner"]
ADMIN: str = ALL["people"]["admin"]
MEMBER: str = ALL["people"]["member"]
OUTSIDER: str = ALL["people"]["outsider"]
ADMINS_GROUP: str = ALL["groups"]["admins"]
TEAM_GROUP: str = ALL["groups"]["team"]
TENANT: str = ALL["tenants"][0]
TEAM_TENANT: str = ALL["tenants"][1]
STATE_BUCKET: str = ALL["buckets"][0]
ARTIFACT_BUCKET: str = ALL["buckets"][1]
TENANT_GIT_SECRET: str = ALL["secrets"]["tenant_git"]
OAUTH_CLIENT_ID: str = ALL["oauth_client_id"]
GITHUB_OWNER: str = ALL["github"]["owner"]
GITHUB_REPOSITORY: str = ALL["github"]["repository"]
GITHUB_APP_ID: str = ALL["github"]["app_id"]
GITHUB_INSTALLATION_ID: str = ALL["github"]["installation_id"]
GITHUB_REPOSITORY_ID: str = ALL["github"]["repository_id"]
PEER: dict[str, str] = dict(ALL["shared_project_resources"])
PRODUCT: str = ALL["product"]
TAILNET: str = ALL["tailnet"]
INSTALLER_CIDR: str = ALL["installer_cidr"]


def service_account(role: str, project: str = PROJECT) -> str:
    """`<role>@<project>.iam.gserviceaccount.com`, with the placeholder project."""
    return f"{role}@{project}.iam.gserviceaccount.com"
