from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCapabilities:
    provider: str
    model: str
    context_window: int
    default_output_tokens: int
    maximum_output_tokens: int
    cloud: bool
    supports_stream: bool = True
    supports_structured_output: bool = False
    supports_retry_after: bool = True
    tokenizer_kind: str = "conservative"
    tokenizer_path: str = ""


class ModelCapabilitiesRegistry:
    def __init__(self, settings):
        self.settings = settings

    def for_model(self, provider: str, model: str) -> ModelCapabilities:
        normalized = str(provider or "").strip().lower()
        default_context = max(
            1024,
            int(getattr(self.settings, "model_context_window_default", 8192)),
        )
        default_output = max(1, int(getattr(self.settings, "ai_max_tokens", 512)))
        if normalized == "ollama":
            return ModelCapabilities(
                provider=normalized,
                model=model,
                context_window=max(
                    1024,
                    int(getattr(self.settings, "model_ollama_context_window", default_context)),
                ),
                default_output_tokens=default_output,
                maximum_output_tokens=max(
                    default_output,
                    int(getattr(self.settings, "model_ollama_max_output_tokens", default_output)),
                ),
                cloud=False,
                tokenizer_kind=str(
                    getattr(self.settings, "model_ollama_tokenizer_kind", "conservative")
                ),
                tokenizer_path=str(
                    getattr(self.settings, "model_ollama_tokenizer_path", "")
                ),
            )
        if normalized == "openai":
            return ModelCapabilities(
                provider=normalized,
                model=model,
                context_window=max(
                    1024,
                    int(getattr(self.settings, "model_openai_context_window", default_context)),
                ),
                default_output_tokens=default_output,
                maximum_output_tokens=max(
                    default_output,
                    int(getattr(self.settings, "model_openai_max_output_tokens", default_output)),
                ),
                cloud=True,
                supports_structured_output=True,
                tokenizer_kind=str(
                    getattr(self.settings, "model_openai_tokenizer_kind", "tiktoken")
                ),
            )
        return ModelCapabilities(
            provider=normalized,
            model=model,
            context_window=default_context,
            default_output_tokens=default_output,
            maximum_output_tokens=default_output,
            cloud=False,
        )

