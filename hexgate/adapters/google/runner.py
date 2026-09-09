"""Google ADK ``Runner`` wrapper: opens a :class:`HexgateContext` scope around each
``Runner.run*`` call so the wrapped tools' enforcers can resolve the
active role. Langfuse propagation mirrors HexgateContext identity into spans.
"""

import asyncio
from collections import OrderedDict
from collections.abc import Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, AsyncGenerator, Generator

import nest_asyncio
from google.adk.agents import BaseAgent
from google.adk.apps import App
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService
from google.genai import types
from langfuse import get_client, propagate_attributes
from openinference.instrumentation.google_adk import GoogleADKInstrumentor

from hexgate.adapters._common import langfuse_propagate_kwargs
from hexgate.adapters.google.usage import HexgateUsagePlugin
from hexgate.adapters.google.wrapper import wrap_google_agent
from hexgate.approvals import ApprovalHandler
from hexgate.cloud.client import HexgateClient, HexgateConfig
from hexgate.config.env import resolve_api_key
from hexgate.runtime import HexgateContext, run_scope
from hexgate.security.agent_gate import (
    HandoffDepthExceededError,
    ReachNotAllowedError,
    resolve_agent_gate,
    resolve_reach_gate,
)
from hexgate.security.bans import resolve_ban_gate
from hexgate.security.naming import canonical_agent_name, canonical_name

if TYPE_CHECKING:
    from hexgate.guards.types import Guard, GuardObserver

# ADK's PluginManager re-raises every plugin exception as a fresh RuntimeError
# (``Error in plugin '<name>' during '<cb>' callback: ...``), keeping the original
# only as ``__cause__``. That would erase our typed seam errors and their
# ``.decision`` / ``.depth`` payload (and leak the internal plugin name), so the
# runner unwraps them back at the ADK boundary. ``except RuntimeError`` still
# catches either form — all three seam errors subclass it by design.
_TYPED_SEAM_ERRORS = (ReachNotAllowedError, HandoffDepthExceededError)


def _unwrap_plugin_error(exc: RuntimeError) -> BaseException:
    """Recover a typed seam error ADK wrapped into a bare ``RuntimeError``; return
    ``exc`` unchanged when its cause is not one of ours."""
    cause = exc.__cause__
    return cause if isinstance(cause, _TYPED_SEAM_ERRORS) else exc


