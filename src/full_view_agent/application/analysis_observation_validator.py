"""Trusted verification of persisted analysis-step observation references."""

from full_view_agent.application.analysis_step_ledger import (
    AnalysisStepLedgerConflict,
)
from full_view_agent.application.errors import ResourceNotFound
from full_view_agent.application.ports import AgentStore
from full_view_agent.application.tool_observation_service import (
    durable_tool_result_id,
)


class AgentStoreAnalysisObservationValidator:
    """Bind a ledger terminal transition to immutable Result/Evidence rows."""

    def __init__(self, store: AgentStore) -> None:
        self._store = store

    async def validate(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        tool_call_id: str,
        result_id: str,
        evidence_ids: tuple[str, ...],
    ) -> None:
        del tenant_id  # tenant is part of the validated ledger/tool-call identity
        if result_id != durable_tool_result_id(
            run_id=run_id, tool_call_id=tool_call_id
        ):
            self._reject("STEP_RESULT_ID_MISMATCH")
        try:
            result = await self._store.get_result_for_run(
                user_id=user_id, run_id=run_id, result_id=result_id
            )
        except ResourceNotFound as exc:
            raise AnalysisStepLedgerConflict(
                "STEP_RESULT_NOT_STORED",
                "persisted step result is not owned by this run",
            ) from exc
        if tuple(result.evidence_ids) != evidence_ids or not evidence_ids:
            self._reject("STEP_EVIDENCE_SET_MISMATCH")
        for evidence_id in evidence_ids:
            try:
                evidence = await self._store.get_evidence_for_run(
                    user_id=user_id,
                    run_id=run_id,
                    evidence_id=evidence_id,
                )
            except ResourceNotFound as exc:
                raise AnalysisStepLedgerConflict(
                    "STEP_EVIDENCE_NOT_STORED",
                    "persisted step evidence is not owned by this run",
                ) from exc
            if (
                evidence.result_id != result.result_id
                or evidence.result_fingerprint != result.result_fingerprint
            ):
                self._reject("STEP_EVIDENCE_RESULT_MISMATCH")

    @staticmethod
    def _reject(code: str) -> None:
        raise AnalysisStepLedgerConflict(
            code,
            "persisted step references do not match the durable observation",
        )
