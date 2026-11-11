"""Pluggable LLM factory.

Every model is described by a ``ModelSpec`` resolved from ``models.json``; this
module only turns that description into an ADK-compatible model object. It never
reads .env directly and never substitutes a default model.
"""

from typing import Union

from google.adk.models.google_llm import Gemini
from google.adk.models.lite_llm import LiteLlm

from ..models import ModelSpec


def create_llm(spec: ModelSpec) -> Union[Gemini, LiteLlm]:
    """Build the ADK model object for a resolved catalog entry."""
    model_name = (spec.model or "").strip()
    if not model_name:
        raise ValueError(f"Model '{spec.name}' has an empty model id in models.json.")

    # Native Gemini
    if spec.transport == "gemini-rest" or (
        (model_name.startswith("gemini-") or model_name.startswith("models/gemini-")) and not spec.base_url
    ):
        if not spec.api_key:
            raise ValueError(f"Model '{spec.name}' has no api_key for the Gemini transport.")
        base_url = spec.base_url or None
        return Gemini(
            model=model_name,
            base_url=base_url,
            client_kwargs={"api_key": spec.api_key} if spec.api_key else None,
        )

    # OpenAI-compatible / LiteLLM provider.
    known_prefixes = (
        "openai/",
        "gemini/",
        "anthropic/",
        "groq/",
        "ollama/",
        "azure/",
        "bedrock/",
        "together_ai/",
        "vertex_ai/",
        "mistral/",
        "deepseek/",
        "fireworks_ai/",
        "cohere/",
    )
    if not any(model_name.startswith(p) for p in known_prefixes):
        model_name = f"openai/{model_name}"

    kwargs = {}
    if spec.temperature is not None:
        kwargs["temperature"] = spec.temperature

    return LiteLlm(
        model=model_name,
        api_base=spec.base_url,
        api_key=spec.api_key,
        **kwargs,
    )
