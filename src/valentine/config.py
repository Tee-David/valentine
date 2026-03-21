# src/valentine/config.py
from __future__ import annotations

from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # API Keys
    groq_api_key: str = Field(default="")
    cerebras_api_key: str = Field(default="")
    sambanova_api_key: str = Field(default="")
    telegram_bot_token: str = Field(default="")

    # Redis
    redis_url: str = Field(default="redis://localhost:6379/0")

    # Qdrant
    qdrant_host: str = Field(default="localhost")
    qdrant_port: int = Field(default=6333)

    # Model mappings per provider
    groq_base_url: str = "https://api.groq.com/openai/v1"
    cerebras_base_url: str = "https://api.cerebras.ai/v1"
    sambanova_base_url: str = "https://api.sambanova.ai/v1"

    # Default models per provider
    groq_default_model: str = "llama-3.1-8b-instant"
    groq_reasoning_model: str = "qwen-qwq-32b"
    groq_whisper_model: str = "whisper-large-v3-turbo"
    cerebras_default_model: str = "qwen-3-32b"
    sambanova_default_model: str = "QwQ-32B"
    sambanova_vision_model: str = "Qwen2.5-VL-72B"

    # Agent config
    workspace_dir: str = Field(default="/tmp/valentine/workspace")
    skills_dir: str = Field(default="/opt/valentine/skills")
    skills_builtin_dir: str = Field(default="/opt/valentine/scripts/skills-builtin")
    max_shell_timeout: int = Field(default=30)
    allowed_shell_dirs: list[str] = Field(default_factory=lambda: ["/tmp/valentine/workspace"])

    # Rate limits (requests per minute)
    groq_rpm: int = 30
    cerebras_rpm: int = 30
    sambanova_rpm: int = 20

    # Rate limits (requests per day)
    groq_rpd: int = 14400
    cerebras_rpd: int = 1000
    sambanova_rpd: int = 10000

    # Logging
    log_level: str = Field(default="INFO")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
