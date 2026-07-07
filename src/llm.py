"""Provider-agnostic LLM layer (REQUIREMENT A).

The entire app calls one function: ``complete(role, system, user, json_mode)``.
Model selection is pure config (CLASSIFIER_MODEL / DRAFTER_MODEL in .env),
routed through LiteLLM so one code path covers Anthropic, OpenAI, Gemini,
Groq, Together, OpenRouter, and local models via Ollama / vLLM / any
OpenAI-compatible endpoint (configurable via LLM_BASE_URL).

Verified against official LiteLLM docs (Jul 2026):
- Signature: ``litellm.completion(model=..., messages=[...], response_format=...)``
- Result:    ``response.choices[0].message.content``
- JSON mode: pass ``response_format={"type": "json_object"}``. Anthropic,
  OpenAI, Gemini 2.0+, Groq, Ollama, and Bedrock all support this; LiteLLM
  maps it to each provider's native structured-output mechanism.
- We additionally force ``temperature=0`` for determinism.
- The Pydantic validate-retry-failsafe in agents/classifier.py is the
  cross-provider safety net, since open-source models are the least reliable
  at strict JSON.
"""

from __future__ import annotations

import logging
from typing import Optional

import litellm

from .config import Config

log = logging.getLogger(__name__)

# LiteLLM can be chatty; keep our logs clean.
litellm.suppress_debug_info = True

Role = str  # "classifier" | "drafter"


class LLMError(Exception):
    """Raised when the LLM call fails after retries."""


def _model_for_role(config: Config, role: Role) -> str:
    if role == "classifier":
        return config.classifier_model
    if role == "drafter":
        return config.drafter_model
    raise LLMError(f"Unknown LLM role: {role!r}")


def complete(
    config: Config,
    role: Role,
    system: str,
    user: str,
    json_mode: bool = False,
    max_tokens: Optional[int] = None,
) -> str:
    """Run a completion and return the assistant text.

    Args:
        config: validated Config.
        role: "classifier" or "drafter" — selects the model.
        system: system prompt.
        user: user prompt.
        json_mode: if True, request JSON output via response_format.
        max_tokens: override default token budget.

    Raises:
        LLMError: on any failure (network, auth, empty content).
    """
    model = _model_for_role(config, role)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    kwargs: dict = {
        "model": model,
        "messages": messages,
        "temperature": 0.0,
        "timeout": config.llm_timeout,
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

    # Optional OpenAI-compatible / local endpoint. LiteLLM auto-detects
    # ollama/ prefix, but an explicit base_url covers LM Studio / vLLM /
    # text-generation-webui when the model string is provider-prefixed.
    if config.llm_base_url:
        kwargs["api_base"] = config.llm_base_url

    if json_mode:
        # Native JSON object mode. For models that support json_schema
        # natively (Anthropic 4.5+, OpenAI, Gemini 2.0+, Groq, Ollama),
        # LiteLLM forwards appropriately. The classifier also pins the
        # schema in the system prompt so even non-native models are guided.
        kwargs["response_format"] = {"type": "json_object"}

    try:
        response = litellm.completion(**kwargs)
    except Exception as exc:
        # LiteLLM raises typed exceptions; we collapse to one for the caller.
        raise LLMError(f"LiteLLM completion failed for role={role} model={model}: {exc}") from exc

    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as exc:
        raise LLMError(f"Unexpected LiteLLM response shape for role={role}: {exc}") from exc

    if content is None:
        raise LLMError(f"LLM returned empty content for role={role} model={model}")
    return content
