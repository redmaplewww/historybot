from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def _extract_json_object(text: str) -> str:
    """Strip prose or markdown code fences that gateways sometimes add."""
    trimmed = text.strip()
    if trimmed.startswith("```"):
        trimmed = re.sub(r"^```(?:json)?\s*", "", trimmed, flags=re.IGNORECASE)
        trimmed = re.sub(r"\s*```$", "", trimmed)
    if trimmed.lstrip().startswith("{"):
        return trimmed
    start = trimmed.find("{")
    end = trimmed.rfind("}")
    if start >= 0 and end > start:
        return trimmed[start : end + 1]
    return trimmed


@dataclass(frozen=True)
class ModelResult:
    value: Any
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    raw: dict[str, Any]


async def _post_with_retry(client: httpx.AsyncClient, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
    """Retry transient gateway failures without duplicating a completed request."""
    attempts = int(os.getenv("HISTORYBOT_HTTP_RETRIES", "3"))
    delay = 2.0
    for attempt in range(1, attempts + 1):
        try:
            response = await client.post(url, headers=headers, json=json)
            if response.status_code < 500:
                return response
            if attempt == attempts:
                response.raise_for_status()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError) as exc:
            if attempt == attempts:
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
    raise RuntimeError("unreachable retry state")


def _raise_for_status(response: httpx.Response, *, model: str, task: str) -> None:
    """Raise with the gateway's own error text attached.

    A bare 400/403 from an OpenAI-compatible gateway says nothing about which
    request was rejected or why. The body usually names the offending field, so
    it is carried into the exception instead of being discarded.
    """
    if response.status_code < 400:
        return
    try:
        detail = response.text[:800]
    except Exception:  # noqa: BLE001 - diagnostics must never mask the error
        detail = "<unreadable body>"
    raise httpx.HTTPStatusError(
        f"{response.status_code} from {response.request.url} "
        f"for task={task!r} model={model!r}: {detail}",
        request=response.request,
        response=response,
    )


class ModelGateway:
    """Provider-neutral gateway. No graph node may call a vendor SDK directly."""

    async def structured(self, *, system: str, user: str, schema: type[T], task: str) -> ModelResult:
        provider = os.getenv("HISTORYBOT_LLM_PROVIDER", "anthropic")
        if provider == "anthropic":
            return await self._anthropic(system=system, user=user, schema=schema, task=task)
        if provider == "openai_compatible":
            return await self._openai_compatible(system=system, user=user, schema=schema, task=task)
        raise ValueError(f"Unsupported HISTORYBOT_LLM_PROVIDER={provider!r}")

    async def _anthropic(self, *, system: str, user: str, schema: type[T], task: str) -> ModelResult:
        token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
        base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        model = os.environ.get("HISTORYBOT_MODEL", "claude-3-5-sonnet-latest")
        if not token:
            raise RuntimeError("ANTHROPIC_AUTH_TOKEN is required for a live model run")
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        prompt = (
            f"任务标识: {task}\n"
            "只返回符合 JSON Schema 的 JSON，不要 markdown，不要补充不存在的事实。\n"
            f"JSON Schema:\n{schema_json}\n\n{user}"
        )
        headers = {"x-api-key": token, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        max_tokens = int(os.getenv("HISTORYBOT_MAX_TOKENS", "2048"))
        timeout = float(os.getenv("HISTORYBOT_HTTP_TIMEOUT", "300"))
        body = {"model": model, "max_tokens": max_tokens, "temperature": 0, "system": system, "messages": [{"role": "user", "content": prompt}]}
        async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
            response = await _post_with_retry(client, "/v1/messages", json=body, headers=headers)
            _raise_for_status(response, model=model, task=task)
            raw = response.json()
        text = _extract_json_object("".join(block.get("text", "") for block in raw.get("content", []) if block.get("type") == "text"))
        try:
            value = schema.model_validate_json(text)
        except Exception as exc:
            raise ValueError(f"Model returned invalid {schema.__name__}: {exc}; raw={text[:500]}") from exc
        usage = raw.get("usage", {})
        if not usage and raw.get("usage_json"):
            usage = raw["usage_json"]
        return ModelResult(value, "anthropic", model, usage.get("input_tokens"), usage.get("output_tokens"), raw)

    async def _openai_compatible(self, *, system: str, user: str, schema: type[T], task: str) -> ModelResult:
        token = os.environ.get("HISTORYBOT_API_KEY") or os.environ.get("OPENAI_API_KEY")
        base_url = os.environ.get("HISTORYBOT_BASE_URL", "https://api.openai.com")
        model = os.environ.get("HISTORYBOT_MODEL", "gpt-4o-mini")
        if not token:
            raise RuntimeError("HISTORYBOT_API_KEY or OPENAI_API_KEY is required for a live model run")
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        prompt = f"任务标识: {task}\n只返回符合 JSON Schema 的 JSON。\nJSON Schema:\n{schema_json}\n\n{user}"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        max_tokens = int(os.getenv("HISTORYBOT_MAX_TOKENS", "2048"))
        timeout = float(os.getenv("HISTORYBOT_HTTP_TIMEOUT", "300"))
        body = {"model": model, "temperature": 0, "max_tokens": max_tokens, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}], "response_format": {"type": "json_object"}}
        reasoning_effort = os.getenv("HISTORYBOT_REASONING_EFFORT")
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
            response = await _post_with_retry(client, "/v1/chat/completions", json=body, headers=headers)
            _raise_for_status(response, model=model, task=task)
            raw = response.json()
        text = _extract_json_object(raw["choices"][0]["message"]["content"])
        try:
            value = schema.model_validate_json(text)
        except ValueError:
            value = schema.model_validate(json.loads(text, strict=False))
        usage = raw.get("usage", {})
        if not usage and raw.get("usage_json"):
            usage = raw["usage_json"]
        return ModelResult(value, "openai_compatible", model, usage.get("prompt_tokens"), usage.get("completion_tokens"), raw)