class _HexgateReachPlugin(BasePlugin):
    """Enforce *handoff* reach and the handoff-depth cap at the ADK transfer seam.

    ADK expresses a handoff as the ``transfer_to_agent(agent_name=...)`` tool call;
    ``before_tool_callback`` fires before it runs, so deciding ``agent.handoff:<target>``
    here and raising :class:`ReachNotAllowedError` on a deny stops the transfer
    before it happens. Reach is governed by the source agent's policy; only the
    governed root's transfers are gated (a transfer from an un-governed sub-agent is
    left alone, matching the OpenAI adapter). Agent-as-tool reach is *not* handled
    here — an :class:`AgentTool` is an ordinary tool on ``agent.tools``, so
    :func:`~hexgate.adapters.google.tools.wrap_tool` gates it under its reach key,
    the same substitution the OpenAI adapter uses (gating it here as well would
    decide one delegation twice and emit two audit events).
    """

    # Cap on tracked invocations. after_run_callback clears an invocation's depth
    # on a normal run, but ADK skips it when the agent loop raises or the caller
    # stops iterating early, so a shared, long-lived plugin would otherwise leak one
    # entry per abnormally-terminated run. Bounding the map turns that unbounded
    # leak into a fixed ceiling: the least-recently-transferred entry is evicted
    # once this many distinct invocations are in flight (far above real concurrency).
    _MAX_TRACKED_INVOCATIONS = 4096

    def __init__(self, runner: "HexgateRunner") -> None:
        super().__init__(name="hexgate_reach")
        self._runner = runner
        # Handoff depth per invocation (this plugin is shared across runs, unlike
        # the OpenAI per-run hook). Cleared in after_run_callback; bounded (see the
        # class constant) so a skipped cleanup on an aborted run can't leak forever.
        self._depth: OrderedDict[str, int] = OrderedDict()

    def _bump_depth(self, invocation_id: str) -> int:
        """Increment and return this invocation's handoff depth, keeping the most
        recently active invocations and evicting the stalest past the cap."""
        depth = self._depth.get(invocation_id, 0) + 1
        self._depth[invocation_id] = depth
        self._depth.move_to_end(invocation_id)
        while len(self._depth) > self._MAX_TRACKED_INVOCATIONS:
            self._depth.popitem(last=False)  # evict least-recently-transferred
        return depth

    async def before_tool_callback(self, *, tool, tool_args, tool_context) -> None:
        # A raise here aborts the run, so after_run_callback never fires; drop this
        # invocation's depth entry on the way out to keep the shared map from
        # leaking one entry per aborted (over-depth or reach-denied) run.
        try:
            return await self._gate_tool(tool, tool_args, tool_context)
        except Exception:
            self._depth.pop(tool_context.invocation_id, None)
            raise

    async def _gate_tool(self, tool, tool_args, tool_context) -> None:
        # Handoff reach + the depth cap only. Agent-as-tool reach is gated in
        # wrap_tool (an AgentTool is a real tool on agent.tools) under the same
        # reach-key substitution the OpenAI adapter uses, so gating it here too
        # would double-decide it and emit two audit events for one delegation.
        if tool.name != "transfer_to_agent":
            return None  # ordinary tool / AgentTool — gated by the wrapped enforcer
        # Depth cap first, as a runaway guard independent of reach policy: a
        # transfer moves control forward, so the count of transfers in one
        # invocation is the chain depth. Counts every transfer, governed or not.
        cap = self._runner._max_handoff_depth
        if cap is not None:
            depth = self._bump_depth(tool_context.invocation_id)
            if depth > cap:
                raise HandoffDepthExceededError(depth, cap)
        if canonical_name(tool_context.agent_name) != self._runner._agent_name:
            return None  # source is not the governed root; reach from it isn't gated
        target = tool_args.get("agent_name")
        if not target:
            return None
        if not self._runner._binding.enforcer.policy.declares_reach():
            return None  # no 'agents' block — skip building a gate on the hot seam
        gate = resolve_reach_gate(
            self._runner._binding.enforcer,
            approval_handler=self._runner._approval_handler,
        )
        await gate.check_reach_async(canonical_name(target), via="handoff")
        return None

    async def after_run_callback(self, *, invocation_context) -> None:
        # Drop this invocation's depth counter so the map does not grow unbounded.
        self._depth.pop(invocation_context.invocation_id, None)
        return None


