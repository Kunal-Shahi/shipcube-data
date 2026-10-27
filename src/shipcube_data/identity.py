"""Caller identity, as ShipCube's API sees it, and how it becomes a SecurityContext.

`Caller` mirrors the security workstream's schema (shipcube docs/research/database-design.md):
users, workspaces, memberships(role), api_tokens(scopes) and access_grants for app viewers.
The API builds it after verifying the token or the gateway's signed assertion; the data layer
never takes identity from tool arguments.

Mapping to the data layer:

* tenant      = the workspace (`ws_<uuid hex>`), unless a data attribute `tenant` assigns the
                caller to a sub-tenant inside the workspace (an app serving several customers).
* roles       = `workspace:<role>` for the membership role and every role below it, plus data
                roles from `data_role_assignments` matched by user, group, domain or workspace role.
* attrs       = `data_attributes` (most specific principal wins) plus built-ins: email, domain,
                user_id, workspace_id, workspace_slug, app_id.
"""

from __future__ import annotations

import re
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from .catalog import Catalog
from .compiler import SecurityContext
from .model import ModelError

WorkspaceRole = Literal["owner", "admin", "developer", "viewer"]
ROLE_ORDER = ["viewer", "developer", "admin", "owner"]
_TENANT_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class Caller(BaseModel):
    workspace_id: UUID
    workspace_slug: Optional[str] = None
    via: Literal["console", "mcp", "app"]
    email: str
    user_id: Optional[UUID] = None  # users.id; None for an app viewer who is not a ShipCube user
    hosted_domain: Optional[str] = None  # Google `hd` claim
    groups: list[str] = Field(default_factory=list)  # Google groups, when the gateway resolved them
    membership_role: Optional[WorkspaceRole] = None  # None = not a workspace member
    token_id: Optional[UUID] = None  # api_tokens.id when via == "mcp"
    scopes: list[str] = Field(default_factory=list)  # api_tokens.scopes
    app_id: Optional[UUID] = None  # apps.id when via == "app"

    @property
    def principal(self) -> str:
        """Audit principal, in the form Catalog.audit expects."""
        if self.token_id:
            return f"token:{self.token_id}"
        if self.user_id:
            return f"user:{self.user_id}"
        return f"viewer:{self.email}"

    @property
    def is_human_session(self) -> bool:
        return self.via == "console" and self.token_id is None and self.user_id is not None

    def has_scope(self, scope: str) -> bool:
        # Console sessions and app requests are bounded by role and IAP instead of token scopes.
        return self.via != "mcp" or scope in self.scopes

    def at_least(self, role: WorkspaceRole) -> bool:
        return self.membership_role is not None and ROLE_ORDER.index(self.membership_role) >= ROLE_ORDER.index(role)

    @property
    def domain(self) -> str:
        return (self.hosted_domain or self.email.rpartition("@")[2]).lower()

    def principals(self) -> list[tuple[str, str]]:
        out = [("user", self.email.lower()), ("domain", self.domain)]
        out += [("group", g.lower()) for g in self.groups]
        if self.membership_role:
            out += [("workspace_role", r) for r in ROLE_ORDER[: ROLE_ORDER.index(self.membership_role) + 1]]
        return out


def workspace_tenant(workspace_id: UUID) -> str:
    return f"ws_{workspace_id.hex}"


def security_context(caller: Caller, catalog: Catalog) -> SecurityContext:
    if catalog.workspace_id and str(caller.workspace_id) != catalog.workspace_id:
        raise ModelError("this catalog belongs to another workspace", "forbidden")
    if caller.membership_role is None and caller.via != "app":
        raise ModelError("only workspace members can use the data layer outside an app", "forbidden")
    data_roles, attrs = catalog.principal_grants(caller.principals())
    roles = sorted(data_roles | {f"workspace:{r}" for _, r in caller.principals() if _ == "workspace_role"})
    tenant = attrs.pop("tenant", None) or workspace_tenant(caller.workspace_id)
    if not isinstance(tenant, str) or not _TENANT_RE.match(tenant):
        raise ModelError(f"data attribute tenant={tenant!r} is not a valid identifier", "forbidden")
    builtins = {
        "email": caller.email.lower(),
        "domain": caller.domain,
        "user_id": str(caller.user_id) if caller.user_id else None,
        "workspace_id": str(caller.workspace_id),
        "workspace_slug": caller.workspace_slug,
        "app_id": str(caller.app_id) if caller.app_id else None,
    }
    return SecurityContext(
        tenant=tenant,
        user=caller.email.lower(),
        roles=roles,
        # Built-ins come last: a data attribute can never impersonate the verified identity.
        attrs={**attrs, **{k: v for k, v in builtins.items() if v is not None}},
    )
