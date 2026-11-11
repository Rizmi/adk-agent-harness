"""Context Compaction configuration for ADK agents."""

from typing import Optional
from google.adk.apps.compaction import EventsCompactionConfig
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer
from .config import config
from .providers.model import create_llm


def create_compaction_config(llm_model: Optional[str] = None) -> EventsCompactionConfig:
    """Create EventsCompactionConfig with an LLM summarizer."""
    # A summarizer model name must also be a catalog entry; no default is invented.
    spec = config.spec_for(llm_model)
    summarizer_llm = create_llm(spec)
    summarizer = None
    if hasattr(summarizer_llm, "generate_content_async") or hasattr(summarizer_llm, "acompletion"):
        try:
            summarizer = LlmEventSummarizer(llm=summarizer_llm)
        except Exception as e:
            print(f"[Compaction] Notice: LlmEventSummarizer: {e}")

    # Use context_limit directly from models.json (75% threshold)
    if not spec.context_limit or spec.context_limit <= 0:
        raise ValueError(
            f"Model '{spec.name}' must define a positive 'context_limit' in models.json for compaction."
        )

    token_threshold = int(spec.context_limit * 0.75)

    return EventsCompactionConfig(
        summarizer=summarizer,
        token_threshold=token_threshold,
        event_retention_size=config.compaction_retention_size,
    )
