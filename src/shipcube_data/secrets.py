"""Secret resolvers: turn a `SecretRef` into a value at connect time.

* `gcp_secret_resolver()`: Google Secret Manager, using the runtime service account (ADC).
  Only pinned versions are read; the workspace/project checks happen in `datasources` first.
* `datasources.local_secret_resolver`: SHIPCUBE_DEV_MODE only, reads env vars.
"""

from __future__ import annotations

import base64
import os
from typing import Callable

from .datasources import LOCAL_SECRET_PROJECT, SecretRef, dev_mode, local_secret_resolver
from .model import ModelError
from .obs import log_event


def gcp_secret_resolver(timeout_s: float = 10.0) -> Callable[[SecretRef], str]:
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = AuthorizedSession(creds)

    def resolve(ref: SecretRef) -> str:
        url = f"https://secretmanager.googleapis.com/v1/{ref.resource_name}/versions/{ref.version}:access"
        for attempt in range(3):
            resp = session.get(url, timeout=timeout_s)
            if resp.status_code == 200:
                return base64.b64decode(resp.json()["payload"]["data"]).decode()
            if resp.status_code not in (429, 500, 502, 503, 504):
                break
            log_event("secret.retry", level=30, attempt=attempt + 1, status=resp.status_code)
        log_event("secret.error", level=40, secret_ref_id=ref.id, status=resp.status_code)
        raise ModelError(f"cannot read secret {ref.id} (HTTP {resp.status_code})", "no_datasource")

    return resolve


def resolver_from_env() -> Callable[[SecretRef], str]:
    """Local secrets in DEV_MODE; Secret Manager otherwise (local refs are still refused outside DEV_MODE)."""
    if dev_mode():
        gcp = None

        def resolve(ref: SecretRef) -> str:
            nonlocal gcp
            if ref.resource_name.startswith(f"projects/{LOCAL_SECRET_PROJECT}/"):
                return local_secret_resolver(ref)
            gcp = gcp or gcp_secret_resolver()
            return gcp(ref)

        return resolve
    if os.environ.get("SHIPCUBE_DATA_SECRET_PROJECT") is None:
        log_event("secret.unconfigured", level=30, detail="SHIPCUBE_DATA_SECRET_PROJECT is not set; warehouses with secrets will be refused")
    return gcp_secret_resolver()
