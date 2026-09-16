"""Section 7 of the standard: ONE uniform entry point for every agent.

The standard fixes the *conceptual* signature ``run(agent_input, state)`` and
leaves the concrete sync/async form to the orchestrator.  This repository locks
it as::

    async def run(self, agent_input: AgentInput, state=None) -> AgentOutput

``state`` is optional, so both supported call styles are the same call:

  * ``await agent.run(agent_input)``         # state travels inside the input
  * ``await agent.run(agent_input, state)``  # shared state passed explicitly

The injected state is validated through ``SharedUserState`` and must belong to
``agent_input.user_id``, so an agent can never be handed two parallel copies of
the shared state (section 1: "one shared user state").  A ``dict`` is only
accepted as a transport format -- it is validated, never used raw.

What this base class deliberately does NOT do: it touches no orchestrator, no
UI, no filesystem, and performs no risk reasoning.  Its only extra job is to
*check* the result against sections 4-6, so a non-conformant output cannot
leave an agent silently.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from typing import ClassVar, Optional, Tuple, Union

from reprojourney.schemas.agent_contract import (
    STANDARD_VERSION,
    AgentContractError,
    assert_output_contract,
    requested_by_problem,
)
from reprojourney.schemas.base_schemas import AgentInput, AgentOutput, SharedUserState

StateLike = Union[SharedUserState, dict, None]


class BaseAgent(ABC):
    """Standard-conformant agent: one entry point, one shared state, one output."""

    #: ``AgentOutput.agent`` (section 4) -- the name other agents see.
    agent_name: ClassVar[str] = ""
    #: The task this agent answers (section 3 ``task``).
    task: ClassVar[str] = ""
    #: Section 4: only an agent that owns a *risk* decision may report a tier.
    #: An agent without that duty must answer ``risk_level="UNKNOWN"``.
    risk_duty: ClassVar[bool] = False

    # ------------------------------------------------------------------ entry point
    @abstractmethod
    async def run(self, agent_input: AgentInput, state: StateLike = None) -> AgentOutput:
        """Standard entry point (section 7). Subclasses do the actual work."""

    def prepare_input(self, agent_input: AgentInput, state: StateLike = None) -> AgentInput:
        """Normalise both supported call styles into a single validated input.

        * ``state is None``  -> the input is used as-is (``agent_input.state``);
        * ``state`` is given -> it is validated as the shared state and injected,
          replacing nothing else in the input (section 3: no scattered params).
        """

        if not isinstance(agent_input, AgentInput):
            raise AgentContractError("第一个参数必须是 AgentInput（标准 §3）")
        problem = requested_by_problem(agent_input.requested_by)
        if problem:
            raise AgentContractError(problem)
        if state is None:
            return agent_input
        resolved = state if isinstance(state, SharedUserState) else SharedUserState.model_validate(state)
        if resolved.user_id != agent_input.user_id:
            raise AgentContractError(
                f"注入的 state 属于 {resolved.user_id!r}，与 agent_input.user_id={agent_input.user_id!r} 不一致"
            )
        return agent_input.model_copy(update={"state": resolved})

    def enforce_output(self, output: AgentOutput, request_id: Optional[str] = None) -> AgentOutput:
        """Fail loudly when an output breaks sections 4-6 of the standard."""

        if output.agent != self.agent_name:
            raise AgentContractError(
                f"{self.agent_name} 的 output.agent={output.agent!r} 与声明的 agent_name 不一致"
            )
        return assert_output_contract(output, risk_duty=self.risk_duty, request_id=request_id)

    # ------------------------------------------------------------------ introspection
    @classmethod
    def contract(cls) -> dict:
        """Machine-readable summary of this agent's standard obligations."""

        return {
            "agent": cls.agent_name,
            "task": cls.task,
            "risk_duty": cls.risk_duty,
            "entry_point": "run(agent_input, state=None)",
            "standard_version": STANDARD_VERSION,
        }


def parameter_names(agent_cls: type) -> Tuple[str, ...]:
    """Parameter names of ``agent_cls.run`` (used by the tests and the docs)."""

    parameters = inspect.signature(agent_cls.run).parameters
    return tuple(name for name in parameters if name != "self")


__all__ = ["BaseAgent", "StateLike", "parameter_names"]
