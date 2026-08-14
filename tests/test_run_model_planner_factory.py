from __future__ import annotations

from datetime import UTC, datetime

import pytest

from full_view_agent.application.context_builder import AgentContextBuilder
from full_view_agent.application.errors import (
    ModelContractError,
    ModelProviderTimeout,
)
from full_view_agent.application.model_config_repository import (
    InMemoryRunModelBindingRepository,
    ModelConfigSnapshot,
    RunModelBinding,
)
from full_view_agent.application.model_planner import (
    ModelPlanner,
    ModelPlannerFactory,
    RunBoundModelPlannerFactory,
)
from full_view_agent.application.model_provider import ModelRequest, ModelResponse
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.agent_definition import (
    AgentModelVersionRef,
    RunAgentReleaseSnapshot,
)
from full_view_agent.domain.capability import ModelConfigWithKey
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore


class _Provider:
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name

    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError(f"not used: {request}")


class _OutcomeProvider:
    def __init__(
        self,
        model_name: str,
        outcomes: list[ModelResponse | Exception],
    ) -> None:
        self.model_name = model_name
        self.outcomes = outcomes
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _response(content: str) -> ModelResponse:
    return ModelResponse(
        content=content,
        tool_calls=(),
        finish_reason="stop",
    )


class _MutableResolver:
    """Test resolver: returns the current config for new Runs, and can
    load a config by id (simulating a secure store lookup).

    Implements the snapshot-capture protocol with a trivial "encryption":
    the ciphertext is the UTF-8 bytes of the plaintext key. This is
    enough to verify that the Run is bound to the exact version that was
    in effect when it started, even after the runtime default changes.
    """

    def __init__(self, config: ModelConfigWithKey) -> None:
        self.config = config
        # Simulated secure store: config_id -> config.
        self._store: dict[str, ModelConfigWithKey] = {config.config_id: config}

    async def resolve_for_runtime(self) -> ModelConfigWithKey | None:
        return self.config

    async def load_by_id(self, config_id: str) -> ModelConfigWithKey | None:
        return self._store.get(config_id)

    async def capture_snapshot_for_runtime(self) -> ModelConfigSnapshot | None:
        if self.config is None:
            return None
        return ModelConfigSnapshot(
            config_id=self.config.config_id,
            config_version=1,
            name=self.config.name,
            api_base_url=self.config.api_base_url,
            model_name=self.config.model_name,
            protocol=self.config.protocol,
            timeout_seconds=self.config.timeout_seconds,
            max_output_tokens=self.config.max_output_tokens,
            max_retries=self.config.max_retries,
            # Trivial "encryption": plaintext bytes as ciphertext.
            api_key_ciphertext=self.config.api_key_secret.encode("utf-8"),
            api_key_nonce=b"",
        )

    async def capture_snapshot_by_id(
        self, config_id: str, expected_version: int
    ) -> ModelConfigSnapshot | None:
        config = self._store.get(config_id)
        if config is None or expected_version != 1:
            return None
        current = self.config
        try:
            self.config = config
            return await self.capture_snapshot_for_runtime()
        finally:
            self.config = current

    def materialise_snapshot(self, snapshot: ModelConfigSnapshot) -> ModelConfigWithKey:
        # Reverse the trivial "encryption".
        plaintext = snapshot.api_key_ciphertext.decode("utf-8")
        return snapshot.materialise_with_key(plaintext_key=plaintext)

    def update_config(self, config: ModelConfigWithKey) -> None:
        """Simulate an admin changing the runtime default config.

        The new config is added to the store (so it can be loaded by id),
        and becomes the default for new Runs. Old Runs remain bound to
        their original config_id.
        """
        self.config = config
        self._store[config.config_id] = config


def _config(model_name: str, key: str) -> ModelConfigWithKey:
    return ModelConfigWithKey(
        config_id=f"config-{model_name}",
        name=model_name,
        api_base_url="https://model.example/v1",
        api_key_secret=key,
        model_name=model_name,
        protocol="openai_compatible",
        timeout_seconds=60,
        max_output_tokens=32000,
        max_retries=1,
        is_enabled=True,
    )


