from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from full_view_agent.application.errors import ResourceNotFound, RunStateConflict
from full_view_agent.application.session_run_service import new_id
from full_view_agent.domain.prompt_template import (
    PromptLifecycleEvent,
    PromptTemplate,
    PromptTemplateStatus,
    RuntimePromptSnapshot,
)


class PromptTemplateRepository(Protocol):
    async def save(self, template: PromptTemplate) -> None: ...
    async def get(self, prompt_id: str, version: str) -> PromptTemplate | None: ...
    async def list(self, *, app_id: str | None = None) -> list[PromptTemplate]: ...
    async def append_event(self, event: PromptLifecycleEvent) -> None: ...
    async def list_events(
        self, *, prompt_id: str | None = None
    ) -> list[PromptLifecycleEvent]: ...


class PromptTemplateService:
    _TRANSITIONS: dict[PromptTemplateStatus, frozenset[PromptTemplateStatus]] = {
        "draft": frozenset({"testing"}),
        "testing": frozenset({"pending_approval", "draft"}),
        "pending_approval": frozenset({"published", "draft"}),
        "published": frozenset({"disabled"}),
        "disabled": frozenset(),
    }

    def __init__(self, repository: PromptTemplateRepository) -> None:
        self._repository = repository

    async def create(
        self,
        *,
        prompt_id: str,
        app_id: str,
        name: str,
        version: str,
        content: str,
        actor: str,
        reason: str,
    ) -> PromptTemplate:
        if await self._repository.get(prompt_id, version) is not None:
            raise RunStateConflict("prompt template version already exists")
        template = PromptTemplate(
            prompt_id=prompt_id,
            app_id=app_id,
            name=name,
            version=version,
            content=content,
            created_by=actor,
            updated_by=actor,
        )
        await self._repository.save(template)
        await self._repository.append_event(
            PromptLifecycleEvent(
                event_id=new_id("pev"),
                prompt_id=prompt_id,
                version=version,
                to_status="draft",
                actor=actor,
                reason=_required_reason(reason),
            )
        )
        return template

    async def list(self, *, app_id: str | None = None) -> list[PromptTemplate]:
        return await self._repository.list(app_id=app_id)

    async def get_template(
        self, prompt_id: str, version: str
    ) -> PromptTemplate | None:
        """Return one exact template version for release validation."""
        return await self._repository.get(prompt_id, version)

    async def list_events(
        self, *, prompt_id: str | None = None
    ) -> list[PromptLifecycleEvent]:
        return await self._repository.list_events(prompt_id=prompt_id)

    async def transition(
        self,
        *,
        prompt_id: str,
        version: str,
        to_status: PromptTemplateStatus,
        expected_etag: int,
        actor: str,
        reason: str,
    ) -> PromptTemplate:
        current = await self._repository.get(prompt_id, version)
        if current is None:
            raise ResourceNotFound("prompt template not found")
        if current.etag != expected_etag:
            raise RunStateConflict("prompt template etag conflict")
        if to_status not in self._TRANSITIONS[current.status]:
            raise RunStateConflict(
                f"invalid prompt transition: {current.status} -> {to_status}"
            )
        if to_status == "published":
            for other in await self._repository.list(app_id=current.app_id):
                if other.status == "published" and (
                    other.prompt_id != prompt_id or other.version != version
                ):
                    raise RunStateConflict(
                        "another prompt template is already published for this app"
                    )
        updated = current.model_copy(
            update={
                "status": to_status,
                "etag": current.etag + 1,
                "updated_by": actor,
                "updated_at": datetime.now(UTC),
            }
        )
        await self._repository.save(updated)
        await self._repository.append_event(
            PromptLifecycleEvent(
                event_id=new_id("pev"),
                prompt_id=prompt_id,
                version=version,
                from_status=current.status,
                to_status=to_status,
                actor=actor,
                reason=_required_reason(reason),
            )
        )
        return updated

    async def get_effective_template(self, *, app_id: str) -> PromptTemplate | None:
        published = [
            item
            for item in await self._repository.list(app_id=app_id)
            if item.status == "published"
        ]
        if not published:
            return None
        if len(published) != 1:
            raise RuntimeError("multiple published prompts for one application")
        return published[0]

    async def get_effective(self, *, app_id: str) -> RuntimePromptSnapshot | None:
        template = await self.get_effective_template(app_id=app_id)
        if template is None:
            return None
        return RuntimePromptSnapshot(
            prompt_id=template.prompt_id,
            app_id=template.app_id,
            version=template.version,
            content=template.content,
        )

    async def load_snapshot(
        self, *, prompt_id: str, version: str
    ) -> RuntimePromptSnapshot | None:
        template = await self._repository.get(prompt_id, version)
        if template is None:
            return None
        return RuntimePromptSnapshot(
            prompt_id=template.prompt_id,
            app_id=template.app_id,
            version=template.version,
            content=template.content,
        )


def _required_reason(reason: str) -> str:
    reason = reason.strip()
    if not reason:
        raise ValueError("reason must not be blank")
    return reason
