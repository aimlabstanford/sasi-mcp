"""YAML-backed config for sasi-mcp.

Lives at ~/.sasi-mcp/config.yaml by default. Loader pattern follows
ledger_bridge.config but the schema is fresh — no Drive/Ledger/Gmail bits.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

DEFAULT_DATA_DIR = Path.home() / ".sasi-mcp"
DEFAULT_CONFIG_PATH = DEFAULT_DATA_DIR / "config.yaml"


class OutlookConfig(BaseModel):
    mailbox_email: str = "summermed@stanford.edu"
    folders: list[str] = Field(default_factory=lambda: ["inbox", "sent"])
    days_back: int = 365 * 8  # default backfill: 8 years
    cache_first: bool = False  # cache reader can't extract bodies; disabled in v1


class RedactionConfig(BaseModel):
    presidio_entities: list[str] = Field(
        default_factory=lambda: [
            "PERSON",
            "EMAIL_ADDRESS",
            "PHONE_NUMBER",
            "US_SSN",
            "LOCATION",
            "CREDIT_CARD",
            "URL",
            "IP_ADDRESS",
        ]
    )
    keep_year_in_dates: bool = True


class LLMConfig(BaseModel):
    provider: str = "ollama"  # "ollama" | "anthropic"
    model: str = "llama3.1:8b"
    ollama_url: str = "http://127.0.0.1:11434"
    anthropic_model: str = "claude-haiku-4-5"


class EmbeddingConfig(BaseModel):
    model: str = "sentence-transformers/all-mpnet-base-v2"
    dim: int = 768


class StalenessConfig(BaseModel):
    contradiction_threshold: float = 0.6
    evergreen_threshold: float = 0.85
    similarity_floor: float = 0.55  # probe-set coverage gap threshold


class RetrievalConfig(BaseModel):
    default_top_k: int = 5
    recency_half_life_years: float = 3.0  # 0.7 + 0.3*exp(-age/half_life)
    evergreen_boost: float = 0.15  # final = base * (1 + 0.15 * evergreen_score)


class McpConfig(BaseModel):
    transport: str = "stdio"
    name: str = "sasi-faq"


class Config(BaseModel):
    data_dir: Path = DEFAULT_DATA_DIR
    log_level: str = "INFO"
    outlook: OutlookConfig = Field(default_factory=OutlookConfig)
    redaction: RedactionConfig = Field(default_factory=RedactionConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    staleness: StalenessConfig = Field(default_factory=StalenessConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "corpus.sqlite"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def redaction_audit_path(self) -> Path:
        return self.data_dir / "redaction_audit.jsonl"

    @property
    def policy_snapshot_path(self) -> Path:
        return self.data_dir / "policy_snapshot.yaml"

    @property
    def probe_questions_path(self) -> Path:
        return self.data_dir / "probe_questions.txt"


def load_config(path: Path | None = None) -> Config:
    config_path = path or DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return Config()
    with config_path.open() as f:
        raw = yaml.safe_load(f) or {}
    return Config.model_validate(raw)
