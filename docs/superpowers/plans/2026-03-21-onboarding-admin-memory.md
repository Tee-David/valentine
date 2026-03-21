# Onboarding, Admin Controls & Persistent Memory — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add whitelist-based multi-user access control, interactive onboarding with inline buttons, admin management commands, and continuous memory learning to Valentine's Telegram bot.

**Architecture:** SQLite stores user records and access control (structured, queryable). Redis stores onboarding session state and conversation history (ephemeral, fast). mem0+Qdrant stores semantic memory facts (vector search for context injection). Capabilities flow from SQLite → TelegramAdapter → IncomingMessage → ZeroClaw for enforcement. Memory extraction fires async from BaseAgent → Cortex after every response.

**Tech Stack:** SQLite3 (stdlib), python-telegram-bot (existing), Redis (existing), mem0 (existing), Groq Whisper API (for onboarding voice transcription)

**Spec:** `docs/superpowers/specs/2026-03-21-onboarding-admin-memory-design.md`

---

## File Structure

| File | Responsibility |
|------|---------------|
| `src/valentine/db.py` | **NEW** — UserDB class: SQLite user CRUD, capabilities, audit log |
| `src/valentine/nexus/telegram.py` | **MODIFY** — Access control gate, commands, callbacks, onboarding state machine |
| `src/valentine/nexus/onboarding.py` | **NEW** — Onboarding step definitions and state machine logic (keeps telegram.py focused) |
| `src/valentine/models.py` | **MODIFY** — Add `user_capabilities` field to IncomingMessage |
| `src/valentine/config.py` | **MODIFY** — Add `admin_telegram_id`, `db_path` |
| `src/valentine/main.py` | **MODIFY** — Create UserDB in bot process, pass to adapter |
| `src/valentine/orchestrator/zeroclaw.py` | **MODIFY** — Capability enforcement, mem0 context fetch |
| `src/valentine/agents/base.py` | **MODIFY** — Fire Cortex extraction after response |
| `src/valentine/agents/cortex.py` | **MODIFY** — Handle extract_memory tasks, onboarding fact storage |
| `src/valentine/agents/oracle.py` | **MODIFY** — Handle capability_blocked intent |
| `tests/test_db.py` | **NEW** — UserDB unit tests |
| `tests/test_onboarding.py` | **NEW** — Onboarding state machine tests |

---

### Task 1: Config & Model Changes

**Files:**
- Modify: `src/valentine/config.py`
- Modify: `src/valentine/models.py`

- [ ] **Step 1: Add config fields**

```python
# In src/valentine/config.py, add to Settings class:
    admin_telegram_id: str = Field(default="")
    db_path: str = Field(default="/opt/valentine/data/valentine.db")
```

- [ ] **Step 2: Add user_capabilities to IncomingMessage**

In `src/valentine/models.py`, add field to `IncomingMessage`:

```python
@dataclass
class IncomingMessage:
    message_id: str
    chat_id: str
    user_id: str
    platform: str
    content_type: ContentType
    text: str | None = None
    media_path: str | None = None
    user_capabilities: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
```

Update `to_dict()` — add:
```python
"user_capabilities": self.user_capabilities,
```

Update `from_dict()` — add:
```python
user_capabilities=data.get("user_capabilities", []),
```

- [ ] **Step 3: Verify syntax**

Run: `python -m py_compile src/valentine/config.py && python -m py_compile src/valentine/models.py && echo OK`

- [ ] **Step 4: Commit**

```bash
git add src/valentine/config.py src/valentine/models.py
git commit -m "feat: add admin_telegram_id, db_path config and user_capabilities to IncomingMessage"
```

---

### Task 2: SQLite UserDB

**Files:**
- Create: `src/valentine/db.py`
- Create: `tests/test_db.py`

- [ ] **Step 1: Write UserDB tests**

Create `tests/test_db.py`:

```python
# tests/test_db.py
from __future__ import annotations
import os
import pytest
from valentine.db import UserDB

@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "test.db")
    return UserDB(path)

def test_create_and_get_user(db):
    user = db.create_user("123", "testuser", "Test User")
    assert user["user_id"] == "123"
    assert user["access_level"] == "pending"
    fetched = db.get_user("123")
    assert fetched is not None
    assert fetched["telegram_username"] == "testuser"

def test_get_nonexistent_user(db):
    assert db.get_user("999") is None

def test_approve_user(db):
    db.create_user("123", "testuser", "Test")
    user = db.approve_user("123", "pro", "admin1", ["oracle", "codesmith", "iris"])
    assert user["access_level"] == "pro"
    assert "oracle" in user["capabilities"]

def test_revoke_user(db):
    db.create_user("123", "testuser", "Test")
    db.approve_user("123", "pro", "admin1", ["oracle"])
    db.revoke_user("123", "admin1")
    user = db.get_user("123")
    assert user["access_level"] == "revoked"

def test_capabilities(db):
    db.create_user("123", "testuser", "Test")
    db.approve_user("123", "basic", "admin1", ["oracle"])
    assert db.has_capability("123", "oracle") is True
    assert db.has_capability("123", "codesmith") is False
    db.add_capability("123", "codesmith")
    assert db.has_capability("123", "codesmith") is True
    db.remove_capability("123", "codesmith")
    assert db.has_capability("123", "codesmith") is False

def test_is_admin(db):
    db.create_user("123", "testuser", "Test")
    db.approve_user("123", "admin", "system", ["oracle"])
    assert db.is_admin("123") is True

def test_list_users(db):
    db.create_user("1", "a", "A")
    db.create_user("2", "b", "B")
    db.approve_user("1", "pro", "admin", ["oracle"])
    all_users = db.list_users()
    assert len(all_users) == 2
    pro_users = db.list_users(level="pro")
    assert len(pro_users) == 1

def test_update_user(db):
    db.create_user("123", "testuser", "Test")
    db.update_user("123", display_name="New Name", preferences='{"tone": "casual"}')
    user = db.get_user("123")
    assert user["display_name"] == "New Name"
    assert "casual" in user["preferences"]

def test_audit_log(db):
    db.log_action("admin1", "approve", "user1", "approved as pro")
    log = db.get_audit_log(limit=10)
    assert len(log) == 1
    assert log[0]["action"] == "approve"

def test_admin_notified_flag(db):
    db.create_user("123", "testuser", "Test")
    user = db.get_user("123")
    assert user["admin_notified"] == 0
    db.update_user("123", admin_notified=1)
    user = db.get_user("123")
    assert user["admin_notified"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src python -m pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'valentine.db'`

- [ ] **Step 3: Implement UserDB**

Create `src/valentine/db.py`:

```python
# src/valentine/db.py
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

ALL_CAPABILITIES = [
    "oracle", "codesmith", "iris", "echo", "nexus",
    "web_search", "shell_exec", "skills", "image_gen",
]

DEFAULT_CAPABILITIES = {
    "admin": ALL_CAPABILITIES,
    "pro": [c for c in ALL_CAPABILITIES if c != "shell_exec"],
    "basic": ["oracle"],
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id             TEXT PRIMARY KEY,
    telegram_username   TEXT,
    display_name        TEXT,
    access_level        TEXT NOT NULL DEFAULT 'pending',
    capabilities        TEXT NOT NULL DEFAULT '[]',
    onboarding_complete INTEGER NOT NULL DEFAULT 0,
    preferences         TEXT NOT NULL DEFAULT '{}',
    admin_notified      INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    granted_by          TEXT,
    granted_at          TEXT,
    revoked_at          TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id    TEXT NOT NULL,
    action      TEXT NOT NULL,
    target_user TEXT,
    details     TEXT,
    timestamp   TEXT NOT NULL
);
"""


class UserDB:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ---- helpers ----

    def _row_to_dict(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        if "capabilities" in d:
            d["capabilities"] = json.loads(d["capabilities"])
        return d

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # ---- CRUD ----

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        cur = self._conn.execute("SELECT * FROM users WHERE user_id = ?", (str(user_id),))
        return self._row_to_dict(cur.fetchone())

    def create_user(self, user_id: str, username: str, display_name: str) -> dict[str, Any]:
        user_id = str(user_id)
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO users (user_id, telegram_username, display_name, created_at) VALUES (?, ?, ?, ?)",
                (user_id, username, display_name, self._now()),
            )
            self._conn.commit()
        return self.get_user(user_id)

    def approve_user(self, user_id: str, level: str, granted_by: str, capabilities: list[str] | None = None) -> dict[str, Any]:
        user_id = str(user_id)
        caps = capabilities or DEFAULT_CAPABILITIES.get(level, ["oracle"])
        now = self._now()
        with self._lock:
            self._conn.execute(
                "UPDATE users SET access_level = ?, capabilities = ?, granted_by = ?, granted_at = ?, revoked_at = NULL WHERE user_id = ?",
                (level, json.dumps(caps), granted_by, now, user_id),
            )
            self._conn.commit()
        self.log_action(granted_by, "approve", user_id, f"level={level}")
        return self.get_user(user_id)

    def revoke_user(self, user_id: str, admin_id: str):
        user_id = str(user_id)
        with self._lock:
            self._conn.execute(
                "UPDATE users SET access_level = 'revoked', revoked_at = ? WHERE user_id = ?",
                (self._now(), user_id),
            )
            self._conn.commit()
        self.log_action(admin_id, "revoke", user_id, "access revoked")

    def update_user(self, user_id: str, **fields):
        user_id = str(user_id)
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [user_id]
        with self._lock:
            self._conn.execute(f"UPDATE users SET {set_clause} WHERE user_id = ?", values)
            self._conn.commit()

    # ---- capabilities ----

    def set_capabilities(self, user_id: str, capabilities: list[str]):
        self.update_user(user_id, capabilities=json.dumps(capabilities))

    def add_capability(self, user_id: str, capability: str):
        user = self.get_user(str(user_id))
        if not user:
            return
        caps = user["capabilities"]
        if capability not in caps:
            caps.append(capability)
            self.set_capabilities(user_id, caps)

    def remove_capability(self, user_id: str, capability: str):
        user = self.get_user(str(user_id))
        if not user:
            return
        caps = user["capabilities"]
        if capability in caps:
            caps.remove(capability)
            self.set_capabilities(user_id, caps)

    def has_capability(self, user_id: str, capability: str) -> bool:
        user = self.get_user(str(user_id))
        if not user:
            return False
        return capability in user["capabilities"]

    def is_admin(self, user_id: str) -> bool:
        user = self.get_user(str(user_id))
        return user is not None and user["access_level"] == "admin"

    # ---- listing ----

    def list_users(self, level: str | None = None) -> list[dict[str, Any]]:
        if level:
            cur = self._conn.execute("SELECT * FROM users WHERE access_level = ?", (level,))
        else:
            cur = self._conn.execute("SELECT * FROM users")
        return [self._row_to_dict(row) for row in cur.fetchall()]

    # ---- audit ----

    def log_action(self, admin_id: str, action: str, target_user: str | None = None, details: str | None = None):
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_log (admin_id, action, target_user, details, timestamp) VALUES (?, ?, ?, ?, ?)",
                (admin_id, action, target_user, details, self._now()),
            )
            self._conn.commit()

    def get_audit_log(self, limit: int = 50) -> list[dict[str, Any]]:
        cur = self._conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(row) for row in cur.fetchall()]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src python -m pytest tests/test_db.py -v`
