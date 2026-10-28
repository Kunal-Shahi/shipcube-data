"""Migration runner: status, dry-run with impact analysis, apply, rollback, propose/approve."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from .catalog import Catalog, _entities, _loads
from .compiler import Compiler, SecurityContext
from .model import Model, ModelError
from .ops import Migration, apply_ops, conflicts
from .obs import log_event
from .query import Query


class MigrationError(ModelError):
    def __init__(self, message: str, code: str = "invalid_migration", hint: str | None = None):
        super().__init__(message, code, hint)


def load_dir(path: str | Path) -> list[Migration]:
    out = []
    for f in sorted(Path(path).glob("*.json")):
        data = json.loads(f.read_text())
        data.setdefault("id", f.stem)
        if data["id"] != f.stem:
            raise MigrationError(f"{f.name}: id {data['id']!r} must match the file name")
        out.append(Migration.model_validate(data))
    return out


def diff_models(old: Model, new: Model) -> dict[str, list[str]]:
    before, after = _entities(old), _entities(new)
    label = lambda k: f"{k[0]}:{'.'.join(k[1])}"  # noqa: E731
    return {
        "added": sorted(label(k) for k in after.keys() - before.keys()),
        "changed": sorted(label(k) for k in after.keys() & before.keys() if after[k] != before[k]),
        "removed": sorted(label(k) for k in before.keys() - after.keys()),
    }


class Runner:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog

    # ------------------------------------------------------------ status

    def status(self, files: list[Migration]) -> list[dict[str, Any]]:
        rows = []
        known = {m["id"]: m for m in self.catalog.migrations()}
        for m in files:
            rec = known.pop(m.id, None)
            if rec is None:
                state = "pending"
            elif rec["status"] == "applied" and rec["checksum"] != m.checksum():
                state = "modified"  # edited after apply: an error
            else:
                state = rec["status"] if rec["status"] != "approved" else "approved"
            rows.append({"id": m.id, "state": state, "version": rec["version"] if rec else None, "kind": m.kind()})
        for rec in known.values():
            rows.append({"id": rec["id"], "state": rec["status"] if rec["status"] != "applied" else "applied (no file)",
                         "version": rec["version"], "kind": rec["kind"]})
        return sorted(rows, key=lambda r: r["id"])

    # ------------------------------------------------------------ planning

    def _base(self, m: Migration) -> int:
        if m.base_version is not None:
            return m.base_version
        applied = [r for r in self.catalog.migrations("applied")]
        later = [r for r in applied if r["id"] > m.id]
        if not later:
            return self.catalog.head()
        earlier = [r["version"] for r in applied if r["id"] < m.id]
        return max(earlier, default=0)

    def plan(self, m: Migration, *, saved: bool = True) -> dict[str, Any]:
        """Everything apply would do, without writing. Raises on invalid ops or conflicts."""
        prior = self.catalog.migration(m.id)
        if prior and prior["status"] == "applied":
            if prior["checksum"] != m.checksum():
                raise MigrationError(f"{m.id} was edited after it was applied (checksum changed); write a new migration instead", "checksum_mismatch")
            return {"id": m.id, "status": "already_applied", "version": prior["version"]}
        old = self.catalog.load()
        # Conflicts first: if a concurrent migration renamed or dropped what this one edits, the
        # useful answer is "conflict, rebase", not the op error that would follow.
        touched = set().union(*(op.touches(old) for op in m.ops))
        base = self._base(m)
        clashes = []
        for rec in self.catalog.applied_since(base):
            overlap = conflicts(touched, set(_loads(rec["touches"])))
            if overlap:
                clashes.append({"migration": rec["id"], "version": rec["version"], "touches": sorted(overlap)})
        if clashes:
            detail = "; ".join(f"{c['migration']} also changed {', '.join(c['touches'])}" for c in clashes)
            raise MigrationError(f"{m.id} conflicts with migrations applied after version {base}: {detail}. Rebase it on version {self.catalog.head()}", "conflict")
        new, touched_after = apply_ops(old, m.ops, self.catalog.dialect)
        touched |= touched_after
        report = {
            "id": m.id,
            "status": "planned",
            "kind": m.kind(),
            "widens_exposure": m.widens_exposure(),
            "summary": summarize(m.ops, new),
            "base_version": base,
            "head_version": self.catalog.head(),
            "rebased": base != self.catalog.head(),
            "touches": sorted(touched),
            "diff": diff_models(old, new),
            "fingerprint": {"before": old.fingerprint(), "after": new.fingerprint()},
        }
        if saved:
            report["impact"] = self.impact(old, new)
        return report | {"_new": new, "_touched": touched, "_old": old}

    def impact(self, old: Model, new: Model) -> dict[str, Any]:
        """Compile every saved query against both models and report what breaks or changes."""
        breaks, changes, warnings = [], [], []
        unaffected = 0
        dialect = self.catalog.dialect
        for sq in self.catalog.saved_queries():
            ctx = SecurityContext(tenant=sq["tenant"] or "impact_check", roles=sq["roles"])
            try:
                q = Query.model_validate(sq["query"])
                before = Compiler(old, ctx, dialect, dialect).compile(q)
            except ModelError:
                continue  # already broken before this migration; not our impact
            try:
                after_c = Compiler(new, ctx, dialect, dialect)
                after = after_c.compile(q)
            except ModelError as e:
                breaks.append({"query": sq["name"], "app": sq["app"], "error": str(e)})
                continue
            new_warn = [w for w in after.warnings if w not in before.warnings]
            if after.sql != before.sql:
                changes.append({"query": sq["name"], "app": sq["app"], "note": "same query, different SQL: results may change"})
            elif new_warn:
                warnings.append({"query": sq["name"], "app": sq["app"], "warnings": new_warn})
            else:
                unaffected += 1
        apps = sorted({x["app"] for x in breaks if x["app"]})
        return {"breaks": breaks, "changes": changes, "warnings": warnings, "unaffected": unaffected, "apps_broken": apps}

    @staticmethod
    def _public(report: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in report.items() if not k.startswith("_")}

    def dry_run(self, m: Migration, actor: Optional[str] = None) -> dict[str, Any]:
        report = self._public(self.plan(m))
        self.catalog.audit("dry_run", actor, m.id, kind=report.get("kind"), diff=report.get("diff"))
        return report | {"status": "dry_run" if report["status"] == "planned" else report["status"]}

    # ------------------------------------------------------------ apply / rollback

    def apply(self, m: Migration, actor: Optional[str] = None, *, require_approval: bool = False,
              allow_breaking: bool = True) -> dict[str, Any]:
        """Apply under the catalog lock. Everything is re-checked inside the lock (approval, checksum,
        conflicts, impact), so concurrent appliers serialise and the loser sees the winner's version."""
        with self.catalog.transaction() as cat:
            prior = cat.migration(m.id)
            if prior and prior["status"] == "applied":
                return self.plan(m, saved=False) | {"status": "already_applied"}
            if require_approval and (not prior or prior["status"] != "approved"):
                raise MigrationError(f"{m.id} needs human approval before it can be applied", "needs_approval")
            report = self.plan(m)
            if not allow_breaking and report["impact"]["breaks"]:
                names = ", ".join(b["query"] for b in report["impact"]["breaks"])
                raise MigrationError(f"{m.id} would break saved queries: {names}", "would_break")
            version, diff = cat.commit_version(m.id, report["_old"], report["_new"], actor)
            cat.upsert_migration(
                id=m.id, description=m.description, checksum=m.checksum(),
                ops=json.dumps([op.model_dump(mode="json", by_alias=True, exclude_defaults=True) for op in m.ops]),
                touches=json.dumps(sorted(report["_touched"])), kind=m.kind(), status="applied",
                base_version=report["base_version"], version=version,
                impact=json.dumps(report.get("impact")), applied_at=_now(),
            )
            cat.audit("apply", actor, m.id, version, diff=diff)
        log_event("migration.applied", migration_id=m.id, version=version, kind=m.kind(), actor=actor,
                  rebased=report["rebased"], workspace_id=self.catalog.workspace_id)
        return self._public(report) | {"status": "applied", "version": version}

    def apply_all(self, files: list[Migration], actor: Optional[str] = None) -> list[dict[str, Any]]:
        """Apply every file that is not already applied, rolled back or rejected, in id order."""
        status = {r["id"]: r["status"] for r in self.catalog.migrations()}
        done = {mid for mid, st in status.items() if st in {"applied", "rolled_back", "rejected"}}
        for m in files:
            if status.get(m.id) == "applied":
                self.plan(m, saved=False)  # raises if the file was edited after apply
        return [self.apply(m, actor) for m in files if m.id not in done]

    def rollback(self, actor: Optional[str] = None, *, dry_run: bool = False) -> dict[str, Any]:
        with self.catalog.transaction(lock=not dry_run) as cat:
            head = cat.head()
            if head == 0:
                raise MigrationError("nothing to roll back")
            rec = next(r for r in cat.migrations("applied") if r["version"] == head)
            old, new = cat.load(head), cat.load(self._previous(head))
            report = {"id": rec["id"], "version": head, "to_version": self._previous(head),
                      "diff": diff_models(old, new), "impact": self.impact(old, new)}
            if dry_run:
                return report | {"status": "dry_run"}
            cat.pop_version(head)
            cat.upsert_migration(id=rec["id"], status="rolled_back")
            cat.audit("rollback", actor, rec["id"], head)
        log_event("migration.rolled_back", migration_id=rec["id"], version=head, actor=actor,
                  workspace_id=self.catalog.workspace_id)
        return report | {"status": "rolled_back"}

    def _previous(self, version: int) -> int:
        live = [v["version"] for v in self.catalog.versions() if v["version"] < version and not v["rolled_back_at"]]
        return max(live, default=0)

    # ------------------------------------------------------------ agent proposals

    def propose(self, ops: list[dict[str, Any]], description: str, proposed_by: str,
                migration_id: Optional[str] = None, proposed_by_user: Optional[str] = None) -> dict[str, Any]:
        """Validate, dry-run and store a migration for a human to approve. Never applies it."""
        with self.catalog.transaction() as cat:  # the lock also makes id allocation race-free
            mid = migration_id or self._next_id(description)
            m = Migration.model_validate({"id": mid, "description": description, "base_version": cat.head(), "ops": ops})
            report = self._public(self.plan(m))
            cat.upsert_migration(
                id=m.id, description=description, checksum=m.checksum(),
                ops=json.dumps([op.model_dump(mode="json", by_alias=True, exclude_defaults=True) for op in m.ops]),
                touches=json.dumps(report["touches"]), kind=m.kind(), status="proposed",
                base_version=m.base_version, proposed_by=proposed_by, proposed_by_user=proposed_by_user,
                impact=json.dumps(report["impact"]),
            )
            cat.audit("propose", proposed_by, m.id, kind=m.kind())
        log_event("migration.proposed", migration_id=m.id, kind=m.kind(), actor=proposed_by,
                  breaks=len(report["impact"]["breaks"]), workspace_id=self.catalog.workspace_id)
        return report | {"status": "proposed", "migration": m.model_dump(mode="json", by_alias=True, exclude_defaults=True)}

    def approve(self, mid: str, approver: str, approver_user: Optional[str] = None) -> None:
        """Approve a proposal. The approver must be a different *person*: a token and its owner's
        console session are the same human (compared through `*_by_user`)."""
        with self.catalog.transaction() as cat:
            rec = cat.migration(mid)
            if not rec or rec["status"] != "proposed":
                raise MigrationError(f"{mid} is not a pending proposal", "not_found")
            same_principal = approver == rec["proposed_by"]
            same_person = bool(approver_user) and approver_user == rec.get("proposed_by_user")
            if same_principal or same_person:
                raise MigrationError("a proposal must be approved by someone other than the person who proposed it", "forbidden")
            cat.upsert_migration(id=mid, status="approved", approved_by=approver, approved_by_user=approver_user)
            cat.audit("approve", approver, mid)

    def reject(self, mid: str, approver: str, reason: str = "") -> None:
        with self.catalog.transaction() as cat:
            rec = cat.migration(mid)
            if not rec or rec["status"] not in {"proposed", "approved"}:
                raise MigrationError(f"{mid} is not a pending proposal", "not_found")
            cat.upsert_migration(id=mid, status="rejected", approved_by=approver)
            cat.audit("reject", approver, mid, reason=reason)

    def stored(self, mid: str) -> Migration:
        rec = self.catalog.migration(mid)
        if not rec:
            raise MigrationError(f"unknown migration {mid}", "not_found")
        return Migration.model_validate({"id": rec["id"], "description": rec["description"],
                                         "base_version": rec["base_version"], "ops": _loads(rec["ops"])})

    def _next_id(self, description: str) -> str:
        nums = [int(r["id"].split("_")[0]) for r in self.catalog.migrations() if r["id"][:4].isdigit()]
        slug = re.sub(r"[^a-z0-9]+", "_", description.lower()).strip("_")[:40] or "change"
        return f"{max(nums, default=0) + 1:04d}_{slug}"