class HexgateRunner:
    """Runner for Google ADK agents with Hexgate tool policy and observability."""

    def __init__(
        self,
        *,
        agent: BaseAgent,
        app_name: str,
        session_service: BaseSessionService,
        api_key: str | None = None,
        approval_handler: ApprovalHandler | None = None,
        guards: "Sequence[Guard] | None" = None,
        guard_observer: "GuardObserver | None" = None,
        max_handoff_depth: int | None = None,
        **runner_kwargs: Any,
    ):
        # ``guards`` matches the OpenAI runner's constructor. ADK's ``run`` has
        # no ``hooks=`` to collide with (it takes ``**runner_kwargs``), so the
        # name is for cross-runner symmetry, not disambiguation.
        self.api_key = resolve_api_key(api_key)
        if self.api_key is None:
            raise ValueError(
                "HEXGATE_API_KEY is not set. Pass api_key= explicitly or set the HEXGATE_API_KEY environment variable."
            )
        # Policy resolves at construction (the loud-failure point); the
        # Runner is built once — refresh swaps the enforcer's policy
        # without touching it. One client is shared with the ban resolver.
        self._approval_handler = approval_handler
        # Handoff-chain depth cap (None = no cap); enforced per invocation by the
        # reach plugin.
        self._max_handoff_depth = max_handoff_depth
        client = HexgateClient(HexgateConfig.from_env(api_key=self.api_key))
        self._wrapped_agent, self._binding = wrap_google_agent(
            agent,
            api_key=self.api_key,
            approval_handler=approval_handler,
            client=client,
            guards=guards,
            guard_observer=guard_observer,
        )
        plugins = list(runner_kwargs.pop("plugins", None) or [])
        plugins.append(HexgateUsagePlugin(api_key=self.api_key))
        # Reach + depth enforcement at the ADK transfer seam. Inserted *first*:
        # ADK's PluginManager runs before_tool_callback in registration order and
        # early-exits on the first plugin that returns non-None, so a caller plugin
        # that overrides a tool result (replay, environment simulation, a cache)
        # would otherwise short-circuit this gate — a silent fail-open. Ours only
        # ever returns None or raises, so it can never short-circuit anything itself.
        plugins.insert(0, _HexgateReachPlugin(self))
        app = App(name=app_name, root_agent=self._wrapped_agent, plugins=plugins)
        self._runner = Runner(
            app=app,
            session_service=session_service,
            **runner_kwargs,
        )
        self._agent_name = canonical_agent_name(agent)
        self._ban_gate = resolve_ban_gate(
            self._agent_name, api_key=self.api_key, client=client
        )

    async def _check_admission_async(self) -> None:
        """Refuse a caller not admitted by the root agent's policy before the run
        drives. Must run inside the active HexgateContext scope. No-op when the
        policy declares no admission."""
        gate = resolve_agent_gate(
            self._binding.enforcer, approval_handler=self._approval_handler
        )
        await gate.check_admission_async()

    def _check_admission_sync(self) -> None:
        """Sync mirror of :meth:`_check_admission_async`."""
        gate = resolve_agent_gate(
            self._binding.enforcer, approval_handler=self._approval_handler
        )
        gate.check_admission()

    def _setup_observability(self):
        """Install Langfuse + GoogleADKInstrumentor (idempotent)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running loop: safe to patch (and only useful for sync entry points).
            # Patching a live loop breaks asyncio.current_task() on Python 3.12+.
            nest_asyncio.apply()
        get_client()
        GoogleADKInstrumentor().instrument()

    @contextmanager
    def _propagate(self, context: HexgateContext):
        """Propagate HexgateContext identity into Langfuse spans for the block."""
        with propagate_attributes(
            **langfuse_propagate_kwargs(
                context, f"google.runner.run.{self._agent_name}"
            )
        ):
            yield

    def run(
        self,
        *,
        new_message: types.Content,
        hexgate_context: HexgateContext,
        **kwargs: Any,
    ) -> Generator[Any, None, None]:
        """Run the Google ADK agent synchronously, yielding events.

        ADK's ``Runner.run`` drives the agent loop in a worker thread whose
        context cannot see our :class:`HexgateContext` scope, so the tools' enforcers
        lose the active role. We drive ``run_async`` inline on a per-call loop
        instead, keeping execution in this scoped thread.
        """
        self._setup_observability()
        self._binding.refresh()  # per-run policy pull; 304 when unchanged
        if self._ban_gate is not None:
            self._ban_gate.check(hexgate_context)
        with (
            hexgate_context.sync_scope(),
            run_scope(self._agent_name),
            self._propagate(hexgate_context),
        ):
            self._check_admission_sync()  # in-scope: reads the caller's role
            agen = self._runner.run_async(
                user_id=hexgate_context.user_id,
                session_id=hexgate_context.session_id,
                new_message=new_message,
                **kwargs,
            )
            loop = asyncio.new_event_loop()
            try:
                while True:
                    try:
                        event = loop.run_until_complete(agen.__anext__())
                    except StopAsyncIteration:
                        break
                    except RuntimeError as exc:
                        # Recover a typed seam error ADK wrapped (see module top).
                        unwrapped = _unwrap_plugin_error(exc)
                        if unwrapped is exc:
                            raise
                        raise unwrapped from exc
                    yield event
            finally:
                loop.run_until_complete(agen.aclose())
                loop.close()

    async def run_async(
        self,
        *,
        new_message: types.Content | None = None,
        hexgate_context: HexgateContext,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[Any, None]:
        """Run the Google ADK agent asynchronously, yielding events.

        ``session_id`` overrides the ADK session used for the run; it defaults to
        ``hexgate_context.session_id``. Pass it explicitly to decouple the ADK
        conversation store from the caller-facing session id that lands in audit
        (``hexgate serve`` does this so audit stays correlatable while ADK memory
        is managed separately).
        """
        self._setup_observability()
        await self._binding.refresh_async()  # per-run policy pull; 304 when unchanged
        if self._ban_gate is not None:
            await self._ban_gate.check_async(hexgate_context)
        adk_session_id = (
            session_id if session_id is not None else hexgate_context.session_id
        )
        async with hexgate_context:
            await self._check_admission_async()  # in-scope: reads the caller's role
            with run_scope(self._agent_name), self._propagate(hexgate_context):
                try:
                    async for event in self._runner.run_async(
                        user_id=hexgate_context.user_id,
                        session_id=adk_session_id,
                        new_message=new_message,
                        **kwargs,
                    ):
                        yield event
                except RuntimeError as exc:
                    # Recover a typed seam error ADK wrapped (see module top).
                    unwrapped = _unwrap_plugin_error(exc)
                    if unwrapped is exc:
                        raise
                    raise unwrapped from exc