Expected: All 11 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/valentine/db.py tests/test_db.py
git commit -m "feat: add UserDB with SQLite access control, capabilities, and audit log"
```

---

### Task 3: Onboarding State Machine

**Files:**
- Create: `src/valentine/nexus/onboarding.py`
- Create: `tests/test_onboarding.py`

- [ ] **Step 1: Write onboarding tests**

Create `tests/test_onboarding.py`:

```python
# tests/test_onboarding.py
from __future__ import annotations
import pytest
from valentine.nexus.onboarding import OnboardingFlow, STEPS

def test_steps_order():
    assert STEPS[0] == "welcome"
    assert STEPS[-1] == "complete"
    assert "name" in STEPS
    assert "timezone" in STEPS

def test_new_flow():
    flow = OnboardingFlow.new("user123")
    assert flow.step == "welcome"
    assert flow.data == {}
    assert flow.user_id == "user123"

def test_advance_step():
    flow = OnboardingFlow.new("user123")
    flow.set_answer("welcome", "start")
    flow.advance()
    assert flow.step == "name"

def test_set_answer():
    flow = OnboardingFlow.new("user123")
    flow.advance()  # → name
    flow.set_answer("name", "David")
    assert flow.data["name"] == "David"

def test_skip_step():
    flow = OnboardingFlow.new("user123")
    flow.advance()  # → name
    flow.skip()
    assert flow.step == "timezone"

def test_skip_all():
    flow = OnboardingFlow.new("user123")
    flow.skip_all()
    assert flow.is_complete

def test_conditional_tools_step():
    flow = OnboardingFlow.new("user123")
    # Advance to usage_intent
    while flow.step != "usage_intent":
        flow.advance()
    # Without coding selected, tools should be skipped
    flow.data["usage_intent"] = ["research"]
    flow.advance()
    assert flow.step != "tools"  # should skip to comm_style

def test_conditional_tools_shown():
    flow = OnboardingFlow.new("user123")
    while flow.step != "usage_intent":
        flow.advance()
    flow.data["usage_intent"] = ["coding"]
    flow.advance()
    assert flow.step == "tools"

def test_get_step_config():
    flow = OnboardingFlow.new("user123")
    config = flow.get_step_config()
    assert "message" in config
    assert config["step"] == "welcome"

def test_serialization():
    flow = OnboardingFlow.new("user123")
    flow.advance()
    flow.set_answer("name", "David")
    data = flow.to_dict()
    restored = OnboardingFlow.from_dict(data)
    assert restored.step == flow.step
    assert restored.data["name"] == "David"
    assert restored.user_id == "user123"

def test_multi_select_toggle():
    flow = OnboardingFlow.new("user123")
    while flow.step != "usage_intent":
        flow.advance()
    flow.toggle_select("coding")
    assert "coding" in flow.data.get("usage_intent", [])
    flow.toggle_select("coding")
    assert "coding" not in flow.data.get("usage_intent", [])

def test_single_step_mode():
    """Settings update should only change one field, not re-run full onboarding."""
    existing = {"name": "David", "timezone": "Africa", "role": "Developer"}
    flow = OnboardingFlow.for_setting("user123", "name", existing)
    assert flow.single_step_mode is True
    assert flow.step == "name"
    assert flow.data["timezone"] == "Africa"  # existing data preserved
    flow.set_answer("name", "Dave")
    flow.advance()
    assert flow.is_complete  # should jump to complete, not continue to timezone
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src python -m pytest tests/test_onboarding.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement OnboardingFlow**

Create `src/valentine/nexus/onboarding.py`:

```python
# src/valentine/nexus/onboarding.py
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

STEPS = [
    "welcome", "name", "timezone", "role", "role_detail", "experience",
    "usage_intent", "tools", "comm_style", "comm_tone",
    "proactivity", "personal", "complete",
]

STEP_CONFIGS = {
    "welcome": {
        "message": (
            "Hey! I'm Valentine — your personal AI assistant.\n"
            "I'd love to get to know you so I can be more helpful.\n"
            "Want to do a quick intro? Takes about 2 minutes."
        ),
        "buttons": [("Let's go!", "onboard:next"), ("Skip for now", "onboard:skip_all")],
        "input_type": "buttons",
    },
    "name": {
        "message": "What should I call you?",
        "buttons": [],
        "input_type": "text",
        "field": "name",
    },
    "timezone": {
        "message": "Where in the world are you?",
        "buttons": [
            ("Africa", "onboard:select:Africa"),
            ("Americas", "onboard:select:Americas"),
            ("Europe", "onboard:select:Europe"),
            ("Asia/Middle East", "onboard:select:Asia"),
            ("Pacific/Oceania", "onboard:select:Pacific"),
        ],
        "input_type": "buttons_or_text",
        "field": "timezone",
    },
    "role": {
        "message": "What do you do, {name}?",
        "buttons": [
            ("Student", "onboard:select:Student"),
            ("Developer/Engineer", "onboard:select:Developer/Engineer"),
            ("Designer", "onboard:select:Designer"),
            ("Business/Entrepreneur", "onboard:select:Business/Entrepreneur"),
            ("Creative/Writer", "onboard:select:Creative/Writer"),
            ("Researcher", "onboard:select:Researcher"),
            ("Other", "onboard:select:Other"),
        ],
        "input_type": "buttons_or_text",
        "field": "role",
    },
    "role_detail": {
        "message": "Nice! What's your focus area or specialty?",
        "buttons": [("Skip", "onboard:skip")],
        "input_type": "text",
        "field": "role_detail",
    },
    "experience": {
        "message": "How technical are you?",
        "buttons": [
            ("Just getting started", "onboard:select:beginner"),
            ("Intermediate", "onboard:select:intermediate"),
            ("Advanced", "onboard:select:advanced"),
            ("Expert", "onboard:select:expert"),
        ],
        "input_type": "buttons",
        "field": "experience",
    },
    "usage_intent": {
        "message": "What will you mainly use me for? Pick all that apply, then hit Done.",
        "buttons": [
            ("Coding", "onboard:toggle:coding"),
            ("Research", "onboard:toggle:research"),
            ("Writing", "onboard:toggle:writing"),
            ("Learning", "onboard:toggle:learning"),
            ("Productivity", "onboard:toggle:productivity"),
            ("Creative work", "onboard:toggle:creative"),
            ("Daily assistant", "onboard:toggle:assistant"),
            ("Just vibes", "onboard:toggle:vibes"),
            ("Done ✓", "onboard:done"),
        ],
        "input_type": "multi_select",
        "field": "usage_intent",
    },
    "tools": {
        "message": "What languages, frameworks, or tools do you use daily?",
        "buttons": [("Skip", "onboard:skip")],
        "input_type": "text",
        "field": "tools",
        "conditional": lambda data: "coding" in data.get("usage_intent", []),
    },
    "comm_style": {
        "message": "How do you like your responses?",
        "buttons": [
            ("Short & punchy", "onboard:select:concise"),
            ("Balanced", "onboard:select:balanced"),
            ("Detailed & thorough", "onboard:select:detailed"),
        ],
        "input_type": "buttons",
        "field": "comm_style",
    },
    "comm_tone": {
        "message": "And the tone?",
        "buttons": [
            ("Casual & friendly", "onboard:select:casual"),
            ("Professional", "onboard:select:professional"),
            ("Match my energy", "onboard:select:adaptive"),
        ],
        "input_type": "buttons",
        "field": "comm_tone",
    },
    "proactivity": {
        "message": "Should I be proactive — suggest things, follow up, check in?",
        "buttons": [
            ("Yes, be proactive", "onboard:select:proactive"),
            ("Only when I ask", "onboard:select:reactive"),
            ("Somewhere in between", "onboard:select:balanced"),
        ],
        "input_type": "buttons",
        "field": "proactivity",
    },
    "personal": {
        "message": (
            "Last one — anything else you want me to know?\n"
            "Hobbies, interests, things you geek out about, "
            "pet peeves, anything at all. Or skip this."
        ),
        "buttons": [("Skip", "onboard:skip")],
        "input_type": "text",
        "field": "personal",
    },
    "complete": {
        "message": (
            "Got it, {name}! Here's what I know about you:\n\n"
            "Name: {name}\n"
            "Location: {timezone}\n"
            "Role: {role} — {role_detail}\n"
            "Level: {experience}\n"
            "Uses: {usage_intent}\n"
            "Style: {comm_style}, {comm_tone}\n"
            "Proactivity: {proactivity}\n\n"
            "I'll remember all of this and keep learning as we chat.\n"
            "Update anytime with /settings. Now — what can I do for you?"
        ),
        "buttons": [],
        "input_type": "none",
    },
}


class OnboardingFlow:
    def __init__(self, user_id: str, step: str, data: dict[str, Any], started_at: str,
                 single_step_mode: bool = False):
        self.user_id = user_id
        self.step = step
        self.data = data
        self.started_at = started_at
        self.single_step_mode = single_step_mode

    @classmethod
    def new(cls, user_id: str) -> OnboardingFlow:
        return cls(
            user_id=user_id,
            step="welcome",
            data={},
            started_at=datetime.now(timezone.utc).isoformat(),
        )

    @classmethod
    def for_setting(cls, user_id: str, step: str, existing_data: dict[str, Any]) -> OnboardingFlow:
        """Create a single-step flow for updating one setting."""
        return cls(
            user_id=user_id,
            step=step,
            data=existing_data,
            started_at=datetime.now(timezone.utc).isoformat(),
            single_step_mode=True,
        )

    @classmethod
    def from_dict(cls, d: dict) -> OnboardingFlow:
        return cls(
            user_id=d["user_id"],
            step=d["step"],
            data=d.get("data", {}),
            started_at=d.get("started_at", ""),
            single_step_mode=d.get("single_step_mode", False),
        )

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "step": self.step,
            "data": self.data,
            "started_at": self.started_at,
            "single_step_mode": self.single_step_mode,
        }

    @property
    def is_complete(self) -> bool:
        return self.step == "complete"

    def get_step_config(self) -> dict[str, Any]:
        config = STEP_CONFIGS.get(self.step, {}).copy()
        config["step"] = self.step
        # Format message with collected data
        if "message" in config:
            safe_data = {k: (", ".join(v) if isinstance(v, list) else v or "—") for k, v in self.data.items()}
            safe_data.setdefault("name", "there")
            safe_data.setdefault("timezone", "—")
            safe_data.setdefault("role", "—")
            safe_data.setdefault("role_detail", "—")
            safe_data.setdefault("experience", "—")
            safe_data.setdefault("usage_intent", "—")
            safe_data.setdefault("comm_style", "—")
            safe_data.setdefault("comm_tone", "—")
            safe_data.setdefault("proactivity", "—")
            try:
                config["message"] = config["message"].format(**safe_data)
            except (KeyError, IndexError):
                pass
        return config

    def set_answer(self, step: str, value: Any):
        config = STEP_CONFIGS.get(step, {})
        field = config.get("field", step)
        self.data[field] = value

    def toggle_select(self, value: str):
        config = STEP_CONFIGS.get(self.step, {})
        field = config.get("field", self.step)
        current = self.data.get(field, [])
        if not isinstance(current, list):
            current = []
        if value in current:
            current.remove(value)
        else:
            current.append(value)
        self.data[field] = current

    def advance(self):
        if self.is_complete:
            return
        # In single-step mode (settings update), complete immediately after answering
        if self.single_step_mode:
            self.step = "complete"
            return
        idx = STEPS.index(self.step)
        # Move to next step, skipping conditional steps that don't apply
        while idx < len(STEPS) - 1:
            idx += 1
            next_step = STEPS[idx]
            config = STEP_CONFIGS.get(next_step, {})
            condition = config.get("conditional")
            if condition is None or condition(self.data):
                self.step = next_step
                return
        self.step = "complete"

    def skip(self):
        self.advance()

    def skip_all(self):
        self.step = "complete"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src python -m pytest tests/test_onboarding.py -v`
Expected: All 12 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/valentine/nexus/onboarding.py tests/test_onboarding.py
git commit -m "feat: add onboarding state machine with step configs, multi-select, and serialization"
```

---

### Task 4: Wire UserDB into Main + Adapter Constructor

**Files:**
- Modify: `src/valentine/main.py`
- Modify: `src/valentine/nexus/telegram.py`

- [ ] **Step 1: Update TelegramAdapter constructor to accept UserDB**

In `src/valentine/nexus/telegram.py`, update `__init__`:

```python
from valentine.db import UserDB

class TelegramAdapter(PlatformAdapter):
    def __init__(self, bus: RedisBus, db: UserDB):
        self.bus = bus
        self.db = db
        self.app = Application.builder().token(settings.telegram_bot_token).build()
        self._last_send: dict[str, float] = defaultdict(float)
        self._response_task: asyncio.Task | None = None
        self._setup_handlers()
```

- [ ] **Step 2: Update _run_bot_process in main.py**

In `src/valentine/main.py`, update the bot process to create and pass UserDB:

```python
def _run_bot_process():
    from valentine.nexus.telegram import TelegramAdapter
    from valentine.db import UserDB

    async def run():
        bus = _make_bus()
        db = UserDB(settings.db_path)
        adapter = TelegramAdapter(bus=bus, db=db)
        await adapter.start()
        stop_event = asyncio.Event()
        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await adapter.stop()
    # ... rest stays the same
```

- [ ] **Step 3: Verify syntax**

Run: `python -m py_compile src/valentine/main.py && python -m py_compile src/valentine/nexus/telegram.py && echo OK`

- [ ] **Step 4: Commit**

```bash
git add src/valentine/main.py src/valentine/nexus/telegram.py
git commit -m "feat: wire UserDB into TelegramAdapter via main.py bot process"
```

---

### Task 5: Access Control Gate in Telegram Adapter

**Files:**
- Modify: `src/valentine/nexus/telegram.py`

- [ ] **Step 1: Add access control helper methods**

Add these methods to `TelegramAdapter`:

```python
async def _check_access(self, update: Update) -> dict | None:
    """Check if user has access. Returns user dict or None.
    Handles pending/revoked/unknown users automatically."""
    user_id = str(update.effective_user.id)
    user = self.db.get_user(user_id)

    if user is None:
        # New user — create as pending
        username = update.effective_user.username or ""
        name = update.effective_user.full_name or ""
        user = self.db.create_user(user_id, username, name)

    # Auto-approve admin
    if user_id == settings.admin_telegram_id and user["access_level"] != "admin":
        from valentine.db import ALL_CAPABILITIES
        user = self.db.approve_user(user_id, "admin", "system", ALL_CAPABILITIES)

    level = user["access_level"]
    if level == "pending":
        await self._handle_pending(update, user)
        return None
    if level == "revoked":
        await update.message.reply_text("Your access has been revoked. Contact the admin if you think this is a mistake.")
        return None

    return user

async def _handle_pending(self, update: Update, user: dict):
    """Handle a pending user — notify them and alert admin once."""
    await update.message.reply_text(
        "Hey! I'm Valentine, a private AI assistant.\n"
        "I've notified my admin about your access request.\n"
        "You'll hear back soon!"
    )
    if not user.get("admin_notified"):
        admin_id = settings.admin_telegram_id
        if admin_id:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            username = user.get("telegram_username", "unknown")
            uid = user["user_id"]
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Approve as Pro", callback_data=f"approve:{uid}:pro"),
                    InlineKeyboardButton("Approve as Basic", callback_data=f"approve:{uid}:basic"),
                ],
                [InlineKeyboardButton("Deny", callback_data=f"deny:{uid}")],
            ])
            await self.app.bot.send_message(
                chat_id=admin_id,
                text=f"New access request:\n@{username} (ID: {uid})",
                reply_markup=keyboard,
            )
            self.db.update_user(uid, admin_notified=1)
