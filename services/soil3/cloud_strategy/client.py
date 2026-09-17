from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests


class CloudStrategyError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class CloudResponse:
    content: str
    provider: str
    model: str
    request_id: Optional[str]
    usage: Dict[str, Any]


class OpenAICompatibleClient:
    def __init__(
        self,
        config: Dict[str, Any],
        api_key: str,
        session: Any = requests,
    ) -> None:
        self.config = config
        self.api_key = api_key
        self.session = session

    def complete(self, system_prompt: str, state: Dict[str, Any]) -> CloudResponse:
        if not self.config.get("enabled", False):
            raise CloudStrategyError("provider_disabled", "cloud strategy provider is disabled")
        if not self.api_key:
            raise CloudStrategyError("missing_api_key", "cloud strategy API key is not configured")

        endpoint = self.config["base_url"].rstrip("/") + "/chat/completions"
        payload: Dict[str, Any] = {
            "model": self.config["model"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(state, ensure_ascii=False, separators=(",", ":"))},
            ],
            "temperature": float(self.config.get("temperature", 0)),
            "max_tokens": int(self.config.get("max_tokens", 1200)),
        }
        if self.config.get("json_response_format", True):
            payload["response_format"] = {"type": "json_object"}

        retries = max(0, int(self.config.get("max_retries", 1)))
        for attempt in range(retries + 1):
            try:
                response = self.session.post(
                    endpoint,
                    headers={
                        "Authorization": "Bearer " + self.api_key,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=float(self.config.get("timeout_seconds", 30)),
                )
            except requests.Timeout as error:
                if attempt < retries:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                raise CloudStrategyError("model_timeout", str(error), retryable=True) from error
            except requests.RequestException as error:
                raise CloudStrategyError("model_transport_error", str(error), retryable=True) from error

            if response.status_code >= 500 and attempt < retries:
                time.sleep(0.2 * (attempt + 1))
                continue
            if response.status_code != 200:
                code = "model_http_error"
                if response.status_code == 401:
                    code = "model_auth_failed"
                elif response.status_code == 403:
                    code = "model_quota_or_permission_denied"
                elif response.status_code == 429:
                    code = "model_rate_limited"
                raise CloudStrategyError(
                    code,
                    f"model returned HTTP {response.status_code}",
                    retryable=response.status_code >= 500,
                )

            try:
                data = response.json()
                content = data["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError) as error:
                raise CloudStrategyError("invalid_model_envelope", "model response envelope is invalid") from error
            if not isinstance(content, str) or not content.strip():
                raise CloudStrategyError("empty_model_response", "model returned empty content")
            return CloudResponse(
                content=content,
                provider=str(self.config.get("provider") or "unknown"),
                model=str(data.get("model") or self.config["model"]),
                request_id=data.get("id"),
                usage=data.get("usage") or {},
            )
        raise CloudStrategyError("model_unavailable", "model request failed")
