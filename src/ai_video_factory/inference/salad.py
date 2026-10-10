from __future__ import annotations

import json
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_SALAD_API_BASE_URL = "https://api.salad.com/api/public"
REQUEST_TIMEOUT_SECONDS = 30
READ_MAX_ATTEMPTS = 6
TRANSIENT_HTTP_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
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


class SaladTransientRequestError(RuntimeError):
    """Retryable Salad transport failure such as timeout or disconnect."""


def build_salad_ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def _read_retry_delay_seconds(attempt: int) -> int:
    return min(15, 2 * attempt)


class SaladClient:
    """Minimal Salad Container Groups client used by the host-owned autoscaler."""

    def __init__(
        self,
        config: SaladDispatchConfig,
        *,
        warning_logger: Callable[[str], None] | None = None,
        retry_observer: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self.warning_logger = warning_logger or print
        self.retry_observer = retry_observer

    @property
    def container_group_name(self) -> str:
        return str(self.config.container_group_name or "").strip()

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

    def list_project_container_groups(self) -> list[dict[str, object]]:
        self._validate_project_config()
        payload = self._get(self._build_project_containers_url())
        items = payload.get("items", [])
        if not isinstance(items, list):
            raise RuntimeError("Salad returned an invalid container group list")
        return [dict(item) for item in items if isinstance(item, dict)]

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

    def reallocate_container_group_instance(self, instance_id: str) -> None:
        """Request Salad to move one idle, fenced instance to a different node.

        Only the singleton controller may invoke this provider mutation.
        Do not use this operation while a worker has any active leased job.
        """
        self._validate_config()
        resolved = str(instance_id or "").strip()
        import uuid

        try:
            resolved = str(uuid.UUID(resolved))
        except ValueError as exc:
            raise ValueError("Salad instance ID must be a UUID") from exc
        self._post(self._build_container_url() + f"/instances/{resolved}/reallocate")

    def list_container_group_instances(self) -> list[dict[str, object]]:
        self._validate_config()
        payload = self._get(self._build_container_url() + "/instances")
        items = payload.get("items", payload.get("instances", []))
        if not isinstance(items, list):
            raise RuntimeError("Salad returned an invalid instance list")
        return [dict(item) for item in items if isinstance(item, dict)]

    def _validate_project_config(self) -> None:
        missing = [
            name
            for name, value in (
                ("SALAD_API_KEY", self.config.api_key),
                ("SALAD_ORGANIZATION", self.config.organization_name),
                ("SALAD_PROJECT", self.config.project_name),
            )
            if not str(value or "").strip()
        ]
        if missing:
            raise RuntimeError(
                "Salad environment variables are missing for project inventory: "
                + ", ".join(missing)
            )

    def _validate_config(self) -> None:
        missing = [
            name
            for name, value in (
                ("SALAD_API_KEY", self.config.api_key),
                ("SALAD_ORGANIZATION", self.config.organization_name),
                ("SALAD_PROJECT", self.config.project_name),
                ("SALAD_CONTAINER_GROUP_NAME", self.config.container_group_name),
            )
            if not str(value or "").strip()
        ]
        if missing:
            raise RuntimeError(
                "Salad environment variables are missing for predictive scaling: "
                + ", ".join(missing)
            )

    def _build_project_containers_url(self) -> str:
        base_url = self.config.api_base_url.rstrip("/")
        return (
            f"{base_url}/organizations/{self.config.organization_name}"
            f"/projects/{self.config.project_name}/containers"
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
        for attempt in range(1, READ_MAX_ATTEMPTS + 1):
            request = Request(url, method="GET", headers=self._headers())
            try:
                status_code, detail = self._request(request)
            except SaladTransientRequestError as error:
                if attempt >= READ_MAX_ATTEMPTS:
                    raise
                self._warn_read_retry(
                    url=url,
                    attempt=attempt,
                    reason=str(error),
                )
                continue
            if status_code == 200:
                return _decode_object(detail, url=url)
            if (
                status_code in TRANSIENT_HTTP_STATUS_CODES
                and attempt < READ_MAX_ATTEMPTS
            ):
                self._warn_read_retry(
                    url=url,
                    attempt=attempt,
                    reason=f"HTTP {status_code}",
                )
                continue
            self._raise_request_error(
                status_code=status_code,
                url=url,
                detail=detail,
            )
        raise AssertionError("Salad GET retry loop exhausted unexpectedly")

    def _warn_read_retry(self, *, url: str, attempt: int, reason: str) -> None:
        delay_seconds = _read_retry_delay_seconds(attempt)
        self.warning_logger(
            "Warning: Salad GET transient failure "
            f"({attempt}/{READ_MAX_ATTEMPTS}) url={url} reason={reason}; "
            f"retrying in {delay_seconds} seconds."
        )
        if self.retry_observer is not None:
            self.retry_observer()
        time.sleep(delay_seconds)

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
        except (TimeoutError, URLError, ConnectionError) as error:
            raise SaladTransientRequestError(
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