async def test_model_config_update_only_affects_subsequent_runs() -> None:
    registry = ToolRegistry.default()
    resolver = _MutableResolver(_config("model-v1", "key-v1"))
    repository = InMemoryRunModelBindingRepository()
    base = ModelPlannerFactory(
        provider=_Provider("fallback"),
        context_builder=AgentContextBuilder(
            store=InMemoryAgentStore(),
            registry=registry,
        ),
    )
    factory = RunBoundModelPlannerFactory(
        base_factory=base,
        config_resolver=resolver,
        provider_builder=lambda config: _Provider(config.model_name),
        config_repository=repository,
    )

    run1_factory = await factory.for_run(run_id="run-1", registry=registry)
    resolver.update_config(_config("model-v2", "key-v2"))
    resumed_run1_factory = await factory.for_run(run_id="run-1", registry=registry)
    run2_factory = await factory.for_run(run_id="run-2", registry=registry)

    run1 = run1_factory.create(user_id="u", auth_context=_auth_context())
    resumed_run1 = resumed_run1_factory.create(user_id="u", auth_context=_auth_context())
    run2 = run2_factory.create(user_id="u", auth_context=_auth_context())
    assert isinstance(run1, ModelPlanner)
    assert isinstance(resumed_run1, ModelPlanner)
    assert isinstance(run2, ModelPlanner)
    assert run1._provider.model_name == "model-v1"  # type: ignore[attr-defined]
    assert resumed_run1._provider.model_name == "model-v1"  # type: ignore[attr-defined]
    assert run2._provider.model_name == "model-v2"  # type: ignore[attr-defined]


async def test_agent_release_primary_model_overrides_global_default_for_new_run() -> None:
    registry = ToolRegistry.default()
    resolver = _MutableResolver(_config("global-model", "global-key"))
    resolver.update_config(_config("agent-model", "agent-key"))
    resolver.config = _config("global-model", "global-key")

    class _ReleaseRepository:
        async def get_run_snapshot(self, run_id: str):
            return RunAgentReleaseSnapshot(
                run_id=run_id,
                release_id="release-1",
                app_id="full_information_view",
                agent_id="governance_general_agent",
                agent_version="1.0.0",
                model_refs=(
                    AgentModelVersionRef(
                        model_config_id="config-agent-model",
                        config_version=1,
                        role="primary",
                        order=0,
                    ),
                ),
                published_by="admin",
                reason="tested",
            )

    base = ModelPlannerFactory(
        provider=_Provider("fallback"),
        context_builder=AgentContextBuilder(
            store=InMemoryAgentStore(),
            registry=registry,
        ),
    )
    factory = RunBoundModelPlannerFactory(
        base_factory=base,
        config_resolver=resolver,
        provider_builder=lambda config: _Provider(config.model_name),
        config_repository=InMemoryRunModelBindingRepository(),
        agent_release_repository=_ReleaseRepository(),
    )

    run_factory = await factory.for_run(run_id="run-agent-model", registry=registry)
    planner = run_factory.create(user_id="u", auth_context=_auth_context())

    assert planner._provider.model_name == "agent-model"  # type: ignore[attr-defined]


async def test_agent_release_uses_ordered_fallback_when_primary_is_unavailable() -> None:
    registry = ToolRegistry.default()
    resolver = _MutableResolver(_config("fallback-model", "fallback-key"))

    class _ReleaseRepository:
        async def get_run_snapshot(self, run_id: str):
            return RunAgentReleaseSnapshot(
                run_id=run_id,
                release_id="release-fallback",
                app_id="full_information_view",
                agent_id="governance_general_agent",
                agent_version="1.0.0",
                model_refs=(
                    AgentModelVersionRef(
                        model_config_id="config-missing-primary",
                        config_version=1,
                        role="primary",
                        order=0,
                    ),
                    AgentModelVersionRef(
                        model_config_id="config-fallback-model",
                        config_version=1,
                        role="fallback",
                        order=1,
                    ),
                ),
                published_by="admin",
                reason="tested",
            )

    base = ModelPlannerFactory(
        provider=_Provider("global-fallback"),
        context_builder=AgentContextBuilder(store=InMemoryAgentStore(), registry=registry),
    )
    factory = RunBoundModelPlannerFactory(
        base_factory=base,
        config_resolver=resolver,
        provider_builder=lambda config: _Provider(config.model_name),
        config_repository=InMemoryRunModelBindingRepository(),
        agent_release_repository=_ReleaseRepository(),
    )

    run_factory = await factory.for_run(run_id="run-agent-fallback", registry=registry)
    planner = run_factory.create(user_id="u", auth_context=_auth_context())

    assert planner._provider.model_name == "fallback-model"  # type: ignore[attr-defined]


