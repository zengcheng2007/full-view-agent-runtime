# pyright: reportArgumentType=false, reportCallIssue=false
"""Persistence for Run-scoped capability snapshots.

Stores the (run_id, capability_id, version) triples that made up the
pinned ``ToolRegistry`` when the Run started. The actual ``ToolRegistry``
object is rebuilt in-memory from the capability repository plus this
map — so we only need to persist the version triples.

Two implementations are provided:

* ``InMemoryRunCapabilitySnapshotStore`` — for tests / single-process.
  Does NOT survive restart.
* ``PostgresRunCapabilitySnapshotStore`` — writes to the
  ``run_capability_snapshots`` table (migration V012). Survives restart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from full_view_agent.domain.agent_definition import AgentExecutionPolicy

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PersistedRunCapabilitySnapshot:
    """The (run_id, tool_id -> version) triples captured at snapshot time."""

    run_id: str
    tool_versions: dict[str, str]
    captured_at: datetime
    skill_versions: dict[str, str] = field(default_factory=dict)
    workflow_versions: dict[str, str] = field(default_factory=dict)
    prompt_versions: dict[str, str] = field(default_factory=dict)
    application_prompt_versions: dict[str, str] = field(default_factory=dict)
    agent_prompt_versions: dict[str, str] = field(default_factory=dict)
    application_prompt_fingerprints: dict[str, str] = field(default_factory=dict)
    agent_prompt_fingerprints: dict[str, str] = field(default_factory=dict)
    knowledge_base_versions: dict[str, int] = field(default_factory=dict)
    static_tool_versions: dict[str, str] = field(default_factory=dict)
    application_scoped: bool = False
    agent_scoped: bool = False
    application_id: str | None = None
    execution_policy: AgentExecutionPolicy = field(default_factory=AgentExecutionPolicy)
    tool_contract_fingerprints: dict[str, str] = field(default_factory=dict)


_EMPTY_SENTINEL_CAPABILITY_ID = "__run_empty_capability_set__"
_SKILL_PREFIX = "__skill__:"
_WORKFLOW_PREFIX = "__workflow__:"
_PROMPT_PREFIX = "__prompt__:"
_APPLICATION_PROMPT_PREFIX = "__application_prompt__:"
_AGENT_PROMPT_PREFIX = "__agent_prompt__:"
_APPLICATION_PROMPT_FINGERPRINT_PREFIX = "__application_prompt_fingerprint__:"
_AGENT_PROMPT_FINGERPRINT_PREFIX = "__agent_prompt_fingerprint__:"
_KNOWLEDGE_PREFIX = "__knowledge__:"
_STATIC_TOOL_PREFIX = "__static_tool__:"
_APPLICATION_SCOPED_MARKER = "__application_scoped__"
_APPLICATION_ID_MARKER = "__application_id__"
_AGENT_SCOPED_MARKER = "__agent_scoped__"
_SNAPSHOT_HEADER = "__run_snapshot_header__"
_EXECUTION_POLICY_PREFIX = "__execution_policy__:"
_TOOL_CONTRACT_PREFIX = "__tool_contract__:"


class RunCapabilitySnapshotStore(Protocol):
    """Persist per-Run capability version maps."""

    async def store_if_absent(
        self, snapshot: PersistedRunCapabilitySnapshot
    ) -> PersistedRunCapabilitySnapshot:
        """Persist ``snapshot`` only if no snapshot exists yet for ``run_id``.

        Returns the winning snapshot — either the one just inserted
        (first writer) or the one already present (later writer).
        This gives callers atomic first-write-wins semantics across
        concurrent processes.

        An empty ``tool_versions`` map is recorded as a sentinel row so
        that ``load`` can distinguish "Run was snapshotted with no
        dynamic tools" from "Run has not been snapshotted yet".
        """
        ...

    async def load(self, run_id: str) -> PersistedRunCapabilitySnapshot | None:
        """Return the persisted snapshot for ``run_id`` or None."""
        ...

    async def delete(self, run_id: str) -> None:
        """Remove the persisted snapshot for ``run_id``. Idempotent."""
        ...


class InMemoryRunCapabilitySnapshotStore:
    """In-memory store for tests. Does NOT survive process restart."""

    def __init__(self) -> None:
        self._snapshots: dict[str, PersistedRunCapabilitySnapshot] = {}

    async def store_if_absent(
        self, snapshot: PersistedRunCapabilitySnapshot
    ) -> PersistedRunCapabilitySnapshot:
        existing = self._snapshots.get(snapshot.run_id)
        if existing is not None:
            return existing
        self._snapshots[snapshot.run_id] = snapshot
        return snapshot

    async def load(self, run_id: str) -> PersistedRunCapabilitySnapshot | None:
        return self._snapshots.get(run_id)

    async def delete(self, run_id: str) -> None:
        self._snapshots.pop(run_id, None)


class PostgresRunCapabilitySnapshotStore:
    """PostgreSQL-backed capability snapshot store (table ``run_capability_snapshots``)."""

    def __init__(
        self,
        *,
        dsn: str,
        schema: str = "full_view_agent",
    ) -> None:
        self._dsn = dsn
        self._schema = schema

    async def store_if_absent(
        self, snapshot: PersistedRunCapabilitySnapshot
    ) -> PersistedRunCapabilitySnapshot:
        import psycopg

        # Rows to insert: the real tool versions, plus a sentinel if
        # the tool_versions map is empty so that ``load`` can
        # distinguish "no capabilities" from "not yet snapshotted".
        rows: list[tuple[str, str]] = []
        rows.extend(snapshot.tool_versions.items())
        rows.extend(
            (f"{_SKILL_PREFIX}{capability_id}", version)
            for capability_id, version in snapshot.skill_versions.items()
        )
        rows.extend(
            (f"{_APPLICATION_PROMPT_PREFIX}{prompt_id}", version)
            for prompt_id, version in snapshot.application_prompt_versions.items()
        )
        rows.extend(
            (f"{_AGENT_PROMPT_PREFIX}{prompt_id}", version)
            for prompt_id, version in snapshot.agent_prompt_versions.items()
        )
        rows.extend(
            (f"{_APPLICATION_PROMPT_FINGERPRINT_PREFIX}{prompt_id}", fingerprint)
            for prompt_id, fingerprint in (
                snapshot.application_prompt_fingerprints.items()
            )
        )
        rows.extend(
            (f"{_AGENT_PROMPT_FINGERPRINT_PREFIX}{prompt_id}", fingerprint)
            for prompt_id, fingerprint in snapshot.agent_prompt_fingerprints.items()
        )
        rows.extend(
            (f"{_PROMPT_PREFIX}{prompt_id}", version)
            for prompt_id, version in snapshot.prompt_versions.items()
        )
        rows.extend(
            (f"{_KNOWLEDGE_PREFIX}{knowledge_base_id}", str(version))
            for knowledge_base_id, version in snapshot.knowledge_base_versions.items()
        )
        rows.extend(
            (f"{_WORKFLOW_PREFIX}{capability_id}", version)
            for capability_id, version in snapshot.workflow_versions.items()
        )
        rows.extend(
            (f"{_STATIC_TOOL_PREFIX}{tool_id}", version)
            for tool_id, version in snapshot.static_tool_versions.items()
        )
        if snapshot.application_scoped:
            rows.append((_APPLICATION_SCOPED_MARKER, "1"))
        if snapshot.application_id is not None:
            rows.append((_APPLICATION_ID_MARKER, snapshot.application_id))
        if snapshot.agent_scoped:
            rows.append((_AGENT_SCOPED_MARKER, "1"))
        rows.append(
            (
                _EXECUTION_POLICY_PREFIX,
                snapshot.execution_policy.model_dump_json(),
            )
        )
        rows.extend(
            (f"{_TOOL_CONTRACT_PREFIX}{tool_id}", fingerprint)
            for tool_id, fingerprint in snapshot.tool_contract_fingerprints.items()
        )
        if not rows:
            rows.append((_EMPTY_SENTINEL_CAPABILITY_ID, ""))

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            claim = await conn.execute(
                f"""
                INSERT INTO {self._schema}.run_capability_snapshots (
                    run_id, capability_id, version, captured_at
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (run_id, capability_id) DO NOTHING
                """,
                (snapshot.run_id, _SNAPSHOT_HEADER, "1", snapshot.captured_at),
            )
            if claim.rowcount != 1:
                winner = await self.load(snapshot.run_id)
                if winner is None:  # pragma: no cover - race with delete
                    return snapshot
                return winner
            for capability_id, version in rows:
                await conn.execute(
                    f"""
                    INSERT INTO {self._schema}.run_capability_snapshots (
                        run_id, capability_id, version, captured_at
                    ) VALUES (
                        %(run_id)s, %(capability_id)s, %(version)s, %(captured_at)s
                    )
                    ON CONFLICT (run_id, capability_id) DO NOTHING
                    """,
                    {
                        "run_id": snapshot.run_id,
                        "capability_id": capability_id,
                        "version": version,
                        "captured_at": snapshot.captured_at,
                    },
                )
        return snapshot

    async def load(self, run_id: str) -> PersistedRunCapabilitySnapshot | None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f"""
                SELECT capability_id, version, captured_at
                  FROM {self._schema}.run_capability_snapshots
                 WHERE run_id = %s
                """,
                (run_id,),
            )
            rows = await cursor.fetchall()
        if not rows:
            return None
        # Filter out the sentinel row (it's only there to mark presence).
        tool_versions = {
            row[0]: row[1]
            for row in rows
            if row[0] != _EMPTY_SENTINEL_CAPABILITY_ID
            and not row[0].startswith(_SKILL_PREFIX)
            and not row[0].startswith(_WORKFLOW_PREFIX)
            and not row[0].startswith(_PROMPT_PREFIX)
            and not row[0].startswith(_APPLICATION_PROMPT_PREFIX)
            and not row[0].startswith(_AGENT_PROMPT_PREFIX)
            and not row[0].startswith(_APPLICATION_PROMPT_FINGERPRINT_PREFIX)
            and not row[0].startswith(_AGENT_PROMPT_FINGERPRINT_PREFIX)
            and not row[0].startswith(_KNOWLEDGE_PREFIX)
            and not row[0].startswith(_STATIC_TOOL_PREFIX)
            and row[0] != _APPLICATION_SCOPED_MARKER
            and row[0] != _APPLICATION_ID_MARKER
            and row[0] != _AGENT_SCOPED_MARKER
            and row[0] != _SNAPSHOT_HEADER
            and row[0] != _EXECUTION_POLICY_PREFIX
            and not row[0].startswith(_TOOL_CONTRACT_PREFIX)
        }
        skill_versions = {
            row[0][len(_SKILL_PREFIX) :]: row[1]
            for row in rows
            if row[0].startswith(_SKILL_PREFIX)
        }
        workflow_versions = {
            row[0][len(_WORKFLOW_PREFIX) :]: row[1]
            for row in rows
            if row[0].startswith(_WORKFLOW_PREFIX)
        }
        prompt_versions = {
            row[0][len(_PROMPT_PREFIX) :]: row[1]
            for row in rows
            if row[0].startswith(_PROMPT_PREFIX)
        }
        application_prompt_versions = {
            row[0][len(_APPLICATION_PROMPT_PREFIX) :]: row[1]
            for row in rows
            if row[0].startswith(_APPLICATION_PROMPT_PREFIX)
        }
        agent_prompt_versions = {
            row[0][len(_AGENT_PROMPT_PREFIX) :]: row[1]
            for row in rows
            if row[0].startswith(_AGENT_PROMPT_PREFIX)
        }
        application_prompt_fingerprints = {
            row[0][len(_APPLICATION_PROMPT_FINGERPRINT_PREFIX) :]: row[1]
            for row in rows
            if row[0].startswith(_APPLICATION_PROMPT_FINGERPRINT_PREFIX)
        }
        agent_prompt_fingerprints = {
            row[0][len(_AGENT_PROMPT_FINGERPRINT_PREFIX) :]: row[1]
            for row in rows
            if row[0].startswith(_AGENT_PROMPT_FINGERPRINT_PREFIX)
        }
        knowledge_base_versions = {
            row[0][len(_KNOWLEDGE_PREFIX) :]: int(row[1])
            for row in rows
            if row[0].startswith(_KNOWLEDGE_PREFIX)
        }
        static_tool_versions = {
            row[0][len(_STATIC_TOOL_PREFIX) :]: row[1]
            for row in rows
            if row[0].startswith(_STATIC_TOOL_PREFIX)
        }
        # All rows for the same run share captured_at; take the first.
        captured_at = rows[0][2]
        return PersistedRunCapabilitySnapshot(
            run_id=run_id,
            tool_versions=tool_versions,
            captured_at=captured_at,
            skill_versions=skill_versions,
            workflow_versions=workflow_versions,
            prompt_versions=prompt_versions,
            application_prompt_versions=application_prompt_versions,
            agent_prompt_versions=agent_prompt_versions,
            application_prompt_fingerprints=application_prompt_fingerprints,
            agent_prompt_fingerprints=agent_prompt_fingerprints,
            knowledge_base_versions=knowledge_base_versions,
            static_tool_versions=static_tool_versions,
            application_scoped=any(
                row[0] == _APPLICATION_SCOPED_MARKER for row in rows
            ),
            agent_scoped=any(row[0] == _AGENT_SCOPED_MARKER for row in rows),
            application_id=next(
                (row[1] for row in rows if row[0] == _APPLICATION_ID_MARKER),
                None,
            ),
            execution_policy=next(
                (
                    AgentExecutionPolicy.model_validate_json(row[1])
                    for row in rows
                    if row[0] == _EXECUTION_POLICY_PREFIX
                ),
                AgentExecutionPolicy(),
            ),
            tool_contract_fingerprints={
                row[0][len(_TOOL_CONTRACT_PREFIX) :]: row[1]
                for row in rows
                if row[0].startswith(_TOOL_CONTRACT_PREFIX)
            },
        )

    async def delete(self, run_id: str) -> None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            await conn.execute(
                f"DELETE FROM {self._schema}.run_capability_snapshots"
                f" WHERE run_id = %s",
                (run_id,),
            )
