class ApplicationError(Exception):
    code = "application_error"


class ResourceNotFound(ApplicationError):
    code = "resource_not_found"


class SessionActiveRunConflict(ApplicationError):
    code = "session_active_run_conflict"

    def __init__(self, active_run_id: str) -> None:
        super().__init__("session already has an active run")
        self.active_run_id = active_run_id


class RunStateConflict(ApplicationError):
    code = "run_state_conflict"


class IdempotencyConflict(ApplicationError):
    code = "idempotency_conflict"


class PolicyBindingMismatch(ApplicationError):
    code = "policy_binding_mismatch"


class InvalidAuthenticationTransport(ApplicationError):
    code = "invalid_authentication_transport"


class AuthenticationFailed(ApplicationError):
    code = "unauthenticated"


class IdentityProviderUnavailable(ApplicationError):
    code = "identity_provider_unavailable"


class WorkflowNotAvailable(ApplicationError):
    code = "workflow_not_available"


class AnalysisRequestRejected(ApplicationError):
    code = "analysis_request_rejected"


class AnalysisPlanningUnavailable(ApplicationError):
    code = "analysis_planning_unavailable"


class AnalysisExecutionUnavailable(ApplicationError):
    code = "analysis_execution_unavailable"


class CredentialUnavailable(ResourceNotFound):
    code = "credential_unavailable"


class ReauthenticationRequired(ApplicationError):
    code = "reauthentication_required"


class InputRequestClosed(RunStateConflict):
    code = "input_request_closed"


class EventHistoryExpired(ResourceNotFound):
    code = "event_history_expired"


class InvalidCursor(ApplicationError):
    code = "validation_error"


class ResultPayloadExpired(ResourceNotFound):
    code = "result_payload_expired"


class CommandClientMismatch(ApplicationError):
    code = "command_client_mismatch"


class CommandReceiptConflict(ApplicationError):
    code = "command_receipt_conflict"


class BudgetExceeded(ApplicationError):
    code = "budget_exceeded"

    def __init__(
        self,
        message: str,
        *,
        root_cause_code: str | None = None,
        root_cause_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.root_cause_code = root_cause_code
        self.root_cause_message = root_cause_message


class LoopDetected(ApplicationError):
    code = "loop_detected"


class SemanticValidationError(ApplicationError):
    code = "semantic_validation_error"


class UpstreamTimeout(ApplicationError):
    code = "upstream_timeout"


class UpstreamUnavailable(ApplicationError):
    code = "upstream_unavailable"


class UpstreamContractError(ApplicationError):
    code = "upstream_contract_error"


class ModelContractError(ApplicationError):
    code = "model_contract_error"


class ModelProviderTimeout(ApplicationError):
    code = "model_timeout"


class ModelProviderUnavailable(ApplicationError):
    code = "model_provider_unavailable"