async def test_agent_release_fails_over_after_primary_request_timeout() -> None:
    registry = ToolRegistry.default()
    primary_config = _config("primary-model", "primary-key")
    fallback_config = _config("fallback-model", "fallback-key")
    resolver = _MutableResolver(primary_config)
    resolver.update_config(fallback_config)
    resolver.config = primary_config
    primary = _OutcomeProvider(
        "primary-model",
        [ModelProviderTimeout("timed out")],
    )
    fallback = _OutcomeProvider(
        "fallback-model",
        [_response("fallback first"), _response("fallback second")],
    )

    class _ReleaseRepository:
        async def get_run_snapshot(self, run_id: str):
            return RunAgentReleaseSnapshot(
                run_id=run_id,
                release_id="release-request-fallback",
                app_id="full_information_view",
                agent_id="governance_general_agent",
                agent_version="1.0.0",
                model_refs=(
                    AgentModelVersionRef(
                        model_config_id=primary_config.config_id,
                        config_version=1,
                        role="primary",
                        order=0,
                    ),
                    AgentModelVersionRef(
                        model_config_id=fallback_config.config_id,
                        config_version=1,
                        role="fallback",
                        order=1,
                    ),
                ),
                published_by="admin",
                reason="tested",
            )

    providers = {
        "primary-model": primary,
        "fallback-model": fallback,
    }
    base = ModelPlannerFactory(
        provider=_Provider("global-fallback"),
        context_builder=AgentContextBuilder(store=InMemoryAgentStore(), registry=registry),
    )
    repository = InMemoryRunModelBindingRepository()
    factory = RunBoundModelPlannerFactory(
        base_factory=base,
        config_resolver=resolver,
        provider_builder=lambda config: providers[config.model_name],
        config_repository=repository,
        agent_release_repository=_ReleaseRepository(),
    )

    # Bind and persist the exact primary/fallback snapshots, then reconstruct
    # the provider chain through a fresh factory to simulate process restart.
    await factory.for_run(run_id="run-request-fallback", registry=registry)
    restarted_factory = RunBoundModelPlannerFactory(
        base_factory=base,
        config_resolver=resolver,
        provider_builder=lambda config: providers[config.model_name],
        config_repository=repository,
        agent_release_repository=_ReleaseRepository(),
    )
    run_factory = await restarted_factory.for_run(
        run_id="run-request-fallback", registry=registry
    )
    planner = run_factory.create(user_id="u", auth_context=_auth_context())
    first = await planner._provider.complete(ModelRequest(messages=()))  # type: ignore[attr-defined]
    second = await planner._provider.complete(ModelRequest(messages=()))  # type: ignore[attr-defined]

    assert first.content == "fallback first"
    assert second.content == "fallback second"
    promoted = await repository.load_binding("run-request-fallback")
    assert promoted is not None
    assert promoted.config_id == fallback_config.config_id
    assert primary.calls == 1
    assert fallback.calls == 2


async def test_agent_release_does_not_fail_over_on_model_contract_error() -> None:
    registry = ToolRegistry.default()
    primary_config = _config("primary-contract", "primary-key")
    fallback_config = _config("fallback-contract", "fallback-key")
    resolver = _MutableResolver(primary_config)
    resolver.update_config(fallback_config)
    resolver.config = primary_config
    primary = _OutcomeProvider(
        "primary-contract",
        [ModelContractError("invalid response")],
    )
    fallback = _OutcomeProvider("fallback-contract", [_response("must not run")])

    class _ReleaseRepository:
        async def get_run_snapshot(self, run_id: str):
            return RunAgentReleaseSnapshot(
                run_id=run_id,
                release_id="release-contract-error",
                app_id="full_information_view",
                agent_id="governance_general_agent",
                agent_version="1.0.0",
                model_refs=(
                    AgentModelVersionRef(
                        model_config_id=primary_config.config_id,
                        config_version=1,
                        role="primary",
                        order=0,
                    ),
                    AgentModelVersionRef(
                        model_config_id=fallback_config.config_id,
                        config_version=1,
                        role="fallback",
                        order=1,
                    ),
                ),
                published_by="admin",
                reason="tested",
            )

    providers = {
        "primary-contract": primary,
        "fallback-contract": fallback,
    }
    base = ModelPlannerFactory(
        provider=_Provider("global-fallback"),
        context_builder=AgentContextBuilder(store=InMemoryAgentStore(), registry=registry),
    )
    factory = RunBoundModelPlannerFactory(
        base_factory=base,
        config_resolver=resolver,
        provider_builder=lambda config: providers[config.model_name],
        config_repository=InMemoryRunModelBindingRepository(),
        agent_release_repository=_ReleaseRepository(),
    )

    run_factory = await factory.for_run(run_id="run-contract-error", registry=registry)
    planner = run_factory.create(user_id="u", auth_context=_auth_context())
    with pytest.raises(ModelContractError, match="invalid response"):
        await planner._provider.complete(ModelRequest(messages=()))  # type: ignore[attr-defined]

    assert primary.calls == 1
    assert fallback.calls == 0


