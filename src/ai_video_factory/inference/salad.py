from __future__ import annotations

from dataclasses import dataclass
import json
import ssl
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_SALAD_API_BASE_URL = "https://api.salad.com/api/public"
REQUEST_TIMEOUT_SECONDS = 20
DEFAULT_USER_AGENT = "ai-video-factory-salad-client/1.0"


@dataclass(frozen=True)
class SaladDispatchConfig:
    api_key: str | None
    api_base_url: str
    organization_name: str | None
    project_name: str | None
    container_group_name: str | None


@dataclass(frozen=True)
class SaladRequestError(RuntimeError):
    message: str
    status_code: int
    url: str
    detail: str

    def __str__(self) -> str:
        return self.message


def build_salad_ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


class SaladClient:
    """Minimal Salad Container Groups client used by the host-owned autoscaler."""

    def __init__(self, config: SaladDispatchConfig) -> None:
        self.config = config

    def start_container_group(self) -> None:
        self._validate_config()
        self._post(self._build_container_url() + "/start")

    def start_container_group_if_needed(
        self,
        *,
        warning_logger: Callable[[str], None] | None = None,
    ) -> bool:
        try:
            self.start_container_group()
            return True
        except SaladRequestError as error:
            tolerated_status = _extract_tolerated_start_status(error.status_code, error.detail)
            if tolerated_status is None:
                raise
            logger = warning_logger or print
            group_name = str(self.config.container_group_name or "<unknown>").strip()
            logger(
                "Warning: Salad did not start the container group "
                f"{group_name} because it is already in {tolerated_status} state. "
                "Continuing without error."
            )
            return False

    def stop_container_group(self) -> None:
        self._validate_config()
        self._post(self._build_container_url() + "/stop")

    def describe_container_group(self) -> dict[str, object]:
        self._validate_config()
        return self._get(self._build_container_url())

    def set_container_group_replicas(self, replicas: int) -> dict[str, object]:
        self._validate_config()
        if replicas < 0:
            raise ValueError("replicas cannot be negative")
        return self._patch(
            self._build_container_url(),
            payload={"replicas": int(replicas)},
        )

    def set_container_group_instance_deletion_cost(
        self,
        instance_id: str,
        deletion_cost: int,
    ) -> dict[str, object]:
        self._validate_config()
        resolved_instance_id = str(instance_id or "").strip()
        if not resolved_instance_id:
            raise ValueError("instance_id is required")
        return self._patch(
            self._build_container_url() + f"/instances/{resolved_instance_id}",
            payload={"deletion_cost": int(deletion_cost)},
        )

    def list_container_group_instances(self) -> list[dict[str, object]]:
        self._validate_config()
        payload = self._get(self._build_container_url() + "/instances")
        items = payload.get("items", payload.get("instances", []))
        if not isinstance(items, list):
            raise RuntimeError("Salad returned an invalid instance list")
        return [dict(item) for item in items if isinstance(item, dict)]

    def _validate_config(self) -> None:
        missing = [
            name
            for name, value in (
                ("SALAD_API_KEY", self.config.api_key),
                ("SALAD_ORGANIZATION_NAME", self.config.organization_name),
                ("SALAD_PROJECT_NAME", self.config.project_name),
                ("SALAD_CONTAINER_GROUP_NAME", self.config.container_group_name),
            )
            if not str(value or "").strip()
        ]
        if missing:
            raise RuntimeError(
                "Salad environment variables are missing for predictive scaling: "
                + ", ".join(missing)
            )

    def _build_container_url(self) -> str:
        base_url = self.config.api_base_url.rstrip("/")
        return (
            f"{base_url}/organizations/{self.config.organization_name}"
            f"/projects/{self.config.project_name}"
            f"/containers/{self.config.container_group_name}"
        )

    def _headers(self, *, json_content: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Salad-Api-Key": str(self.config.api_key or ""),
            "User-Agent": DEFAULT_USER_AGENT,
        }
        if json_content:
            headers["Content-Type"] = "application/merge-patch+json"
        return headers

    def _get(self, url: str) -> dict[str, object]:
        request = Request(url, method="GET", headers=self._headers())
        status_code, detail = self._request(request)
        if status_code != 200:
            self._raise_request_error(status_code=status_code, url=url, detail=detail)
        return _decode_object(detail, url=url)

    def _post(self, url: str) -> None:
        request = Request(url, data=b"", method="POST", headers=self._headers())
        status_code, detail = self._request(request)
        if status_code not in {200, 202}:
            self._raise_request_error(status_code=status_code, url=url, detail=detail)

    def _patch(self, url: str, *, payload: dict[str, object]) -> dict[str, object]:
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="PATCH",
            headers=self._headers(json_content=True),
        )
        status_code, detail = self._request(request)
        if status_code not in {200, 202}:
            self._raise_request_error(status_code=status_code, url=url, detail=detail)
        return _decode_object(detail, url=url) if detail else {}

    @staticmethod
    def _request(request: Request) -> tuple[int, str]:
        try:
            with urlopen(
                request,
                timeout=REQUEST_TIMEOUT_SECONDS,
                context=build_salad_ssl_context(),
            ) as response:
                return (
                    int(getattr(response, "status", 0)),
                    response.read().decode("utf-8", errors="replace").strip(),
                )
        except HTTPError as error:
            return (
                int(error.code),
                error.read().decode("utf-8", errors="replace").strip(),
            )
        except URLError as error:
            raise RuntimeError(
                f"Salad request failed url={request.full_url} error={error}"
            ) from error

    @staticmethod
    def _raise_request_error(*, status_code: int, url: str, detail: str) -> None:
        hint = ""
        if status_code == 403 and "1010" in detail:
            hint = (
                " hint=Cloudflare/edge blocked the request before it reached the API, "
                "usually because of the HTTP/User-Agent signature or network policy."
            )
        message = (
            "Salad request failed "
            f"with status={status_code} url={url} detail={detail or '<empty>'}{hint}"
        )
        raise SaladRequestError(
            message=message,
            status_code=status_code,
            url=url,
            detail=detail,
        )


def _decode_object(detail: str, *, url: str) -> dict[str, object]:
    try:
        payload = json.loads(detail or "{}")
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Salad returned invalid JSON url={url}") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"Salad returned an invalid response url={url}")
    return payload


def _extract_tolerated_start_status(status_code: int, detail: str) -> str | None:
    if status_code != 400:
        return None
    parsed_status = _parse_salad_current_status_from_detail(str(detail or "").strip())
    if parsed_status in {"deploying", "running"}:
        return parsed_status.capitalize()
    return None


def _parse_salad_current_status_from_detail(detail: str) -> str | None:
    if not detail:
        return None
    try:
        decoded = json.loads(detail)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict):
        error_type = str(decoded.get("type") or "").strip().lower()
        detail_text = str(decoded.get("detail") or "").strip().lower()
        if error_type != "cannot_start_container_group_with_current_status":
            return None
        if "deploying status" in detail_text:
            return "deploying"
        if "running status" in detail_text:
            return "running"
        return None
    lowered = detail.lower()
    if "cannot_start_container_group_with_current_status" not in lowered:
        return None
    if "deploying status" in lowered:
        return "deploying"
    if "running status" in lowered:
        return "running"
    return None
