from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings
from .db import Database
from .utils import extract_json_object


class LLMError(RuntimeError):
    pass


@dataclass(slots=True)
class Completion:
    text: str
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class LLMRouter:
    def __init__(self, settings: Settings, db: Database, run_id: int | None = None):
        self.settings = settings
        self.db = db
        self.run_id = run_id
        self.calls = 0
        self._budget_lock = asyncio.Lock()
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(90, connect=20), follow_redirects=True)

    async def close(self) -> None:
        await self.client.aclose()

    def available(self) -> bool:
        return (
            self.settings.llm_enabled
            and bool(self.settings.configured_providers())
            and self.calls < self.settings.llm_max_calls
        )

    async def _reserve_call(self) -> bool:
        async with self._budget_lock:
            if self.calls >= self.settings.llm_max_calls:
                return False
            self.calls += 1
            return True

    async def _post_with_retry(self, url: str, **kwargs: Any) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(4):
            try:
                response = await self.client.post(url, **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    await asyncio.sleep(min(16, 2 ** attempt + 0.5))
                    continue
                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                last = exc
                if attempt < 3:
                    await asyncio.sleep(min(16, 2 ** attempt + 0.5))
        raise LLMError(str(last or "LLM request failed"))

    async def _groq(self, prompt: str, model: str) -> Completion:
        response = await self._post_with_retry(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.settings.groq_api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "You are a precise public-records regulatory analyst. Return only valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.0,
                "response_format": {"type": "json_object"},
            },
        )
        data = response.json()
        usage = data.get("usage", {})
        return Completion(
            text=data["choices"][0]["message"]["content"], provider="groq", model=model,
            input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
        )

    async def _gemini(self, prompt: str, model: str) -> Completion:
        response = await self._post_with_retry(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": str(self.settings.gemini_api_key),
            },
            json={
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0,
                    "responseMimeType": "application/json",
                },
            },
        )
        data = response.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        usage = data.get("usageMetadata", {})
        return Completion(
            text=text, provider="gemini", model=model,
            input_tokens=usage.get("promptTokenCount"), output_tokens=usage.get("candidatesTokenCount"),
        )

    async def _openrouter(self, prompt: str, model: str) -> Completion:
        response = await self._post_with_retry(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.settings.openrouter_api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/",
                "X-Title": "Texas County Regulatory Radar",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "Return only valid JSON grounded in the supplied public-record text."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
        )
        data = response.json()
        usage = data.get("usage", {})
        return Completion(
            text=data["choices"][0]["message"]["content"], provider="openrouter", model=model,
            input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
        )

    async def complete(self, prompt: str, *, purpose: str, verify: bool = False) -> Completion:
        if not self.available():
            raise LLMError("No configured LLM provider or per-run call budget exhausted")
        errors: list[str] = []
        for provider in self.settings.llm_provider_order:
            if self.calls >= self.settings.llm_max_calls:
                break
            key = {
                "groq": self.settings.groq_api_key,
                "gemini": self.settings.gemini_api_key,
                "openrouter": self.settings.openrouter_api_key,
            }.get(provider)
            if not key:
                continue
            model = {
                "groq": self.settings.groq_verify_model if verify else self.settings.groq_model,
                "gemini": self.settings.gemini_verify_model if verify else self.settings.gemini_model,
                "openrouter": self.settings.openrouter_model,
            }[provider]
            if not await self._reserve_call():
                break
            try:
                completion = await getattr(self, f"_{provider}")(prompt, model)
                # Parse now so malformed JSON falls through to the next provider.
                extract_json_object(completion.text)
                self.db.add_llm_usage(
                    self.run_id, provider, model, purpose, len(prompt), len(completion.text), "ok",
                    completion.input_tokens, completion.output_tokens,
                )
                return completion
            except Exception as exc:
                errors.append(f"{provider}/{model}: {exc}")
                self.db.add_llm_usage(
                    self.run_id, provider, model, purpose, len(prompt), 0, "error", error=str(exc)[:1000],
                )
        raise LLMError("; ".join(errors) or "No configured provider accepted the request")