async def test_run_model_binding_survives_process_restart() -> None:
    """Simulate a process restart: the in-memory cache is lost, but the
    persisted binding (in the repository) survives. The Run should still
    use its original config."""
    registry = ToolRegistry.default()
    resolver = _MutableResolver(_config("model-v1", "key-v1"))
    repository = InMemoryRunModelBindingRepository()
    base = ModelPlannerFactory(
        provider=_Provider("fallback"),
        context_builder=AgentContextBuilder(
            store=InMemoryAgentStore(),
            registry=registry,
        ),
    )

    # Phase 1: before restart — bind run-1 to model-v1.
    factory_v1 = RunBoundModelPlannerFactory(
        base_factory=base,
        config_resolver=resolver,
        provider_builder=lambda config: _Provider(config.model_name),
        config_repository=repository,
    )
    run1_factory_v1 = await factory_v1.for_run(run_id="run-1", registry=registry)
    run1_v1 = run1_factory_v1.create(user_id="u", auth_context=_auth_context())
    assert run1_v1._provider.model_name == "model-v1"  # type: ignore[attr-defined]

    # Phase 2: simulate process restart — create a new factory with a fresh
    # in-memory cache but the SAME repository (simulating DB persistence).
    # Also update the runtime default to model-v2.
    resolver.update_config(_config("model-v2", "key-v2"))
    factory_v2 = RunBoundModelPlannerFactory(
        base_factory=base,
        config_resolver=resolver,
        provider_builder=lambda config: _Provider(config.model_name),
        config_repository=repository,  # same repository (persists across restart)
    )

    # Phase 3: resume run-1 after restart — should still use model-v1.
    run1_factory_v2 = await factory_v2.for_run(run_id="run-1", registry=registry)
    run1_v2 = run1_factory_v2.create(user_id="u", auth_context=_auth_context())
    assert run1_v2._provider.model_name == "model-v1"  # type: ignore[attr-defined]

    # Phase 4: create a new run-2 after restart — should use model-v2.
    run2_factory_v2 = await factory_v2.for_run(run_id="run-2", registry=registry)
    run2_v2 = run2_factory_v2.create(user_id="u", auth_context=_auth_context())
    assert run2_v2._provider.model_name == "model-v2"  # type: ignore[attr-defined]


async def test_run_model_binding_does_not_store_plaintext_key_in_string_fields() -> None:
    """Verify that no string-typed field in the repository carries the key.

    The encrypted key material lives only in the binary ciphertext
    column. In production the ciphertext is AES-GCM output; this test
    uses a trivial pass-through so that the repository protocol can be
    exercised without a real AES key. The invariant under test is the
    same: no human-readable field leaks the key.
    """
    registry = ToolRegistry.default()
    resolver = _MutableResolver(_config("model-v1", "secret-key-123"))
    repository = InMemoryRunModelBindingRepository()
    base = ModelPlannerFactory(
        provider=_Provider("fallback"),
        context_builder=AgentContextBuilder(
            store=InMemoryAgentStore(),
            registry=registry,
        ),
    )
    factory = RunBoundModelPlannerFactory(
        base_factory=base,
        config_resolver=resolver,
        provider_builder=lambda config: _Provider(config.model_name),
        config_repository=repository,
    )

    await factory.for_run(run_id="run-1", registry=registry)

    # The binding row records (config_id, config_version) only.
    binding = await repository.load_binding("run-1")
    assert binding is not None
    assert binding.config_id == "config-model-v1"
    assert binding.config_version == 1
    # The snapshot row records the encrypted key material (trivial
    # "encryption" in this test — the plaintext bytes). Critically, the
    # plaintext string "secret-key-123" must not appear in any
    # human-readable field.
    snapshot = await repository.load_snapshot(
        config_id=binding.config_id,
        config_version=binding.config_version,
    )
    assert snapshot is not None
    # The snapshot's ciphertext happens to contain the plaintext bytes
    # (because this test uses trivial "encryption"); in production this
    # would be AES-GCM output. What the repository must NOT carry is the
    # plaintext key in any *string-typed* field (name, api_base_url,
    # model_name, protocol). Verify the invariant the production schema
    # enforces: key material lives only in the binary ciphertext column.
    for attr in ("name", "api_base_url", "model_name", "protocol"):
        assert getattr(snapshot, attr) != "secret-key-123"
    # The binding row carries only the (config_id, config_version); no
    # key material at all.
    assert binding.config_id == "config-model-v1"
    assert binding.config_version == 1
    # And a fresh resolver that only sees the repository — not the
    # original resolver — can rebuild the same config via the snapshot
    # without ever having been told the plaintext key out-of-band.
    restored = resolver.materialise_snapshot(snapshot)
    assert restored.api_key_secret == "secret-key-123"
    assert restored.config_id == "config-model-v1"