```

- [ ] **Step 2: Gate all message handlers**

Update `_on_text`, `_on_photo`, `_on_voice`, `_on_document`, `_on_video` to check access first. Example for `_on_text`:

```python
async def _on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        user = await self._check_access(update)
        if not user:
            return

        # Check onboarding
        onboarding_state = await self._get_onboarding_state(user["user_id"])
        if onboarding_state and not onboarding_state.is_complete:
            return await self._handle_onboarding_step(update, user, onboarding_state, text=update.message.text)

        if not user["onboarding_complete"]:
            # Start onboarding
            return await self._start_onboarding(update, user)

        await self.send_typing(str(update.effective_chat.id))
        await self._route(update, ContentType.TEXT, update.message.text, user_capabilities=user["capabilities"])
    except Exception as e:
        logger.exception(f"Error handling text message: {e}")
        try:
            await update.message.reply_text(
                "Oops, something went sideways on my end. Try again in a sec?"
            )
        except Exception:
            pass
```

Also add a global error handler in `_setup_handlers`:

```python
async def _error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
    """Global error handler — ensures no message goes unanswered."""
    logger.error(f"Update {update} caused error: {context.error}")
    if update and hasattr(update, "effective_chat") and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Something unexpected happened. I'm still here though — try again!",
            )
        except Exception:
            pass

# In _setup_handlers, after all handlers:
self.app.add_error_handler(self._error_handler)
```

- [ ] **Step 3: Update _route to pass user_capabilities**

```python
async def _route(self, update: Update, content_type: ContentType, text: str,
                 media_path: str | None = None, user_capabilities: list[str] | None = None):
    msg = IncomingMessage(
        message_id=str(update.message.message_id),
        user_id=str(update.effective_user.id),
        chat_id=str(update.effective_chat.id),
        platform=MessageSource.TELEGRAM,
        content_type=content_type,
        text=text,
        media_path=media_path,
        user_capabilities=user_capabilities or [],
        timestamp=update.message.date,
    )
    # ... rest stays the same
```

- [ ] **Step 4: Verify syntax**

Run: `python -m py_compile src/valentine/nexus/telegram.py && echo OK`

- [ ] **Step 5: Commit**

```bash
git add src/valentine/nexus/telegram.py
git commit -m "feat: add access control gate — pending/revoked/admin auto-approve"
```

---

### Task 6: Onboarding Integration in Telegram Adapter

**Files:**
- Modify: `src/valentine/nexus/telegram.py`

- [ ] **Step 1: Add onboarding state management methods**

```python
from valentine.nexus.onboarding import OnboardingFlow

async def _get_onboarding_state(self, user_id: str) -> OnboardingFlow | None:
    raw = await self.bus.redis.get(f"onboarding:{user_id}")
    if raw:
        data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
        return OnboardingFlow.from_dict(data)
    return None

async def _save_onboarding_state(self, flow: OnboardingFlow):
    await self.bus.redis.set(
        f"onboarding:{flow.user_id}",
        json.dumps(flow.to_dict()),
        ex=604800,  # 7-day TTL
    )

async def _clear_onboarding_state(self, user_id: str):
    await self.bus.redis.delete(f"onboarding:{user_id}")

async def _start_onboarding(self, update: Update, user: dict):
    flow = OnboardingFlow.new(user["user_id"])
    await self._save_onboarding_state(flow)
    await self._send_onboarding_step(update, flow)

async def _send_onboarding_step(self, update_or_chat_id, flow: OnboardingFlow):
    config = flow.get_step_config()
    chat_id = update_or_chat_id if isinstance(update_or_chat_id, str) else str(update_or_chat_id.effective_chat.id)

    keyboard = None
    if config.get("buttons"):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        buttons = config["buttons"]
        # Arrange in rows of 2
        rows = []
        row = []
        for label, cb_data in buttons:
            row.append(InlineKeyboardButton(label, callback_data=cb_data))
            if len(row) >= 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        keyboard = InlineKeyboardMarkup(rows)

    await self._send_with_retry(
        self.app.bot.send_message,
        chat_id=chat_id,
        text=config["message"],
        reply_markup=keyboard,
    )
```

- [ ] **Step 2: Add onboarding step handler**

```python
async def _map_text_to_button(self, text: str, step_config: dict) -> str | None:
    """For button-only steps, map free text to the closest button option via LLM."""
    buttons = step_config.get("buttons", [])
    options = [label for label, _ in buttons if label not in ("Skip", "Done ✓")]
    if not options:
        return None
    messages = [
        {"role": "system", "content": "Map the user's text to one of these options. Reply with ONLY the exact option text. Options: " + ", ".join(options)},
        {"role": "user", "content": text},
    ]
    try:
        from valentine.llm.fallback import FallbackLLM
        llm = FallbackLLM()
        result = await llm.chat_completion(messages, temperature=0.0, max_tokens=50)
        result = result.strip().strip('"').strip("'")
        # Find matching button callback value
        for label, cb_data in buttons:
            if result.lower() == label.lower() or result.lower() in cb_data.lower():
                return cb_data.split(":")[-1]  # extract value from "onboard:select:value"
        return options[0].lower()  # fallback to first option
    except Exception:
        return None

async def _handle_onboarding_step(self, update: Update, user: dict, flow: OnboardingFlow, text: str | None = None):
    if text:
        config = STEP_CONFIGS.get(flow.step, {})
        input_type = config.get("input_type", "text")
        # For button-only steps, map text to closest option
        if input_type == "buttons" and config.get("buttons"):
            mapped = await self._map_text_to_button(text, config)
            if mapped:
                flow.set_answer(flow.step, mapped)
            else:
                await update.message.reply_text("Please use the buttons above, or I can try to match your answer.")
                return
        else:
            flow.set_answer(flow.step, text)
        flow.advance()

    if flow.is_complete:
        await self._complete_onboarding(update, user, flow)
        return

    await self._save_onboarding_state(flow)
    await self._send_onboarding_step(update, flow)

async def _complete_onboarding(self, update: Update, user: dict, flow: OnboardingFlow):
    # Save preferences to SQLite
    prefs = json.dumps({k: v for k, v in flow.data.items() if k != "welcome"})
    self.db.update_user(
        user["user_id"],
        display_name=flow.data.get("name", user.get("display_name", "")),
        preferences=prefs,
        onboarding_complete=1,
    )
    await self._clear_onboarding_state(user["user_id"])

    # Send completion message
    config = flow.get_step_config()
    await self._send_with_retry(
        self.app.bot.send_message,
        chat_id=str(update.effective_chat.id),
        text=config["message"],
    )

    # Fire onboarding facts to Cortex via bus
    extraction_task = {
        "type": "store_onboarding",
        "user_id": user["user_id"],
        "onboarding_data": flow.data,
    }
    await self.bus.add_task(self.bus.stream_name("cortex", "task"), extraction_task)
```

- [ ] **Step 3: Verify syntax**

Run: `python -m py_compile src/valentine/nexus/telegram.py && echo OK`

- [ ] **Step 4: Commit**

```bash
git add src/valentine/nexus/telegram.py
git commit -m "feat: add onboarding state machine integration — steps, persistence, completion"
```

---

### Task 7: Callback Handler for Inline Buttons

**Files:**
- Modify: `src/valentine/nexus/telegram.py`

- [ ] **Step 1: Add CallbackQueryHandler to setup**

In `_setup_handlers`, add:
```python
from telegram.ext import CallbackQueryHandler
self.app.add_handler(CallbackQueryHandler(self._on_callback))
```

- [ ] **Step 2: Implement callback dispatcher**

```python
async def _on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass
    data = query.data or ""
    user_id = str(query.from_user.id)

    parts = data.split(":")

    if parts[0] == "onboard":
        await self._handle_onboarding_callback(query, user_id, parts)
    elif parts[0] == "approve":
        await self._handle_approve_callback(query, user_id, parts)
    elif parts[0] == "deny":
        await self._handle_deny_callback(query, user_id, parts)
    elif parts[0] == "confirm":
        await self._handle_confirm_callback(query, user_id, parts)
    elif parts[0] == "settings":
        await self._handle_settings_callback(query, user_id, parts)

async def _handle_onboarding_callback(self, query, user_id, parts):
    flow = await self._get_onboarding_state(user_id)
    if not flow:
        return

    action = parts[1] if len(parts) > 1 else ""
    value = parts[2] if len(parts) > 2 else ""

    if action == "next":
        flow.advance()
    elif action == "skip":
        flow.skip()
    elif action == "skip_all":
        flow.skip_all()
    elif action == "select":
        flow.set_answer(flow.step, value)
        flow.advance()
    elif action == "toggle":
        flow.toggle_select(value)
        # Re-send the same step with updated buttons (checkmarks)
        await self._save_onboarding_state(flow)
        await self._send_onboarding_step(str(query.message.chat_id), flow)
        return
    elif action == "done":
        flow.advance()

    if flow.is_complete:
        user = self.db.get_user(user_id)
        await self._complete_onboarding_from_callback(query, user, flow)
        return

    await self._save_onboarding_state(flow)
    await self._send_onboarding_step(str(query.message.chat_id), flow)

async def _complete_onboarding_from_callback(self, query, user, flow):
    prefs = json.dumps({k: v for k, v in flow.data.items() if k != "welcome"})
    self.db.update_user(
        user["user_id"],
        display_name=flow.data.get("name", user.get("display_name", "")),
        preferences=prefs,
        onboarding_complete=1,
    )
    await self._clear_onboarding_state(user["user_id"])
    config = flow.get_step_config()
    await self.app.bot.send_message(chat_id=str(query.message.chat_id), text=config["message"])
    extraction_task = {"type": "store_onboarding", "user_id": user["user_id"], "onboarding_data": flow.data}
    await self.bus.add_task(self.bus.stream_name("cortex", "task"), extraction_task)

