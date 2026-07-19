from __future__ import annotations

import argparse
import os
import sys
from typing import Dict

SUPPORTED_LLM_PROVIDERS = ("ollama", "gpt-oss", "openai")


def bounded_temperature(raw_value: str) -> float:
    value = float(raw_value)
    if value < 0 or value > 2:
        raise argparse.ArgumentTypeError("temperature must be between 0 and 2")
    return value


def add_llm_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--llm-provider",
        choices=SUPPORTED_LLM_PROVIDERS,
        help="LLM provider/backend. 'openai' uses OPENAI_API_KEY.",
    )
    parser.add_argument(
        "--llm-model",
        help="LLM model name, for example gpt-oss:20b or gpt-4.1.",
    )
    parser.add_argument("--llm-temperature", type=bounded_temperature, help="LLM temperature.")
    parser.add_argument(
        "--llm-timeout",
        type=_nonnegative_int,
        help="LLM request timeout in seconds. Set 0 to disable request timeout.",
    )
    parser.add_argument(
        "--llm-max-tokens",
        type=_nonnegative_int,
        help="Maximum output tokens per LLM response. Set 0 to use the provider default.",
    )
    parser.add_argument(
        "--llm-api-url",
        help=(
            "OpenAI-compatible chat completions URL, for example "
            "http://abc.net:30001/chat/completions."
        ),
    )
    parser.add_argument(
        "--llm-api-key",
        help="API key for OpenAI or an OpenAI-compatible endpoint.",
    )


def _nonnegative_int(raw_value: str) -> int:
    value = int(raw_value)
    if value < 0:
        raise argparse.ArgumentTypeError("value must be 0 or greater")
    return value


def normalize_chat_completions_url(url: str | None) -> str:
    if not url:
        return ""
    normalized = url.rstrip("/")
    suffix = "/chat/completions"
    if normalized.endswith(suffix):
        normalized = normalized[: -len(suffix)]
    return normalized


def resolve_llm_settings(
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
    timeout_seconds: int | None = None,
    max_tokens: int | None = None,
) -> Dict[str, object]:
    resolved_provider = (provider or os.getenv("LLM_PROVIDER") or "ollama").strip().lower()
    if resolved_provider not in SUPPORTED_LLM_PROVIDERS:
        choices = ", ".join(SUPPORTED_LLM_PROVIDERS)
        raise ValueError(f"Unsupported LLM provider. Use {choices}.")
    resolved_temperature = (
        temperature if temperature is not None else float(os.getenv("LLM_TEMPERATURE", "0.1"))
    )
    if resolved_provider == "openai":
        resolved_api_url = api_url or os.getenv("OPENAI_API_URL") or os.getenv("LLM_API_URL") or ""
        resolved_api_key = api_key or os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY") or ""
    elif resolved_provider == "gpt-oss":
        resolved_api_url = api_url or os.getenv("GPT_OSS_API_URL") or os.getenv("LLM_API_URL") or ""
        resolved_api_key = api_key or os.getenv("GPT_OSS_API_KEY") or os.getenv("LLM_API_KEY") or ""
    else:
        resolved_api_url = api_url or os.getenv("LLM_API_URL") or ""
        resolved_api_key = api_key or os.getenv("LLM_API_KEY") or ""
    resolved_timeout_seconds = (
        timeout_seconds
        if timeout_seconds is not None
        else int(os.getenv("LLM_TIMEOUT_SECONDS", "180"))
    )
    resolved_max_tokens = (
        max_tokens if max_tokens is not None else int(os.getenv("LLM_MAX_TOKENS", "8192"))
    )

    if resolved_provider == "gpt-oss":
        resolved_model = model or os.getenv("GPT_OSS_MODEL") or os.getenv("LLM_MODEL") or "gpt-oss"
        backend = "openai-compatible" if resolved_api_url else "ollama"
    elif resolved_provider == "openai":
        resolved_model = model or os.getenv("OPENAI_MODEL") or os.getenv("LLM_MODEL") or "gpt-4.1"
        backend = "openai"
    else:
        resolved_model = model or os.getenv("LLM_MODEL") or os.getenv("OLLAMA_MODEL") or "gpt-oss:20b"
        backend = resolved_provider

    if backend in {"openai", "openai-compatible"}:
        base_url = normalize_chat_completions_url(resolved_api_url)
    elif backend == "ollama":
        base_url = os.getenv("OLLAMA_BASE_URL", "")
    else:
        base_url = resolved_api_url

    return {
        "provider": resolved_provider,
        "backend": backend,
        "model": resolved_model,
        "temperature": resolved_temperature,
        "api_url": resolved_api_url,
        "base_url": base_url,
        "api_key": resolved_api_key,
        "timeout_seconds": resolved_timeout_seconds,
        "max_tokens": resolved_max_tokens,
    }


