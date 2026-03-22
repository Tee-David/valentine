# src/valentine/agents/loop.py
"""
ReAct-style agentic reasoning loop for Valentine agents.

The loop follows: Think -> Act -> Observe -> Think -> ... -> Respond

Instead of one-shot LLM calls, agents can now iteratively:
1. Reason about what to do
2. Execute an action (tool call)
3. Observe the result
4. Decide next step
5. Repeat until done or max steps reached
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from valentine.llm import LLMProvider

logger = logging.getLogger(__name__)

# Safety limits
MAX_LOOP_STEPS = 10
MAX_TOTAL_TIME = 90  # seconds


@dataclass
class Action:
    """A single action the agent wants to take."""

    name: str       # e.g., "shell", "read", "write", "mcp_tool", "search", "respond"
    params: dict    # action-specific parameters


@dataclass
class Observation:
    """The result of executing an action."""

    action_name: str
    success: bool
    output: str
    error: str | None = None


@dataclass
class LoopState:
    """Tracks the full reasoning trajectory."""

    steps: list[dict] = field(default_factory=list)
    actions_taken: list[Action] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    final_response: str = ""
    total_steps: int = 0
    completed: bool = False


# Type alias for action handlers
ActionHandler = Callable[[Action], Awaitable[Observation]]


class AgenticLoop:
    """
    A reusable ReAct-style reasoning loop.

    Any Valentine agent can create an AgenticLoop, register its action
    handlers, and run iterative reasoning over a user message.

    Usage::

        loop = AgenticLoop(llm=self.llm, system_prompt=self.system_prompt)
        loop.register_action("shell", self._handle_shell)
        loop.register_action("read", self._handle_read)
        loop.register_action("write", self._handle_write)

        result = await loop.run(user_message, history=conversation_history)
        # result.final_response is the text to send back to the user
    """

    def __init__(
        self,
        llm: LLMProvider,
        system_prompt: str,
        max_steps: int = MAX_LOOP_STEPS,
        max_time: int = MAX_TOTAL_TIME,
    ):
        self.llm = llm
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.max_time = max_time
        self._handlers: dict[str, ActionHandler] = {}

    def register_action(self, name: str, handler: ActionHandler) -> None:
        """Register an action handler.

        The handler receives an :class:`Action` and must return an
        :class:`Observation`.
        """
        self._handlers[name] = handler

    @property
    def _available_actions(self) -> str:
        """Format available actions for the LLM (always includes 'respond')."""
        return ", ".join(sorted(self._handlers.keys()) | {"respond"})

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_loop_prompt(self) -> str:
        """Build the loop instruction appended to the agent's system prompt."""
        actions_list = "\n".join(
            f'  - "{name}"' for name in sorted(self._handlers.keys())
        )

        return (
            f"\n\n--- AGENTIC MODE ---\n"
            f"You operate in a Think -> Act -> Observe loop. For each step:\n\n"
            f"1. THINK: Briefly reason about what you need to do next.\n"
            f"2. ACT: Output a JSON action to execute.\n"
            f"3. OBSERVE: You'll see the result, then decide your next step.\n\n"
            f"Available actions:\n{actions_list}\n"
            f'  - "respond" (REQUIRED as your final action — this is your '
            f"response to the user)\n\n"
            f"Output format — EXACTLY one JSON object per step:\n"
            f'{{"thought": "brief reasoning", "action": "action_name", '
            f'"params": {{...}}}}\n\n'
            f"When you're ready to respond to the user:\n"
            f'{{"thought": "done reasoning", "action": "respond", '
            f'"params": {{"text": "your response to the user"}}}}\n\n'
            f"RULES:\n"
            f"- Output ONLY valid JSON. No markdown. No extra text.\n"
            f"- One action per step. You'll see the result before choosing "
            f"the next action.\n"
            f"- ALWAYS end with a 'respond' action — never leave the user "
            f"hanging.\n"
            f"- If something fails, try a different approach.\n"
            f"- Maximum {self.max_steps} steps. Be efficient.\n"
            f"- Be warm and conversational in your final response — "
            f"you're Valentine."
        )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(
        self,
        user_message: str,
        history: list[dict[str, Any]] | None = None,
        context: str = "",
    ) -> LoopState:
        """Run the agentic loop until the agent responds or hits limits.

        Args:
            user_message: The user's message.
            history: Prior conversation history ``[{role, content}, ...]``.
            context: Additional context (memory, search results, etc.).

        Returns:
            :class:`LoopState` with the full trajectory and
            ``final_response``.
        """
        state = LoopState()
        start_time = time.monotonic()

        # Build initial messages
        full_system = self.system_prompt + self._build_loop_prompt()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": full_system},
        ]

        # Add conversation history
        if history:
            messages.extend(history)

        # Add user message with optional context
        user_content = user_message
        if context:
            user_content += f"\n\n---\nContext:\n{context}"
        messages.append({"role": "user", "content": user_content})

        for step in range(self.max_steps):
            # Check time limit
            elapsed = time.monotonic() - start_time
            if elapsed > self.max_time:
                logger.warning(
                    "Agentic loop hit time limit (%.0fs)", elapsed,
                )
                if not state.final_response:
                    state.final_response = self._summarize_on_timeout(state)
                state.completed = True
                break

            state.total_steps = step + 1

            # Call LLM
            try:
                kwargs: dict[str, Any] = {}
                if self.llm.provider_name in ("groq", "cerebras"):
                    kwargs["response_format"] = {"type": "json_object"}

                response_text = await self.llm.chat_completion(
                    messages, temperature=0.1, **kwargs,
                )
            except Exception as e:
                logger.error("LLM call failed at step %d: %s", step, e)
                state.final_response = (
                    "Sorry, I hit a snag while thinking. "
                    "Let me try a simpler approach."
                )
                state.completed = True
                break

            # Parse the action
            action_data = self._parse_action(response_text)
            if not action_data:
                # If we can't parse, treat the whole response as the
                # final answer (backwards-compatible with one-shot).
                logger.warning(
                    "Couldn't parse action at step %d, treating as response",
                    step,
                )
                state.final_response = response_text
                state.completed = True
                break

            thought = action_data.get("thought", "")
            action_name = action_data.get("action", "respond")
            params = action_data.get("params", {})

            action = Action(name=action_name, params=params)
            state.actions_taken.append(action)

            # Record step
            state.steps.append({
                "step": step,
                "thought": thought,
                "action": action_name,
                "params": params,
            })

            # Add assistant's action to messages
            messages.append({"role": "assistant", "content": response_text})

            logger.info(
                "Loop step %d: thought='%s' action=%s",
                step,
                thought[:80],
                action_name,
            )

            # Handle "respond" action — we're done
            if action_name == "respond":
                state.final_response = params.get("text", "")
                state.completed = True
                break

            # Execute the action
            observation = await self._execute_action(action)
            state.observations.append(observation)

            # Feed observation back to LLM
            obs_text = self._format_observation(observation)
            messages.append({"role": "user", "content": obs_text})

        # If we exhausted steps without responding
        if not state.completed:
            logger.warning(
                "Agentic loop exhausted %d steps without responding",
                self.max_steps,
            )
            state.final_response = self._summarize_on_timeout(state)
            state.completed = True

        return state

    # ------------------------------------------------------------------
    # Action execution
    # ------------------------------------------------------------------

    async def _execute_action(self, action: Action) -> Observation:
        """Look up the handler for *action* and execute it safely."""
        handler = self._handlers.get(action.name)
        if not handler:
            return Observation(
                action_name=action.name,
                success=False,
                output="",
                error=(
                    f"Unknown action: '{action.name}'. "
                    f"Available: {self._available_actions}"
                ),
            )
        try:
            return await handler(action)
        except Exception as e:
            logger.error("Action handler '%s' failed: %s", action.name, e)
            return Observation(
                action_name=action.name,
                success=False,
                output="",
                error=str(e),
            )

    # ------------------------------------------------------------------
    # JSON parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_action(text: str) -> dict[str, Any] | None:
        """Parse LLM output as a JSON action object.

        Handles markdown code fences and extracts JSON from mixed text
        when possible.
        """
        clean = text.strip()

        # Strip markdown code blocks if present
        if clean.startswith("```"):
            # Remove opening fence (with optional language tag)
            clean = clean.split("\n", 1)[-1] if "\n" in clean else clean[3:]
            if clean.endswith("```"):
                clean = clean[:-3]
            clean = clean.strip()

        try:
            data = json.loads(clean)
            if isinstance(data, dict):
                return data
            return None
        except json.JSONDecodeError:
            pass

        # Try to extract the outermost JSON object from mixed text
        match = re.search(
            r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}",
            clean,
        )
        if match:
            try:
                data = json.loads(match.group())
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass

        return None

    # ------------------------------------------------------------------
    # Observation formatting
    # ------------------------------------------------------------------

    @staticmethod
    def _format_observation(obs: Observation) -> str:
        """Format an observation as text for the LLM to read."""
        if obs.success:
            result = (
                obs.output
                if obs.output
                else "[Action completed successfully with no output]"
            )
            return (
                f"[OBSERVATION] Action '{obs.action_name}' succeeded:\n"
                f"{result}"
            )

        parts = [f"[OBSERVATION] Action '{obs.action_name}' FAILED:"]
        if obs.error:
            parts.append(f"Error: {obs.error}")
        if obs.output:
            parts.append(f"Output: {obs.output}")
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Timeout / step-limit fallback
    # ------------------------------------------------------------------

    @staticmethod
    def _summarize_on_timeout(state: LoopState) -> str:
        """Generate a response when the loop times out or exhausts steps."""
        if state.observations:
            last_obs = state.observations[-1]
            if last_obs.success and last_obs.output:
                return (
                    "I ran into my step limit, but here's what I found:\n\n"
                    + last_obs.output[:3000]
                )
        return (
            "I tried to work through this but hit my reasoning limit. "
            "Could you break this down into smaller steps?"
        )
