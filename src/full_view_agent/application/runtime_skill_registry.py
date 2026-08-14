"""Runtime selection contracts for published Skills.

A Skill is guidance plus a server-enforced Tool allow-list.  It is not a
hard-coded question template: the model may select a relevant Skill from the
published catalogue, while the runtime validates every wrapped Tool call.
"""

from __future__ import annotations

import json
from threading import RLock

from full_view_agent.application.dynamic_skill_workflow_bridge import (
    RuntimeSkillContract,
)

SKILL_INVOKE_TOOL_ID = "agent.invoke_skill"


class RuntimeSkillRegistry:
    """Thread-safe live registry with immutable per-Run snapshots."""

    def __init__(
        self, skills: tuple[RuntimeSkillContract, ...] = ()
    ) -> None:
        self._lock = RLock()
        self._skills = _validated_skills(skills)

    def replace(self, skills: tuple[RuntimeSkillContract, ...]) -> None:
        validated = _validated_skills(skills)
        with self._lock:
            self._skills = validated

    def bind_lock(self, lock: RLock) -> None:
        """Join the runtime's composite capability generation lock."""

        with self._lock:
            self._lock = lock

    def snapshot(self) -> RuntimeSkillRegistry:
        return RuntimeSkillRegistry(self.list())

    def list(self) -> tuple[RuntimeSkillContract, ...]:
        with self._lock:
            return self._skills

    def get(self, skill_id: str, version: str) -> RuntimeSkillContract:
        with self._lock:
            for skill in self._skills:
                if skill.skill_id == skill_id and skill.version == version:
                    return skill
        raise KeyError(f"skill is unavailable in this Run: {skill_id}@{version}")

    def available_for_tools(
        self, authorized_tool_ids: set[str]
    ) -> tuple[RuntimeSkillContract, ...]:
        """Return Skills whose complete allow-list is visible to this user.

        Partial Skills are hidden.  Otherwise a published workflow description
        could advertise a Tool that the current identity cannot execute.
        """

        return tuple(
            skill
            for skill in self.list()
            if set(skill.allowed_tool_ids).issubset(authorized_tool_ids)
        )

    @staticmethod
    def prompt_fragment(skills: tuple[RuntimeSkillContract, ...]) -> str:
        payload = [
            {
                "skill_id": skill.skill_id,
                "version": skill.version,
                "applicable_questions": list(skill.applicable_questions),
                "guidance": skill.guidance,
                "allowed_tool_ids": list(skill.allowed_tool_ids),
            }
            for skill in skills
        ]
        return (
            "可用的已发布 Skills 如下。它们是处理方法而不是固定问法；"
            "请根据用户目标判断是否适用。使用 Skill 时必须调用 "
            f"{SKILL_INVOKE_TOOL_ID}，不得绕过其 Tool 白名单："
            + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )


def _validated_skills(
    skills: tuple[RuntimeSkillContract, ...]
) -> tuple[RuntimeSkillContract, ...]:
    identities: set[tuple[str, str]] = set()
    for skill in skills:
        identity = (skill.skill_id, skill.version)
        if identity in identities:
            raise ValueError(f"duplicate runtime skill: {skill.skill_id}@{skill.version}")
        identities.add(identity)
    return tuple(skills)