def public_llm_config(settings: Dict[str, object]) -> Dict[str, object]:
    api_key = str(settings.get("api_key") or "")
    redacted_key = ""
    if api_key:
        redacted_key = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else "***"
    return {
        "provider": settings.get("provider", ""),
        "backend": settings.get("backend", ""),
        "model": settings.get("model", ""),
        "temperature": settings.get("temperature", ""),
        "api_url": settings.get("api_url", ""),
        "base_url": settings.get("base_url", ""),
        "api_key_set": bool(api_key),
        "api_key_redacted": redacted_key,
        "timeout_seconds": settings.get("timeout_seconds", ""),
        "max_tokens": settings.get("max_tokens", ""),
    }


def create_llm(
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
    timeout_seconds: int | None = None,
    max_tokens: int | None = None,
):
    settings = resolve_llm_settings(
        provider, model, temperature, api_url, api_key, timeout_seconds, max_tokens
    )
    backend = str(settings["backend"])
    model_name = str(settings["model"])
    resolved_temperature = float(settings["temperature"])
    resolved_timeout = int(settings.get("timeout_seconds") or 0)
    resolved_max_tokens = int(settings.get("max_tokens") or 0)

    if backend in {"openai", "openai-compatible"}:
        try:
            from langchain_openai import ChatOpenAI
        except ModuleNotFoundError as exc:
            print(f"Missing dependency: {exc.name}")
            print("Install project dependencies with: python3 -m pip install -r requirements.txt")
            sys.exit(1)
        if backend == "openai" and not settings["api_key"]:
            raise ValueError("OpenAI provider requires OPENAI_API_KEY or --llm-api-key.")
        kwargs = {
            "model": model_name,
            "temperature": resolved_temperature,
            "api_key": str(settings["api_key"] or "dummy"),
        }
        if resolved_timeout > 0:
            kwargs["timeout"] = resolved_timeout
        if resolved_max_tokens > 0:
            kwargs["max_tokens"] = resolved_max_tokens
        if settings["base_url"]:
            kwargs["base_url"] = str(settings["base_url"])
        return ChatOpenAI(**kwargs)

    if backend != "ollama":
        raise ValueError(
            "Unsupported LLM backend. Supported backends: ollama, openai, openai-compatible"
        )

    try:
        from langchain_ollama.chat_models import ChatOllama
    except ModuleNotFoundError as exc:
        print(f"Missing dependency: {exc.name}")
        print("Install project dependencies with: python3 -m pip install -r requirements.txt")
        sys.exit(1)
    kwargs = {"model": model_name, "temperature": resolved_temperature}
    if resolved_max_tokens > 0:
        kwargs["num_predict"] = resolved_max_tokens
    if resolved_timeout > 0:
        kwargs["client_kwargs"] = {"timeout": resolved_timeout}
        kwargs["sync_client_kwargs"] = {"timeout": resolved_timeout}
    if settings["base_url"]:
        kwargs["base_url"] = str(settings["base_url"])
    return ChatOllama(**kwargs)


def llm_config(
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
    timeout_seconds: int | None = None,
    max_tokens: int | None = None,
):
    return public_llm_config(
        resolve_llm_settings(
            provider, model, temperature, api_url, api_key, timeout_seconds, max_tokens
        )
    )
