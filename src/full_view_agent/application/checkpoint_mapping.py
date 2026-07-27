from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

# LangGraph reserves checkpoint_ns for graph/subgraph nesting.  The root graph
# must use its empty namespace; product isolation is carried by the deterministic
# thread_id prefix and the dedicated PostgreSQL schema.
CHECKPOINT_NAMESPACE = ""


def build_checkpoint_thread_id(run_id: str) -> str:
    return f"fva:run:{run_id}"


@dataclass(frozen=True)
class CheckpointThreadMapping:
    """Framework-neutral link between the product ledger and graph state."""

    run_id: str
    session_id: str
    owner_user_id: str
    thread_id: str
    checkpoint_ns: str
    orchestrator: Literal["langgraph"]
    checkpoint_id: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class CheckpointMappingStore(Protocol):
    async def ensure_mapping(
        self,
        *,
        user_id: str,
        run_id: str,
        session_id: str,
    ) -> CheckpointThreadMapping: ...

    async def get_mapping(
        self,
        *,
        user_id: str,
        run_id: str,
    ) -> CheckpointThreadMapping: ...

    async def record_checkpoint(
        self,
        *,
        user_id: str,
        run_id: str,
        checkpoint_id: str,
        expected_version: int,
    ) -> CheckpointThreadMapping: ...
