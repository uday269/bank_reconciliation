"""Optional generative AI adapter (DD-10, CR-13, FR-GAI-01..04)."""

from app.infra.genai.base import (
    GenAIAdapter, ProseRequest, ProseResult, build_adapter, load_env_file,
)

__all__ = ["GenAIAdapter", "ProseRequest", "ProseResult", "build_adapter", "load_env_file"]
