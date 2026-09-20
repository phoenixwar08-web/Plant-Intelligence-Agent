from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

try:
    import requests
except ModuleNotFoundError:  # Offline fixture validation does not require an HTTP client.
    requests = None


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
        session: Any = None,
    ) -> None:
        self.config = config
        self.api_key = api_key
        self.session = session

    def _request_plan(self, system_prompt: str, model_input: Dict[str, Any]):
        """Read the runtime config once, converting any defect into a stable error code.

        These keys are only reached after the enabled and api-key gates pass, which is
        exactly when a malformed runtime config used to raise a bare KeyError out of the
        chain — no reason code, no audit record. Same fail-closed contract the Validator
        config already has.
        """
        try:
            base_url = self.config["base_url"]
            model = self.config["model"]
            if not isinstance(base_url, str) or not base_url.strip():
                raise TypeError("base_url must be a non-empty string")
            if not isinstance(model, str) or not model.strip():
                raise TypeError("model must be a non-empty string")
            endpoint = base_url.rstrip("/") + "/chat/completions"
            payload: Dict[str, Any] = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(model_input, ensure_ascii=False, separators=(",", ":"))},
                ],
                "temperature": float(self.config.get("temperature", 0)),
                "max_tokens": int(self.config.get("max_tokens", 1200)),
            }
            timeout = float(self.config.get("timeout_seconds", 30))
            retries = max(0, int(self.config.get("max_retries", 1)))
        except (KeyError, TypeError, ValueError, OverflowError):
            raise CloudStrategyError("invalid_provider_config", "provider configuration is malformed") from None

        if not math.isfinite(timeout) or timeout <= 0:
            raise CloudStrategyError("invalid_provider_config", "timeout_seconds must be a positive finite number")
        if not math.isfinite(payload["temperature"]):
            raise CloudStrategyError("invalid_provider_config", "temperature must be a finite number")
        if payload["max_tokens"] < 1:
            raise CloudStrategyError("invalid_provider_config", "max_tokens must be positive")
        if self.config.get("json_response_format", True):
            payload["response_format"] = {"type": "json_object"}
        return endpoint, payload, timeout, retries

    def complete(self, system_prompt: str, model_input: Dict[str, Any]) -> CloudResponse:
        if not self.config.get("enabled", False):
            raise CloudStrategyError("provider_disabled", "cloud strategy provider is disabled")
        if not self.api_key:
            raise CloudStrategyError("missing_api_key", "cloud strategy API key is not configured")

        endpoint, payload, timeout, retries = self._request_plan(system_prompt, model_input)
        if self.session is None:
            if requests is None:
                raise CloudStrategyError(
                    "http_client_unavailable",
                    "requests is required to use a real Cloud Strategy provider",
                )
            self.session = requests

        for attempt in range(retries + 1):
            try:
                response = self.session.post(
                    endpoint,
                    headers={
                        "Authorization": "Bearer " + self.api_key,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=timeout,
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
