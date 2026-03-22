# src/valentine/security.py
"""
Valentine Security Module
=========================
Runtime input validation, output sanitisation, and sensitive-info leak
prevention. Works at the adapter layer (before messages reach agents)
and at the output layer (before responses reach users).
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Input limits
# ---------------------------------------------------------------------------
MAX_MESSAGE_LENGTH = 8000  # chars — longer messages are truncated
MAX_MEDIA_SIZE_MB = 20     # reject files bigger than this
ALLOWED_MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",  # images
    ".ogg", ".mp3", ".wav", ".m4a", ".opus",            # audio
    ".pdf", ".txt", ".csv", ".json", ".md",             # documents
    ".mp4", ".webm",                                     # video
}

# ---------------------------------------------------------------------------
# Prompt injection detection patterns
# ---------------------------------------------------------------------------
_INJECTION_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"ignore\s+(all\s+)?previous\s+instructions",
        r"ignore\s+(all\s+)?above\s+instructions",
        r"ignore\s+(all\s+)?prior\s+instructions",
        r"forget\s+(all\s+)?your\s+(instructions|rules|guidelines)",
        r"forget\s+everything",
        r"you\s+are\s+now\s+(DAN|jailbroken|unrestricted|unfiltered)",
        r"new\s+system\s+prompt",
        r"override\s+(system|safety)\s+prompt",
        r"disable\s+(safety|content)\s+filter",
        r"act\s+as\s+(if\s+)?you\s+have\s+no\s+restrictions",
        r"pretend\s+(that\s+)?your?\s+(guidelines|rules|restrictions)\s+(don.t|do\s+not)\s+exist",
        r"\[INST\]",
        r"###\s*(system|instruction|new\s+prompt)",
        r"<\|system\|>",
        r"<\|im_start\|>",
        r"</s><s>",
        r"ASSISTANT:\s*",
        r"SYSTEM:\s*(override|new|ignore)",
    ]
]


def detect_injection(text: str) -> bool:
    """Return True if the text contains likely prompt injection attempts."""
    for pat in _INJECTION_PATTERNS:
        if pat.search(text):
            logger.warning("Prompt injection pattern detected: %s", pat.pattern)
            return True
    return False


# ---------------------------------------------------------------------------
# Sensitive info patterns — things that must never appear in output
# ---------------------------------------------------------------------------
_SENSITIVE_OUTPUT_PATTERNS: list[tuple[re.Pattern, str]] = [
    # API keys (generic long hex/base64 strings that look like keys)
    (re.compile(r"(gsk_|sk-ant-|sk-)[A-Za-z0-9_\-]{20,}"), "[REDACTED_API_KEY]"),
    # Telegram bot tokens: 123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
    (re.compile(r"\d{8,10}:[A-Za-z0-9_\-]{30,}"), "[REDACTED_BOT_TOKEN]"),
    # Generic long secrets (env var assignments)
    (re.compile(r"(?:API_KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)\s*=\s*\S{10,}"), "[REDACTED_SECRET]"),
    # Redis URLs with passwords: redis://:password@host:port
    (re.compile(r"redis://:[^@]+@"), "redis://:[REDACTED]@"),
    # SSH private key headers
    (re.compile(r"-----BEGIN\s+(RSA |OPENSSH )?PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
]


def sanitise_output(text: str) -> str:
    """Scrub any accidentally leaked secrets from agent output."""
    for pattern, replacement in _SENSITIVE_OUTPUT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# ---------------------------------------------------------------------------
# Input sanitisation
# ---------------------------------------------------------------------------
def sanitise_input(text: str) -> str:
    """Clean and bound user input before it reaches an agent.

    - Truncates to MAX_MESSAGE_LENGTH
    - Strips null bytes and control characters (except newline/tab)
    - Does NOT alter the semantic content
    """
    if not text:
        return text
    # Strip null bytes and non-printable control chars (keep \n \t)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    # Truncate
    if len(text) > MAX_MESSAGE_LENGTH:
        text = text[:MAX_MESSAGE_LENGTH] + "\n[Message truncated — too long]"
    return text


def validate_media_extension(filename: str) -> bool:
    """Return True if the file extension is in the allow-list."""
    if not filename:
        return False
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in ALLOWED_MEDIA_EXTENSIONS


# ---------------------------------------------------------------------------
# Self-awareness query detection
# ---------------------------------------------------------------------------
_SELF_AWARENESS_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"what\s+can\s+you\s+do",
        r"what\s+are\s+your\s+capabilities",
        r"what\s+are\s+you\s+capable\s+of",
        r"tell\s+me\s+about\s+yourself",
        r"who\s+are\s+you",
        r"who\s+(made|built|created)\s+you",
        r"what\s+is\s+valentine",
        r"what\s+are\s+your\s+(features|abilities|skills|functions)",
        r"help\s+me\s+understand\s+what\s+you\s+do",
        r"what\s+do\s+you\s+know\s+about\s+yourself",
    ]
]


def is_self_awareness_query(text: str) -> bool:
    """Return True if the user is asking Valentine about itself."""
    return any(pat.search(text) for pat in _SELF_AWARENESS_PATTERNS)
