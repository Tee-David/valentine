# src/valentine/orchestrator/agentscope_bridge.py
"""
AgentScope Bridge — Wraps Valentine's existing agents into AgentScope's
ReActAgent framework for enhanced reasoning, tool dispatch, and memory.

AgentScope AUGMENTS ZeroClaw (does NOT replace it):
- ZeroClaw remains the router (regex + LLM classification)
- AgentScope provides ReAct reasoning loops for individual agents
- MsgHub enables multi-agent collaboration when tasks need it
- Memory compression replaces fixed-window history

Architecture:
  Telegram → ZeroClaw (router) → AgentScope Bridge → ReActAgent(CodeSmith/Oracle/etc)
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy imports — AgentScope may not be installed
_agentscope_available = False
try:
    import agentscope
    from agentscope.agents import ReActAgent, DialogAgent
    from agentscope.message import Msg
    from agentscope.msghub import msghub
    from agentscope.memory import TemporaryMemory
    _agentscope_available = True
    logger.info("AgentScope bridge loaded successfully")
except ImportError:
    logger.info("AgentScope not installed — bridge disabled, using native agents")


def is_available() -> bool:
    """Check if AgentScope is installed and available."""
    return _agentscope_available


class ValentineReActWrapper:
    """
    Wraps a Valentine BaseAgent as an AgentScope ReActAgent.
    
    This gives each agent:
    - Chain-of-thought reasoning (ReAct: Thought → Action → Observation loops)
    - Dynamic tool dispatch via AgentScope Toolkit
    - Compressed memory (auto-summarizes old conversation turns)
    - Multi-agent collaboration via MsgHub
    """
    
    def __init__(self, valentine_agent, tools: Optional[list] = None):
        if not _agentscope_available:
            raise RuntimeError("AgentScope is not installed")
        
        self.native_agent = valentine_agent
        self.name = valentine_agent.name.value if hasattr(valentine_agent.name, 'value') else str(valentine_agent.name)
        
        # Build AgentScope service toolkit from Valentine's action handlers
        service_toolkit = None
        if tools:
            from agentscope.service import ServiceToolkit
            service_toolkit = ServiceToolkit()
            for tool_fn in tools:
                service_toolkit.add(tool_fn)
        
        # Create the ReActAgent with the native agent's system prompt
        self.react_agent = ReActAgent(
            name=self.name,
            model_config_name="valentine_llm",
            sys_prompt=valentine_agent.system_prompt,
            service_toolkit=service_toolkit,
            max_iters=5,  # Max reasoning iterations
            verbose=True,
        )
    
    async def process_with_react(self, user_message: str, history: list = None) -> str:
        """
        Process a message through AgentScope's ReAct loop instead of
        the native JSON-action parser.
        
        Returns the agent's final response text.
        """
        msg = Msg(name="user", content=user_message, role="user")
        
        # Inject history into memory if available
        if history:
            for h in history[-10:]:  # Last 10 messages
                role = h.get("role", "user")
                self.react_agent.memory.add(
                    Msg(name=role, content=h.get("content", ""), role=role)
                )
        
        # Run ReAct loop
        response = self.react_agent(msg)
        return response.content if hasattr(response, 'content') else str(response)


class AgentScopeBridge:
    """
    Central bridge that manages AgentScope initialization and provides
    enhanced processing for Valentine agents.
    
    Usage:
        bridge = AgentScopeBridge()
        bridge.initialize(model_config)
        result = await bridge.process("codesmith", user_msg, history)
    """
    
    def __init__(self):
        self._initialized = False
        self._wrapped_agents = {}
    
    def initialize(self, llm_config: dict = None):
        """Initialize AgentScope with Valentine's LLM configuration."""
        if not _agentscope_available:
            logger.info("AgentScope not available, bridge stays disabled")
            return False
        
        try:
            # Configure AgentScope to use Valentine's existing LLM providers
            config = llm_config or {
                "model_type": "openai_chat",
                "config_name": "valentine_llm",
                "model_name": os.getenv("GROQ_DEFAULT_MODEL", "llama-3.3-70b-versatile"),
                "api_key": os.getenv("GROQ_API_KEY", ""),
                "base_url": os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
            }
            
            agentscope.init(
                model_configs=[config],
                project="valentine",
                name="v3",
            )
            self._initialized = True
            logger.info("AgentScope bridge initialized")
            return True
        except Exception as e:
            logger.error(f"AgentScope initialization failed: {e}")
            return False
    
    def wrap_agent(self, valentine_agent, tools: list = None) -> Optional[ValentineReActWrapper]:
        """Wrap a Valentine agent with AgentScope ReAct capabilities."""
        if not self._initialized:
            return None
        
        agent_name = valentine_agent.name.value if hasattr(valentine_agent.name, 'value') else str(valentine_agent.name)
        
        if agent_name not in self._wrapped_agents:
            try:
                wrapper = ValentineReActWrapper(valentine_agent, tools)
                self._wrapped_agents[agent_name] = wrapper
                logger.info(f"Wrapped {agent_name} with AgentScope ReAct")
            except Exception as e:
                logger.warning(f"Failed to wrap {agent_name}: {e}")
                return None
        
        return self._wrapped_agents.get(agent_name)
    
    async def process(self, agent_name: str, message: str, history: list = None) -> Optional[str]:
        """
        Process a message through the AgentScope-wrapped agent.
        Returns None if AgentScope is unavailable (caller should fall back to native).
        """
        wrapper = self._wrapped_agents.get(agent_name)
        if not wrapper:
            return None
        
        try:
            return await wrapper.process_with_react(message, history)
        except Exception as e:
            logger.error(f"AgentScope processing failed for {agent_name}: {e}")
            return None  # Caller falls back to native processing
    
    @property
    def is_active(self) -> bool:
        return self._initialized and _agentscope_available


# Singleton instance
bridge = AgentScopeBridge()
