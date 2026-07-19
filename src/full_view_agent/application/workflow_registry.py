from full_view_agent.application.errors import WorkflowNotAvailable
from full_view_agent.domain.models import LegacyIdentitySnapshot, WorkflowDefinition, WorkflowRef


class WorkflowRegistry:
    def __init__(self, definitions: list[WorkflowDefinition] | None = None) -> None:
        self._definitions = {
            (definition.workflow_id, definition.workflow_version): definition
            for definition in definitions or []
        }

    @classmethod
    def default(cls) -> "WorkflowRegistry":
        return cls()

    def require_available(
        self,
        *,
        workflow_ref: WorkflowRef,
        identity: LegacyIdentitySnapshot,
    ) -> WorkflowDefinition:
        definition = self._definitions.get(
            (workflow_ref.workflow_id, workflow_ref.workflow_version)
        )
        if definition is None:
            raise WorkflowNotAvailable("workflow is not registered")
        if definition.status != "active":
            raise WorkflowNotAvailable("workflow is not active")
        if not set(definition.required_roles).issubset(identity.principal.roles):
            raise WorkflowNotAvailable("workflow is not available to this identity")
        return definition