def summarize(ops, model: Model) -> list[str]:
    """One plain-English line per op, for the console's review screen."""
    out = []
    for op in ops:
        d = op.model_dump(by_alias=True, exclude_none=True)
        kind = op.classify()
        name = d.get("name") or d.get("member") or d.get("cube") or d.get("target") or d.get("policy") or ""
        if op.op == "add_measure":
            cube, _, mname = name.partition(".")
            m = model.cubes[cube].measures.get(mname) if cube in model.cubes else None
            text = f"Add measure {name} = {m.agg}({m.sql or '*'})" + (f" where {m.filter}" if m and m.filter else "") if m else f"Add measure {name}"
        elif op.op == "add_dimension":
            cube, _, dname = name.partition(".")
            dim = model.cubes[cube].dimensions.get(dname) if cube in model.cubes else None
            text = f"Add {dim.type if dim else ''} dimension {name} = {d['sql']}".replace("  ", " ")
        elif op.op == "create_cube":
            text = f"Create cube {name} on {d.get('table') or 'a SQL query'}"
        elif op.op == "rename_member":
            text = f"Rename {name} to {d['to']} (old name keeps working as an alias)"
        elif op.op == "change_sql":
            text = f"Change the SQL of {name} to {d['sql']}"
        elif op.op == "add_join":
            text = f"Join {d['cube']} to {d['to']} ({d.get('relationship', 'many_to_one')}) on {d.get('on') or d.get('key')}"
        elif op.op == "bind_schema":
            text = f"Route cube {d.get('cube', '*')} for tenant {d.get('tenant', '*')} to schema {d['schema']}"
        elif op.op == "grant_member":
            text = f"Restrict {name} to roles {', '.join(d['roles'])}"
        elif op.op == "add_row_policy":
            text = f"Add row policy {name} for roles {', '.join(d.get('roles') or ['everyone'])}"
        else:
            text = f"{op.op.replace('_', ' ').capitalize()} {name}".strip()
        out.append(f"[{kind}] {text}")
    return out


def _now() -> str:
    from .catalog import _now as now

    return now()