async def _handle_approve_callback(self, query, admin_id, parts):
    if admin_id != settings.admin_telegram_id:
        return
    target_uid = parts[1] if len(parts) > 1 else ""
    level = parts[2] if len(parts) > 2 else "pro"
    from valentine.db import DEFAULT_CAPABILITIES
    self.db.approve_user(target_uid, level, admin_id, DEFAULT_CAPABILITIES.get(level))
    await query.edit_message_text(f"Approved user {target_uid} as {level}.")
    # Notify the user
    try:
        await self.app.bot.send_message(
            chat_id=target_uid,
            text=f"You've been approved! Welcome to Valentine. Send me a message to get started.",
        )
    except Exception:
        pass

async def _handle_deny_callback(self, query, admin_id, parts):
    if admin_id != settings.admin_telegram_id:
        return
    target_uid = parts[1] if len(parts) > 1 else ""
    self.db.revoke_user(target_uid, admin_id)
    await query.edit_message_text(f"Denied user {target_uid}.")

async def _handle_confirm_callback(self, query, user_id, parts):
    action = parts[1] if len(parts) > 1 else ""
    if action == "cancel":
        await query.edit_message_text("Cancelled.")
    elif action == "reset":
        await self.bus.redis.delete(f"chat:{str(query.message.chat_id)}:history")
        await query.edit_message_text("Conversation history cleared.")
    elif action == "revoke" and user_id == settings.admin_telegram_id:
        target = parts[2] if len(parts) > 2 else ""
        self.db.revoke_user(target, user_id)
        await query.edit_message_text(f"Revoked access for {target}.")
    elif action == "memclear" and user_id == settings.admin_telegram_id:
        target = parts[2] if len(parts) > 2 else ""
        task = {"type": "forget_all_memory", "user_id": target}
        await self.bus.add_task(self.bus.stream_name("cortex", "task"), task)
        await query.edit_message_text(f"Clearing all memories for {target}...")
    elif action == "bc" and user_id == settings.admin_telegram_id:
        bc_key = parts[2] if len(parts) > 2 else ""
        raw = await self.bus.redis.get(bc_key)
        if not raw:
            await query.edit_message_text("Broadcast expired. Try again with /broadcast.")
            return
        msg_text = raw if isinstance(raw, str) else raw.decode("utf-8")
        await self.bus.redis.delete(bc_key)
        users = self.db.list_users()
        sent = 0
        for u in users:
            if u["access_level"] in ("pro", "basic", "admin"):
                try:
                    await self.app.bot.send_message(chat_id=u["user_id"], text=f"[Broadcast] {msg_text}")
                    sent += 1
                except Exception:
                    pass
        await query.edit_message_text(f"Broadcast sent to {sent} user(s).")

async def _handle_settings_callback(self, query, user_id, parts):
    category = parts[1] if len(parts) > 1 else ""
    step_map = {
        "name": "name", "timezone": "timezone", "role": "role",
        "comm": "comm_style", "usage": "usage_intent",
        "proactivity": "proactivity", "personal": "personal",
    }
    target_step = step_map.get(category)
    if target_step:
        # Load existing preferences so completion message shows all fields
        user = self.db.get_user(user_id)
        existing = json.loads(user.get("preferences", "{}")) if user else {}
        flow = OnboardingFlow.for_setting(user_id, target_step, existing)
        await self._save_onboarding_state(flow)
        await self._send_onboarding_step(str(query.message.chat_id), flow)
```

- [ ] **Step 3: Verify syntax**

Run: `python -m py_compile src/valentine/nexus/telegram.py && echo OK`

- [ ] **Step 4: Commit**

```bash
git add src/valentine/nexus/telegram.py
git commit -m "feat: add callback handler for inline buttons — onboarding, approve, deny, settings"
```

---

### Task 8: Bot Commands (User + Admin)

**Files:**
- Modify: `src/valentine/nexus/telegram.py`

- [ ] **Step 1: Update _setup_handlers with all commands**

Replace the existing `_setup_handlers` method with the full version including all command handlers and the callback handler. Register user commands, admin commands, callback handler, then message handlers.

- [ ] **Step 2: Implement user commands**

```python
async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await self._check_access(update)
    if not user:
        return
    if not user["onboarding_complete"]:
        await self._start_onboarding(update, user)
    else:
        name = user.get("display_name") or "there"
        await update.message.reply_text(
            f"Hey {name}! I'm already set up and ready. "
            f"Type /help to see what I can do, or just send me a message!"
        )

async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await self._check_access(update)
    if not user:
        return
    user_id = str(update.effective_user.id)
    is_admin = user_id == settings.admin_telegram_id
    text = "Available commands:\n\n"
    text += "/help — This menu\n"
    text += "/settings — Update your preferences\n"
    text += "/reset — Clear conversation history\n"
    text += "/me — View your profile\n"
    text += "/forget <topic> — Forget something about you\n"
    if is_admin:
        text += "\nAdmin commands:\n"
        text += "/approve <user> [level] — Approve a user\n"
        text += "/revoke <user> — Revoke access\n"
        text += "/users — List all users\n"
        text += "/setlevel <user> <level> — Change access tier\n"
        text += "/grant <user> <cap> — Add capability\n"
        text += "/deny <user> <cap> — Remove capability\n"
        text += "/status — Server health\n"
        text += "/logs [n] — View logs\n"
        text += "/skills — List skills\n"
        text += "/memory <user> — View/clear user memories\n"
        text += "/broadcast <msg> — Message all users\n"
    await update.message.reply_text(text)

async def _cmd_settings(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await self._check_access(update)
    if not user:
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Name", callback_data="settings:name"),
         InlineKeyboardButton("Timezone", callback_data="settings:timezone")],
        [InlineKeyboardButton("Role", callback_data="settings:role"),
         InlineKeyboardButton("Communication", callback_data="settings:comm")],
        [InlineKeyboardButton("Usage", callback_data="settings:usage"),
         InlineKeyboardButton("Proactivity", callback_data="settings:proactivity")],
        [InlineKeyboardButton("Personal", callback_data="settings:personal")],
    ])
    await update.message.reply_text("What would you like to update?", reply_markup=keyboard)

async def _cmd_reset(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await self._check_access(update)
    if not user:
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Yes, clear it", callback_data="confirm:reset"),
         InlineKeyboardButton("Cancel", callback_data="confirm:cancel")],
    ])
    await update.message.reply_text("Clear your conversation history?", reply_markup=keyboard)

async def _cmd_me(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await self._check_access(update)
    if not user:
        return
    prefs = json.loads(user.get("preferences", "{}"))
    text = f"Your profile:\n\n"
    text += f"Name: {user.get('display_name', '—')}\n"
    text += f"Access: {user['access_level']}\n"
    text += f"Capabilities: {', '.join(user['capabilities'])}\n"
    if prefs:
        text += f"\nPreferences:\n"
        for k, v in prefs.items():
            text += f"  {k}: {v}\n"
    await update.message.reply_text(text)

async def _cmd_skip(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await self._check_access(update)
    if not user:
        return
    user_id = str(update.effective_user.id)
    flow = await self._get_onboarding_state(user_id)
    if flow:
        flow.skip_all()
        user = self.db.get_user(user_id)
        if user:
            self.db.update_user(user_id, onboarding_complete=1)
        await self._clear_onboarding_state(user_id)
        await update.message.reply_text("Onboarding skipped! You can set preferences anytime with /settings.\nNow — what can I do for you?")
    else:
        await update.message.reply_text("Nothing to skip.")

async def _cmd_forget(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await self._check_access(update)
    if not user:
        return
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("Usage: /forget <topic>\nExample: /forget my job title")
        return
    topic = args[1]
    # Fire forget task to Cortex with reply chat_id for confirmation
    task = {
        "type": "forget_memory",
        "user_id": str(update.effective_user.id),
        "topic": topic,
        "reply_chat_id": str(update.effective_chat.id),
    }
    await self.bus.add_task(self.bus.stream_name("cortex", "task"), task)
    await update.message.reply_text(f"Looking for memories about: {topic}...")
```

- [ ] **Step 3: Implement admin commands**

```python
async def _admin_check(self, update: Update) -> bool:
    if str(update.effective_user.id) != settings.admin_telegram_id:
        await update.message.reply_text("Admin only.")
        return False
    return True

async def _cmd_approve(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await self._admin_check(update):
        return
    args = update.message.text.split()
    if len(args) < 2:
        await update.message.reply_text("Usage: /approve <user_id> [pro|basic]")
        return
    target = args[1]
    level = args[2] if len(args) > 2 else None
    if not level:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("As Pro", callback_data=f"approve:{target}:pro"),
             InlineKeyboardButton("As Basic", callback_data=f"approve:{target}:basic")],
        ])
        await update.message.reply_text(f"Approve {target} as:", reply_markup=keyboard)
        return
    from valentine.db import DEFAULT_CAPABILITIES
    self.db.approve_user(target, level, str(update.effective_user.id), DEFAULT_CAPABILITIES.get(level))
    await update.message.reply_text(f"Approved {target} as {level}.")
    try:
        await self.app.bot.send_message(chat_id=target, text="You've been approved! Send me a message to get started.")
    except Exception:
        pass

async def _cmd_revoke(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await self._admin_check(update):
        return
    args = update.message.text.split()
    if len(args) < 2:
        await update.message.reply_text("Usage: /revoke <user_id>")
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    target = args[1]
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Yes, revoke", callback_data=f"confirm:revoke:{target}"),
         InlineKeyboardButton("Cancel", callback_data="confirm:cancel")],
    ])
    await update.message.reply_text(f"Revoke access for {target}?", reply_markup=keyboard)

