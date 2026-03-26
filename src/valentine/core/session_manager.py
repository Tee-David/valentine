# src/valentine/core/session_manager.py
"""
Session Manager — ChatGPT/Claude-style conversation & session management.

Implements a 4-layer memory architecture inspired by ChatGPT's internal design:

Layer 1: Current Session Messages (last 5 messages, full text)
Layer 2: Session Summary (auto-summarized recent conversation)
Layer 3: Long-term User Facts (extracted preferences, stored in Mem0/Qdrant)
Layer 4: Ephemeral Session Metadata (current project, active tools, etc.)

Key features:
- Multiple concurrent sessions per user (like ChatGPT's conversation threads)
- Session switching without losing context
- Automatic conversation summarization when context gets long
- Project-scoped sessions (like Claude's Projects feature)
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)


@dataclass
class Session:
    """Represents a single conversation session (like a ChatGPT thread)."""
    session_id: str
    chat_id: str
    user_id: str
    title: str = "New Conversation"
    project_path: Optional[str] = None  # Optional project binding
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    
    # Layer 1: Current messages (full text, last N messages)
    messages: List[Dict] = field(default_factory=list)
    
    # Layer 2: Compressed summary of older messages
    summary: str = ""
    
    # Layer 4: Ephemeral metadata
    metadata: Dict = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class SessionManager:
    """
    Manages conversation sessions with tiered memory compression.
    
    Uses Redis for persistence, enabling:
    - Session listing and switching from Workbench UI
    - Automatic context compression for long conversations
    - Project-scoped conversations
    """
    
    # Configuration
    MAX_RECENT_MESSAGES = 10     # Layer 1: full messages kept
    SUMMARY_TRIGGER = 15        # Summarize when history exceeds this
    MAX_SESSIONS_PER_USER = 50  # Prevent unbounded growth
    
    REDIS_PREFIX = "valentine:sessions"
    
    def __init__(self, redis_client=None, llm=None):
        self._redis = redis_client
        self._llm = llm  # For auto-summarization
        self._local_cache: Dict[str, Session] = {}  # In-memory fallback
    
    async def get_or_create_session(self, chat_id: str, user_id: str) -> Session:
        """Get the active session for a chat, or create a new one."""
        session = await self._get_active_session(chat_id)
        if session:
            return session
        
        # Create new session
        session_id = f"{chat_id}:{int(time.time())}"
        session = Session(
            session_id=session_id,
            chat_id=chat_id,
            user_id=user_id,
        )
        await self._save_session(session)
        return session
    
    async def add_message(self, chat_id: str, role: str, content: str, 
                          user_id: str = "", metadata: dict = None):
        """
        Add a message to the active session's history.
        Auto-summarizes when the history gets too long.
        """
        session = await self.get_or_create_session(chat_id, user_id)
        
        msg = {
            "role": role,
            "content": content,
            "timestamp": time.time(),
        }
        if metadata:
            msg["metadata"] = metadata
        
        session.messages.append(msg)
        session.last_active = time.time()
        
        # Auto-generate title from first user message
        if session.title == "New Conversation" and role == "user" and content:
            session.title = content[:60] + ("..." if len(content) > 60 else "")
        
        # Trigger compression if history is getting long
        if len(session.messages) > self.SUMMARY_TRIGGER:
            await self._compress_history(session)
        
        await self._save_session(session)
    
    async def get_context_for_llm(self, chat_id: str) -> List[Dict]:
        """
        Build the optimized context window for an LLM call.
        
        Returns messages in the format:
        [
            {"role": "system", "content": "<session summary>"},  # Layer 2
            {"role": "user", "content": "..."},                  # Layer 1
            {"role": "assistant", "content": "..."},             # Layer 1
            ...
        ]
        """
        session = await self._get_active_session(chat_id)
        if not session:
            return []
        
        context = []
        
        # Layer 2: Inject compressed summary as system context
        if session.summary:
            context.append({
                "role": "system",
                "content": f"[Previous conversation summary]\n{session.summary}"
            })
        
        # Layer 1: Recent messages (full text)
        recent = session.messages[-self.MAX_RECENT_MESSAGES:]
        for msg in recent:
            context.append({
                "role": msg["role"],
                "content": msg["content"],
            })
        
        return context
    
    async def list_sessions(self, chat_id: str = None, user_id: str = None) -> List[Session]:
        """List all sessions for a chat or user (for Workbench UI)."""
        sessions = []
        
        if self._redis:
            pattern = f"{self.REDIS_PREFIX}:*"
            async for key in self._redis.scan_iter(pattern):
                data = await self._redis.get(key)
                if data:
                    session = Session.from_dict(json.loads(data))
                    if chat_id and session.chat_id != chat_id:
                        continue
                    if user_id and session.user_id != user_id:
                        continue
                    sessions.append(session)
        else:
            sessions = list(self._local_cache.values())
            if chat_id:
                sessions = [s for s in sessions if s.chat_id == chat_id]
            if user_id:
                sessions = [s for s in sessions if s.user_id == user_id]
        
        # Sort by last active (most recent first)
        sessions.sort(key=lambda s: s.last_active, reverse=True)
        return sessions
    
    async def switch_session(self, chat_id: str, session_id: str) -> Optional[Session]:
        """Switch the active session for a chat."""
        session = await self._load_session(session_id)
        if session and session.chat_id == chat_id:
            # Mark as active by updating last_active
            session.last_active = time.time()
            await self._save_session(session)
            
            # Store active session pointer
            if self._redis:
                await self._redis.set(
                    f"{self.REDIS_PREFIX}:active:{chat_id}",
                    session_id,
                    ex=86400 * 7,  # 7 day expiry
                )
            return session
        return None
    
    async def new_session(self, chat_id: str, user_id: str, 
                          title: str = "New Conversation",
                          project_path: str = None) -> Session:
        """Create a new session (like clicking 'New Chat' in ChatGPT)."""
        session_id = f"{chat_id}:{int(time.time())}"
        session = Session(
            session_id=session_id,
            chat_id=chat_id,
            user_id=user_id,
            title=title,
            project_path=project_path,
        )
        await self._save_session(session)
        
        # Make it the active session
        if self._redis:
            await self._redis.set(
                f"{self.REDIS_PREFIX}:active:{chat_id}",
                session_id,
                ex=86400 * 7,
            )
        
        return session
    
    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    
    async def _get_active_session(self, chat_id: str) -> Optional[Session]:
        """Get the currently active session for a chat."""
        if self._redis:
            active_id = await self._redis.get(f"{self.REDIS_PREFIX}:active:{chat_id}")
            if active_id:
                session_id = active_id.decode("utf-8") if isinstance(active_id, bytes) else active_id
                return await self._load_session(session_id)
        
        # Fallback: find most recent session for this chat
        sessions = await self.list_sessions(chat_id=chat_id)
        return sessions[0] if sessions else None
    
    async def _load_session(self, session_id: str) -> Optional[Session]:
        """Load a session from Redis or local cache."""
        if self._redis:
            data = await self._redis.get(f"{self.REDIS_PREFIX}:{session_id}")
            if data:
                raw = data.decode("utf-8") if isinstance(data, bytes) else data
                return Session.from_dict(json.loads(raw))
        return self._local_cache.get(session_id)
    
    async def _save_session(self, session: Session):
        """Persist a session to Redis or local cache."""
        if self._redis:
            await self._redis.set(
                f"{self.REDIS_PREFIX}:{session.session_id}",
                json.dumps(session.to_dict()),
                ex=86400 * 30,  # 30 day expiry
            )
            # Update active pointer
            await self._redis.set(
                f"{self.REDIS_PREFIX}:active:{session.chat_id}",
                session.session_id,
                ex=86400 * 7,
            )
        else:
            self._local_cache[session.session_id] = session
    
    async def _compress_history(self, session: Session):
        """
        Compress older messages into a summary (Layer 2).
        Keeps the most recent MAX_RECENT_MESSAGES intact.
        """
        if len(session.messages) <= self.MAX_RECENT_MESSAGES:
            return
        
        # Split: old messages to summarize, recent to keep
        old_messages = session.messages[:-self.MAX_RECENT_MESSAGES]
        recent_messages = session.messages[-self.MAX_RECENT_MESSAGES:]
        
        # Build text to summarize
        old_text = "\n".join(
            f"{m['role']}: {m['content'][:200]}" for m in old_messages
        )
        
        # Use LLM to create summary if available
        if self._llm:
            try:
                summary_prompt = [
                    {"role": "system", "content": (
                        "Summarize this conversation segment in 2-3 concise sentences. "
                        "Focus on: key topics discussed, decisions made, user preferences revealed, "
                        "and any action items. Be factual and dense."
                    )},
                    {"role": "user", "content": old_text[:3000]},
                ]
                new_summary = await self._llm.chat_completion(
                    summary_prompt, temperature=0.1, max_tokens=200,
                )
                # Append to existing summary
                if session.summary:
                    session.summary = f"{session.summary}\n{new_summary}"
                else:
                    session.summary = new_summary
                
                # Trim summary if it's getting too long
                if len(session.summary) > 2000:
                    session.summary = session.summary[-2000:]
                
            except Exception as e:
                logger.warning(f"Auto-summarization failed: {e}")
                # Fallback: simple truncation summary
                session.summary += f"\n[{len(old_messages)} older messages truncated]"
        else:
            session.summary += f"\n[{len(old_messages)} older messages truncated]"
        
        # Keep only recent messages
        session.messages = recent_messages
        logger.info(
            f"Compressed session {session.session_id}: "
            f"{len(old_messages)} messages → summary, {len(recent_messages)} kept"
        )