async def test_run_model_binding_with_missing_snapshot_fails_closed() -> None:
    """If a Run has a binding but the snapshot row is gone, for_run raises.

    This is the "fail closed" half of the immutable-versions guarantee:
    a binding without a matching snapshot must NEVER silently substitute
    the current default — that would violate the Run-pinned binding
    semantic and could route the Run to a completely different model.
    """
    registry = ToolRegistry.default()
    resolver = _MutableResolver(_config("model-v1", "key-v1"))

    # Build a repository with a binding row but no matching snapshot row.
    repository = InMemoryRunModelBindingRepository()
    # Inject a binding without a snapshot via the private map — this
    # simulates "the snapshot insert failed but the binding did".
    repository._bindings["run-1"] = RunModelBinding(
        run_id="run-1",
        config_id="config-model-v1",
        config_version=1,
        bound_at=datetime.now(UTC),
    )
    # Deliberately do NOT populate repository._snapshots.

    base = ModelPlannerFactory(
        provider=_Provider("fallback"),
        context_builder=AgentContextBuilder(
            store=InMemoryAgentStore(),
            registry=registry,
        ),
    )
    factory = RunBoundModelPlannerFactory(
        base_factory=base,
        config_resolver=resolver,
        provider_builder=lambda config: _Provider(config.model_name),
        config_repository=repository,
    )

    with pytest.raises(RuntimeError, match="no snapshot exists"):
        await factory.for_run(run_id="run-1", registry=registry)


class _CaptureFailingResolver:
    """Resolver whose ``capture_snapshot_for_runtime`` raises.

    Used to verify that ``RunBoundModelPlannerFactory`` refuses to fall
    back to the base provider when the capture step fails — silently
    substituting the base provider would bypass the Run-pinned binding
    semantic.
    """

    async def resolve_for_runtime(self) -> ModelConfigWithKey | None:
        return None

    async def load_by_id(self, config_id: str) -> ModelConfigWithKey | None:
        return None

    async def capture_snapshot_for_runtime(self) -> ModelConfigSnapshot | None:
        raise RuntimeError("key store unavailable")

    async def capture_snapshot_by_id(
        self, config_id: str, expected_version: int
    ) -> ModelConfigSnapshot | None:
        del config_id, expected_version
        raise RuntimeError("key store unavailable")

    def materialise_snapshot(self, snapshot: ModelConfigSnapshot) -> ModelConfigWithKey:
        raise AssertionError("should not be called")


async def test_capture_failure_does_not_fall_back_to_base_provider() -> None:
    """If ``capture_snapshot_for_runtime`` raises, ``for_run`` must raise
    — it must not silently use the base factory's provider.
    """
    registry = ToolRegistry.default()
    resolver = _CaptureFailingResolver()
    repository = InMemoryRunModelBindingRepository()
    base = ModelPlannerFactory(
        provider=_Provider("fallback"),
        context_builder=AgentContextBuilder(
            store=InMemoryAgentStore(),
            registry=registry,
        ),
    )
    factory = RunBoundModelPlannerFactory(
        base_factory=base,
        config_resolver=resolver,
        provider_builder=lambda config: _Provider(config.model_name),
        config_repository=repository,
    )

    with pytest.raises(RuntimeError, match="capture_snapshot_for_runtime"):
        await factory.for_run(run_id="run-1", registry=registry)


def _auth_context():
    from tests.test_policy import population_auth_context

    return population_auth_context()