async def _cmd_users(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await self._admin_check(update):
        return
    users = self.db.list_users()
    if not users:
        await update.message.reply_text("No users yet.")
        return
    lines = []
    for u in users:
        name = u.get("display_name") or u.get("telegram_username") or u["user_id"]
        lines.append(f"  {name} ({u['user_id']}) — {u['access_level']}")
    await update.message.reply_text(f"Users ({len(users)}):\n" + "\n".join(lines))

async def _cmd_setlevel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await self._admin_check(update):
        return
    args = update.message.text.split()
    if len(args) < 3:
        await update.message.reply_text("Usage: /setlevel <user_id> <admin|pro|basic>")
        return
    from valentine.db import DEFAULT_CAPABILITIES
    target, level = args[1], args[2]
    self.db.approve_user(target, level, str(update.effective_user.id), DEFAULT_CAPABILITIES.get(level))
    await update.message.reply_text(f"Set {target} to {level}.")

async def _cmd_grant(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await self._admin_check(update):
        return
    args = update.message.text.split()
    if len(args) < 3:
        await update.message.reply_text("Usage: /grant <user_id> <capability>")
        return
    self.db.add_capability(args[1], args[2])
    await update.message.reply_text(f"Granted {args[2]} to {args[1]}.")

async def _cmd_deny(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await self._admin_check(update):
        return
    args = update.message.text.split()
    if len(args) < 3:
        await update.message.reply_text("Usage: /deny <user_id> <capability>")
        return
    self.db.remove_capability(args[1], args[2])
    await update.message.reply_text(f"Removed {args[2]} from {args[1]}.")

async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await self._admin_check(update):
        return
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get("http://127.0.0.1:8080/health", timeout=5)
            await update.message.reply_text(f"Health:\n{r.text}")
    except Exception as e:
        await update.message.reply_text(f"Health check failed: {e}")

async def _cmd_logs(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await self._admin_check(update):
        return
    args = update.message.text.split()
    n = int(args[1]) if len(args) > 1 else 20
    try:
        proc = await asyncio.create_subprocess_exec(
            "journalctl", "-u", "valentine.service", "--no-pager", "-n", str(n),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        text = stdout.decode()[-4000:] if stdout else "No logs."
        await update.message.reply_text(f"Last {n} log lines:\n\n{text}")
    except Exception as e:
        await update.message.reply_text(f"Failed to get logs: {e}")

async def _cmd_skills(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await self._admin_check(update):
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", f"{settings.skills_builtin_dir}/../skills.sh", "list",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=settings.workspace_dir,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        await update.message.reply_text(stdout.decode() if stdout else "No skills found.")
    except Exception as e:
        await update.message.reply_text(f"Failed to list skills: {e}")

async def _cmd_memory(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await self._admin_check(update):
        return
    args = update.message.text.split()
    if len(args) < 2:
        await update.message.reply_text("Usage: /memory <user_id> or /memory clear <user_id>")
        return
    if args[1] == "clear" and len(args) > 2:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        target = args[2]
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Yes, clear all", callback_data=f"confirm:memclear:{target}"),
             InlineKeyboardButton("Cancel", callback_data="confirm:cancel")],
        ])
        await update.message.reply_text(f"Wipe all memories for {target}?", reply_markup=keyboard)
        return
    target = args[1]
    task = {"type": "search_memory", "user_id": target, "query": "everything about this user", "reply_chat_id": str(update.effective_chat.id)}
    await self.bus.add_task(self.bus.stream_name("cortex", "task"), task)
    await update.message.reply_text(f"Fetching memories for {target}...")

async def _cmd_broadcast(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await self._admin_check(update):
        return
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    msg = args[1]
    active_count = len([u for u in self.db.list_users() if u["access_level"] in ("pro", "basic", "admin")])
    # Store broadcast text in Redis (callback_data has 64-byte Telegram limit)
    import uuid
    bc_key = f"broadcast:{uuid.uuid4().hex[:8]}"
    await self.bus.redis.set(bc_key, msg, ex=300)  # 5-min TTL
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Yes, send to {active_count} users", callback_data=f"confirm:bc:{bc_key}"),
         InlineKeyboardButton("Cancel", callback_data="confirm:cancel")],
    ])
    await update.message.reply_text(f"Broadcast to {active_count} active users:\n\n\"{msg}\"", reply_markup=keyboard)
```

- [ ] **Step 4: Verify syntax**

Run: `python -m py_compile src/valentine/nexus/telegram.py && echo OK`

- [ ] **Step 5: Commit**

```bash
git add src/valentine/nexus/telegram.py
git commit -m "feat: add all user and admin commands with inline buttons"
```

---

### Task 9: ZeroClaw Capability Enforcement

**Files:**
- Modify: `src/valentine/orchestrator/zeroclaw.py`
- Modify: `src/valentine/agents/oracle.py`

- [ ] **Step 1: Add capability checking to ZeroClaw**

In `zeroclaw.py`, after determining `target_agent` and before creating the delegated task, add:

```python
# Capability enforcement
AGENT_CAPABILITIES = {
    AgentName.ORACLE: "oracle",
    AgentName.CODESMITH: "codesmith",
    AgentName.IRIS: "iris",
    AgentName.ECHO: "echo",
    AgentName.NEXUS: "nexus",
}

user_caps = msg.user_capabilities or []
if user_caps:  # empty means no restriction (internal or system message)
    required_cap = AGENT_CAPABILITIES.get(target_agent, "oracle")
    if required_cap not in user_caps:
        original_agent = target_agent.value
        target_agent = AgentName.ORACLE
        routing = RoutingDecision(
            intent="capability_blocked",
            agent=target_agent,
            priority=data.get("priority", "normal"),
            params={"blocked_agent": original_agent, "blocked_reason": required_cap},
            memory_context=context_items,
        )
```

- [ ] **Step 2: Add context injection via mem0 in ZeroClaw**

Update `_fetch_context` in `zeroclaw.py` to call mem0 directly (ZeroClaw runs in its own process, can't call Cortex directly):

Also add to `__init__`:
```python
self._memory = None  # lazy-initialized mem0 instance
```

```python
import asyncio

def _get_memory(self):
    """Lazy-initialize mem0 (created once, reused across calls)."""
    if self._memory is None:
        try:
            import mem0
            from valentine.config import settings
            self._memory = mem0.Memory.from_config({
                "vector_store": {
                    "provider": "qdrant",
                    "config": {"host": settings.qdrant_host, "port": settings.qdrant_port},
                }
            })
        except Exception as e:
            logger.debug(f"mem0 init failed: {e}")
    return self._memory

async def _fetch_context(self, message: IncomingMessage) -> List[str]:
    """Fetch relevant memory context for routing decisions."""
    if not message.text or not message.user_id:
        return []
    memory = self._get_memory()
    if not memory:
        return []
    try:
        results = await asyncio.to_thread(
            memory.search, message.text, user_id=message.user_id, limit=3,
        )
        return [r.get("text", r.get("memory", "")) for r in results if r]
    except Exception as e:
        logger.debug(f"Context fetch failed (non-critical): {e}")
        return []
```

- [ ] **Step 3: Handle capability_blocked in Oracle**

In `src/valentine/agents/oracle.py`, at the start of `process_task()`:

```python
async def process_task(self, task: AgentTask) -> TaskResult:
    intent = task.routing.intent
    msg = task.message
    chat_id = msg.chat_id

    # Handle blocked capabilities
    if intent == "capability_blocked":
        blocked = task.routing.params.get("blocked_agent", "that feature")
        return TaskResult(
            task_id=task.task_id, agent=self.name, success=True,
            text=(
                f"I'd love to help with that, but your current access level "
                f"doesn't include {blocked} capabilities. "
                f"Ask the admin to upgrade your access if you need this!"
            ),
        )

    # ... rest of existing process_task
```

- [ ] **Step 4: Verify syntax**

Run: `python -m py_compile src/valentine/orchestrator/zeroclaw.py && python -m py_compile src/valentine/agents/oracle.py && echo OK`

- [ ] **Step 5: Commit**

```bash
git add src/valentine/orchestrator/zeroclaw.py src/valentine/agents/oracle.py
git commit -m "feat: add capability enforcement in ZeroClaw with user-facing blocked message"
```

---

### Task 10: BaseAgent Memory Extraction Trigger

**Files:**
- Modify: `src/valentine/agents/base.py`

- [ ] **Step 1: Store last user message in listen_for_tasks**

In `listen_for_tasks()`, before calling `process_task()`:

```python
# Store for memory extraction
self._last_user_msg = getattr(task.message, 'text', '') or ''
self._last_user_id = getattr(task.message, 'user_id', '')
```

- [ ] **Step 2: Fire extraction in publish_result**

Update `publish_result()`:

```python
async def publish_result(self, result: TaskResult):
    await self.bus.add_task(self.result_stream, result.to_dict())
    await self.bus.publish("agent.response", result.to_dict())

    # Fire memory extraction to Cortex (non-blocking, best-effort)
    if (result.success and result.text
            and getattr(self, '_last_user_msg', '')
            and getattr(self, '_last_user_id', '')
            and self.name != AgentName.ZEROCLAW
            and self.name != AgentName.CORTEX):
        try:
            extraction_task = {
                "type": "extract_memory",
                "user_id": self._last_user_id,
                "user_message": self._last_user_msg,
                "agent_response": result.text[:1000],
            }
            await self.bus.add_task(
                self.bus.stream_name("cortex", "task"),
                extraction_task,
            )
        except Exception:
            pass  # best-effort, don't break response flow
```

- [ ] **Step 3: Verify syntax**

Run: `python -m py_compile src/valentine/agents/base.py && echo OK`

- [ ] **Step 4: Commit**

```bash
git add src/valentine/agents/base.py
git commit -m "feat: fire async memory extraction to Cortex after every agent response"
```

---

### Task 11: Cortex Memory Extraction & Onboarding Storage

**Files:**
- Modify: `src/valentine/agents/cortex.py`

- [ ] **Step 1: Add asyncio import and extraction prompt**

At the top of `cortex.py`:
```python
import asyncio
```

Add the extraction prompt as a module constant:
```python
MEMORY_EXTRACTION_PROMPT = """Analyze this conversation exchange and extract facts worth remembering about the user for future conversations. Focus on:
- Personal details (name, location, job, relationships)
- Technical preferences (languages, tools, frameworks, stack)
- Communication preferences (tone, detail level, style)
- Current projects and goals
- Opinions, likes, dislikes

Only extract CONCRETE facts. Skip greetings, pleasantries, and transient queries. If nothing worth remembering, respond with "NONE".

Output one fact per line.

User said: {user_message}
Agent responded: {agent_response}"""
```

- [ ] **Step 2: Override listen_for_tasks to handle raw dicts**

The Cortex agent needs to handle both normal `AgentTask` payloads and raw dict payloads (memory extraction, onboarding storage, forget). Override the task parsing in `listen_for_tasks` or handle in `process_task`:

```python
async def process_task(self, task: AgentTask) -> TaskResult:
    # This gets called from BaseAgent.listen_for_tasks which already
    # parses AgentTask. We need to handle special task types that come
    # as raw dicts. These are caught in listen_for_tasks override.
    intent = task.routing.intent
    msg = task.message
    # ... existing logic unchanged
```

Add a new method and override how Cortex reads tasks:

```python
async def _handle_raw_task(self, payload: dict) -> None:
    """Handle raw dict tasks (memory extraction, onboarding, forget)."""
    task_type = payload.get("type", "")

    if task_type == "extract_memory":
        await self._extract_from_exchange(payload)
    elif task_type == "store_onboarding":
        await self._store_onboarding_facts(payload["user_id"], payload["onboarding_data"])
    elif task_type == "forget_memory":
        await self._forget_memory(payload)
    elif task_type == "forget_all_memory":
        await self._forget_all_memory(payload["user_id"])
    elif task_type == "search_memory":
        await self._search_and_reply(payload)

async def _extract_from_exchange(self, data: dict):
    user_id = data["user_id"]
    prompt = MEMORY_EXTRACTION_PROMPT.format(
        user_message=data.get("user_message", ""),
        agent_response=data.get("agent_response", ""),
    )
    messages = [
        {"role": "system", "content": self.system_prompt},
        {"role": "user", "content": prompt},
    ]
    try:
        extraction = await self.llm.chat_completion(messages, temperature=0.1)
        if extraction and "NONE" not in extraction.upper():
            for fact in extraction.strip().split("\n"):
                fact = fact.strip("- ").strip()
                if len(fact) > 5 and self.memory:
                    await asyncio.to_thread(self.memory.add, fact, user_id=user_id)
                    logger.info(f"Cortex stored fact for {user_id}: {fact[:80]}")
    except Exception as e:
        logger.error(f"Memory extraction failed: {e}")

async def _store_onboarding_facts(self, user_id: str, data: dict):
    if not self.memory:
        return
    facts = []
    if name := data.get("name"):
        facts.append(f"User's name is {name}")
    if tz := data.get("timezone"):
        facts.append(f"User is based in {tz}")
    if role := data.get("role"):
        detail = data.get("role_detail", "")
        facts.append(f"User is a {role}" + (f" specializing in {detail}" if detail else ""))
    if exp := data.get("experience"):
        facts.append(f"User's technical level is {exp}")
    if intent := data.get("usage_intent"):
        if isinstance(intent, list):
            facts.append(f"User mainly uses Valentine for: {', '.join(intent)}")
    if tools := data.get("tools"):
        facts.append(f"User works with: {tools}")
    if style := data.get("comm_style"):
        tone = data.get("comm_tone", "")
        facts.append(f"User prefers {style} responses with {tone} tone")
    if pro := data.get("proactivity"):
        facts.append(f"User wants Valentine to be {pro}")
    if personal := data.get("personal"):
        facts.append(f"User shared: {personal}")
    for fact in facts:
        try:
            await asyncio.to_thread(self.memory.add, fact, user_id=user_id)
            logger.info(f"Onboarding fact stored for {user_id}: {fact[:80]}")
        except Exception as e:
            logger.error(f"Failed to store onboarding fact: {e}")

async def _forget_memory(self, data: dict):
    user_id = data["user_id"]
    topic = data["topic"]
    reply_chat_id = data.get("reply_chat_id")
    if not self.memory:
        return
    deleted = []
    try:
        results = await asyncio.to_thread(self.memory.search, topic, user_id=user_id, limit=10)
        for r in results:
            mem_id = r.get("id")
            if mem_id:
                await asyncio.to_thread(self.memory.delete, mem_id)
                deleted.append(r.get("text", r.get("memory", "unknown")))
                logger.info(f"Deleted memory {mem_id} for {user_id}")
    except Exception as e:
        logger.error(f"Forget memory failed: {e}")
    # Send confirmation back
    if reply_chat_id:
        if deleted:
            summary = "\n".join(f"- {d[:100]}" for d in deleted)
            text = f"Forgot {len(deleted)} memory/memories about '{topic}':\n{summary}"
        else:
            text = f"I didn't find any memories about '{topic}'."
        result = TaskResult(
            task_id="forget_memory", agent=self.name,
            success=True, text=text, chat_id=reply_chat_id,
        )
        await self.bus.publish("agent.response", result.to_dict())

async def _forget_all_memory(self, user_id: str):
    if not self.memory:
        return
    try:
        await asyncio.to_thread(self.memory.delete_all, user_id=user_id)
        logger.info(f"Cleared all memories for {user_id}")
    except Exception as e:
        logger.error(f"Forget all memory failed: {e}")

async def _search_and_reply(self, data: dict):
    if not self.memory:
        return
    user_id = data["user_id"]
    query = data.get("query", "")
    reply_chat_id = data.get("reply_chat_id")
    try:
        results = await asyncio.to_thread(self.memory.search, query, user_id=user_id, limit=10)
        text = "\n".join(f"- {r.get('text', r.get('memory', ''))}" for r in results) if results else "No memories found."
        if reply_chat_id:
            result = TaskResult(
                task_id="memory_search",
                agent=self.name,
                success=True,
                text=f"Memories for {user_id}:\n{text}",
                chat_id=reply_chat_id,
            )
            await self.bus.publish("agent.response", result.to_dict())
    except Exception as e:
        logger.error(f"Memory search failed: {e}")
```

- [ ] **Step 3: Override listen_for_tasks to handle raw dicts**

In Cortex, override the base class to intercept raw payloads before AgentTask parsing:

```python
async def listen_for_tasks(self):
    logger.info(f"Agent {self.name.value} listening on {self.task_stream}...")
    while not self._shutdown_event.is_set():
        try:
            tasks = await self.bus.read_tasks(
                self.task_stream, self.consumer_group,
                self.consumer_name, count=1, timeout_ms=1000,
            )
            for message_id, payload in tasks:
                # Check if this is a raw task (not an AgentTask)
                if "type" in payload and payload.get("type") in (
                    "extract_memory", "store_onboarding", "forget_memory",
                    "forget_all_memory", "search_memory",
                ):
                    await self._handle_raw_task(payload)
                else:
                    # Normal AgentTask flow
                    task = AgentTask.from_dict(payload)
                    logger.info(f"Agent {self.name.value} received task: {task.task_id}")
                    import time
                    start = time.monotonic()
                    try:
                        result = await asyncio.wait_for(
                            self.process_task(task), timeout=self.task_timeout,
                        )
                        if getattr(task.message, "chat_id", None):
                            result.chat_id = task.message.chat_id
                    except Exception as e:
                        result = TaskResult(
                            task_id=task.task_id, agent=self.name,
                            success=False, error=str(e),
                        )
                    result.processing_time_ms = int((time.monotonic() - start) * 1000)
                    await self.publish_result(result)
                await self.bus.acknowledge_task(
                    self.task_stream, self.consumer_group, message_id,
                )
        except Exception as e:
            logger.error(f"Error reading tasks on {self.name.value}: {e}")
            await asyncio.sleep(1)
```

- [ ] **Step 4: Wrap existing mem0 calls with asyncio.to_thread**

Update `_extract_memories` and `fetch_context_for_routing` to use `asyncio.to_thread`:

```python
async def _extract_memories(self, msg):
    # ... existing LLM call ...
    if extraction and len(extraction) > 5 and "nothing" not in extraction.lower():
        await asyncio.to_thread(
            self.memory.add, extraction,
            user_id=msg.user_id, metadata={"source_msg": msg.message_id},
        )

async def fetch_context_for_routing(self, message) -> list[str]:
    if not self.memory or not message.text:
        return []
    try:
        results = await asyncio.to_thread(
            self.memory.search, message.text, user_id=message.user_id, limit=3,
        )
        return [r.get("text", r.get("memory", "")) for r in results]
    except Exception as e:
        logger.error(f"Memory fast-search failed: {e}")
        return []
```

- [ ] **Step 5: Verify syntax**

Run: `python -m py_compile src/valentine/agents/cortex.py && echo OK`

- [ ] **Step 6: Commit**

```bash
git add src/valentine/agents/cortex.py
git commit -m "feat: add memory extraction, onboarding storage, forget, and search to Cortex"
```

---

### Task 12: Voice Handling During Onboarding

**Files:**
- Modify: `src/valentine/nexus/telegram.py`

- [ ] **Step 1: Add inline transcription method**

```python
async def _transcribe_voice_inline(self, file_path: str) -> str:
    """Transcribe a voice note directly via Groq Whisper API (not routed through Echo)."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            with open(file_path, "rb") as f:
                response = await client.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                    files={"file": (os.path.basename(file_path), f)},
                    data={"model": settings.groq_whisper_model, "response_format": "text"},
                )
                response.raise_for_status()
                return response.text.strip()
    except Exception as e:
        logger.error(f"Inline transcription failed: {e}")
        return ""
```

- [ ] **Step 2: Update _on_voice to handle onboarding**

```python
async def _on_voice(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await self._check_access(update)
    if not user:
        return

    voice = update.message.voice or update.message.audio
    path = await self.download_media(voice)

    # Check onboarding
    flow = await self._get_onboarding_state(user["user_id"])
    if flow and not flow.is_complete:
        # Transcribe inline and use as answer
        transcript = await self._transcribe_voice_inline(path)
        if transcript:
            await self._handle_onboarding_step(update, user, flow, text=transcript)
        else:
            await update.message.reply_text("I couldn't hear that clearly. Could you try again or type your answer?")
        return

    if not user["onboarding_complete"]:
        return await self._start_onboarding(update, user)

    await self.send_typing(str(update.effective_chat.id))
    await self._route(update, ContentType.VOICE, "", media_path=path, user_capabilities=user["capabilities"])
```

- [ ] **Step 3: Update _on_photo, _on_document, _on_video for onboarding gate**

For each, add the access check and onboarding redirect:

```python
async def _on_photo(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await self._check_access(update)
    if not user:
        return
    flow = await self._get_onboarding_state(user["user_id"])
    if flow and not flow.is_complete:
        await update.message.reply_text("Let's finish getting to know each other first! You can send photos after.")
        return
    if not user["onboarding_complete"]:
        return await self._start_onboarding(update, user)
    await self.send_typing(str(update.effective_chat.id))
    path = await self.download_media(update.message.photo[-1])
    caption = update.message.caption or ""
    await self._route(update, ContentType.PHOTO, caption, media_path=path, user_capabilities=user["capabilities"])
```

Same pattern for `_on_document` and `_on_video`.

- [ ] **Step 4: Verify syntax**

Run: `python -m py_compile src/valentine/nexus/telegram.py && echo OK`

- [ ] **Step 5: Commit**

```bash
git add src/valentine/nexus/telegram.py
git commit -m "feat: add voice transcription during onboarding and media gating for all handlers"
```

---

### Task 13: Integration Tests for Access Control, Capability Enforcement, and Memory

**Files:**
- Create: `tests/test_access_control.py`
- Create: `tests/test_capabilities.py`

- [ ] **Step 1: Write access control tests**

Create `tests/test_access_control.py`:

```python
# tests/test_access_control.py
from __future__ import annotations
import pytest
from valentine.db import UserDB, ALL_CAPABILITIES, DEFAULT_CAPABILITIES

@pytest.fixture
def db(tmp_path):
    return UserDB(str(tmp_path / "test.db"))

def test_auto_approve_admin(db):
    """Admin should be auto-approved with all capabilities."""
    db.create_user("admin1", "admin", "Admin")
    user = db.approve_user("admin1", "admin", "system", ALL_CAPABILITIES)
    assert user["access_level"] == "admin"
    assert set(user["capabilities"]) == set(ALL_CAPABILITIES)

def test_pending_user_has_no_capabilities(db):
    user = db.create_user("user1", "test", "Test")
    assert user["access_level"] == "pending"
    assert user["capabilities"] == []
    assert db.has_capability("user1", "oracle") is False

def test_revoked_user_keeps_capabilities_but_blocked(db):
    db.create_user("user1", "test", "Test")
    db.approve_user("user1", "pro", "admin1", ["oracle", "codesmith"])
    db.revoke_user("user1", "admin1")
    user = db.get_user("user1")
    assert user["access_level"] == "revoked"

def test_approve_sets_default_capabilities(db):
    db.create_user("user1", "test", "Test")
    user = db.approve_user("user1", "basic", "admin1")
    assert user["capabilities"] == DEFAULT_CAPABILITIES["basic"]

def test_pro_has_no_shell_exec(db):
    db.create_user("user1", "test", "Test")
    user = db.approve_user("user1", "pro", "admin1")
    assert "shell_exec" not in user["capabilities"]
    assert "oracle" in user["capabilities"]
```

- [ ] **Step 2: Write capability enforcement tests**

Create `tests/test_capabilities.py`:

```python
# tests/test_capabilities.py
from __future__ import annotations
import pytest
from valentine.models import AgentName, IncomingMessage, ContentType

def test_incoming_message_serializes_capabilities():
    msg = IncomingMessage(
        message_id="1", chat_id="1", user_id="u1",
        platform="telegram", content_type=ContentType.TEXT,
        text="hello", user_capabilities=["oracle", "iris"],
    )
    d = msg.to_dict()
    assert d["user_capabilities"] == ["oracle", "iris"]
    restored = IncomingMessage.from_dict(d)
    assert restored.user_capabilities == ["oracle", "iris"]

def test_empty_capabilities_default():
    msg = IncomingMessage(
        message_id="1", chat_id="1", user_id="u1",
        platform="telegram", content_type=ContentType.TEXT,
        text="hello",
    )
    assert msg.user_capabilities == []
    d = msg.to_dict()
    assert d["user_capabilities"] == []
```

- [ ] **Step 3: Run tests**

Run: `PYTHONPATH=src python -m pytest tests/test_access_control.py tests/test_capabilities.py -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_access_control.py tests/test_capabilities.py
git commit -m "test: add access control and capability enforcement tests"
```

---

### Task 14: Add ADMIN_TELEGRAM_ID to .env + Final Integration

**Files:**
- Modify: `.env`

- [ ] **Step 1: Get admin Telegram ID**

The admin needs to find their Telegram user ID. They can message @userinfobot on Telegram, or we get it from the existing chat logs.

Add to `.env`:
```
ADMIN_TELEGRAM_ID=<the admin's telegram user id>
```

- [ ] **Step 2: Create data directory on VM**

```bash
ssh ubuntu@130.61.111.153 "mkdir -p /opt/valentine/data"
```

- [ ] **Step 3: Full syntax check all modified files**

```bash
python -m py_compile src/valentine/config.py
python -m py_compile src/valentine/models.py
python -m py_compile src/valentine/db.py
python -m py_compile src/valentine/nexus/onboarding.py
python -m py_compile src/valentine/nexus/telegram.py
python -m py_compile src/valentine/orchestrator/zeroclaw.py
python -m py_compile src/valentine/agents/base.py
python -m py_compile src/valentine/agents/cortex.py
python -m py_compile src/valentine/agents/oracle.py
python -m py_compile src/valentine/main.py
echo "All files OK"
```

- [ ] **Step 4: Run all tests**

```bash
PYTHONPATH=src python -m pytest tests/ -v
```

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "feat: complete onboarding, admin controls, and memory integration"
```

---

### Task 15: Deploy

- [ ] **Step 1: Push to GitHub**

```bash
git push origin main
```

- [ ] **Step 2: Pull on VM and restart**

```bash
ssh ubuntu@130.61.111.153 "cd /opt/valentine && git pull origin main && sudo systemctl restart valentine.service"
```

- [ ] **Step 3: Verify health**

```bash
ssh ubuntu@130.61.111.153 "sleep 5 && curl -s http://127.0.0.1:8080/health"
```

- [ ] **Step 4: Test on Telegram**

Send `/start` to the bot. Expected:
- Admin auto-approved
- Onboarding flow starts with inline buttons
- Can skip or complete
- Commands work (/help, /status, /users)
