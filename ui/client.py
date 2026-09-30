"""HTTP client for the ColdTrace API. The UI talks only to the API — it never holds
database credentials (docs/DESIGN.md §5.2)."""

from __future__ import annotations

from typing import Any

import httpx

from src.config import get_settings

# A local 7B model on CPU takes minutes per question; don't cut it off.
QUERY_TIMEOUT_S = 600.0
DEFAULT_TIMEOUT_S = 30.0


class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}" if status else detail)
        self.status = status
        self.detail = detail


class ColdTraceClient:
    def __init__(self, base_url: str, *, http: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.Client(base_url=self.base_url, timeout=DEFAULT_TIMEOUT_S)

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def query(self, question: str, session_id: str | None = None) -> dict[str, Any]:
        body = {"question": question, "session_id": session_id}
        return self._request("POST", "/query", json=body, timeout=QUERY_TIMEOUT_S)

    def decide(self, log_id: int, decision: str, reason: str | None = None) -> dict[str, Any]:
        body = {"log_id": log_id, "decision": decision, "reason": reason}
        return self._request("POST", "/decision", json=body)

    def audit(
        self,
        *,
        session_id: str | None = None,
        tool_name: str | None = None,
        decision: str | None = None,
        limit: int = 20,
        cursor: int | None = None,
    ) -> dict[str, Any]:
        params = {
            "session_id": session_id,
            "tool_name": tool_name,
            "decision": decision,
            "limit": limit,
            "cursor": cursor,
        }
        return self._request(
            "GET", "/audit", params={k: v for k, v in params.items() if v is not None}
        )

    def audit_row(self, log_id: int) -> dict[str, Any]:
        return self._request("GET", f"/audit/{log_id}")

    def verify(self) -> dict[str, Any]:
        return self._request("GET", "/audit/verify")

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._http.request(method, f"{self.base_url}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise ApiError(0, f"API unreachable at {self.base_url} ({type(exc).__name__})") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise ApiError(response.status_code, str(detail))
        data: dict[str, Any] = response.json()
        return data


def make_client() -> ColdTraceClient:
    return ColdTraceClient(get_settings().api_url)
