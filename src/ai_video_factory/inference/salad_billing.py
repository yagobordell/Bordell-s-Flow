"""Observed Salad LTX GPU costs (not a Salad invoice or a per-job allocation)."""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .salad import DEFAULT_SALAD_API_BASE_URL, SaladClient, SaladDispatchConfig
from .worker_image_routes import route_for_image

PRICING_URL = "https://salad.com/pricing"
PRICING_AS_OF = "2026-09-12"
_SCHEMA = "salad-observed-cost-v1"
_SERVICES = ("ltx25_i2v", "ltx25")
_DEFAULT_LTX_RATE = Decimal("0.50")


def _usd(amount: Decimal) -> str:
    return f"{amount:.8f}"


def _utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def _save(path: Path, payload: dict[str, Any]) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Refusing linked Salad cost journal")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    if temporary.is_symlink():
        raise ValueError("Refusing linked Salad cost journal temporary file")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _rate(service: str, definition: dict[str, Any]) -> str | None:
    override = os.getenv(f"SALAD_{service.upper()}_USD_PER_GPU_HOUR", "").strip()
    if override:
        try:
            amount = Decimal(override)
        except InvalidOperation:
            return None
        return str(amount) if amount.is_finite() and amount > 0 else None
    resources = definition.get("resources")
    if (
        definition.get("priority") == "high"
        and isinstance(resources, dict)
        and resources.get("gpu_class_names") == ["RTX 5090 (32 GB)"]
    ):
        return str(_DEFAULT_LTX_RATE)
    return None


class SaladCostMeter:
    """Persist billable running-instance observations across video run resumes."""

    def __init__(
        self,
        output: Path,
        *,
        services_path: Path,
        api_key: str | None,
        organization: str | None,
        project: str | None,
        poll_seconds: float = 15.0,
        clients: dict[str, SaladClient] | None = None,
        worker_images: dict[str, str] | None = None,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("Salad cost polling interval must be positive")
        self.path = output / "video" / "salad_costs.json"
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._poll_count = 0
        self._journal: dict[str, Any] = {}
        self._session: dict[str, Any] = {}
        self._clients: dict[str, SaladClient] = {}
        self._unavailable = None
        if not api_key or not organization or not project:
            self._unavailable = "Salad API credentials or organization/project are missing"
        document = json.loads(services_path.read_text(encoding="utf-8"))
        definitions = document["services"]
        self._definitions = (
            {} if worker_images else {name: definitions[name] for name in _SERVICES}
        )
        # This is observed group cost, not a per-user allocation.
        for kind, image in (worker_images or {}).items():
            service = "ltx25" if kind == "a2v" else "ltx25_i2v"
            route = route_for_image(service, image)
            self._definitions[service + "@" + image.rsplit(":", 1)[-1][:16]] = {
                **definitions[service], "group_name": route.group_name,
            }
        if self._unavailable is None:
            for service, definition in self._definitions.items():
                self._clients[service] = (
                    clients[service] if clients is not None else SaladClient(
                        SaladDispatchConfig(
                            api_key=api_key,
                            api_base_url=os.getenv(
                                "SALAD_API_BASE_URL", DEFAULT_SALAD_API_BASE_URL
                            ),
                            organization_name=organization,
                            project_name=project,
                            container_group_name=definition["group_name"],
                        )
                    )
                )

    def __enter__(self) -> SaladCostMeter:
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError("Refusing linked Salad cost journal")
        if self.path.is_file():
            self._journal = json.loads(self.path.read_text(encoding="utf-8"))
            if self._journal.get("schema_version") != _SCHEMA:
                raise ValueError("Unknown Salad cost journal schema")
            for earlier in self._journal["sessions"]:
                if earlier.get("status") == "running":
                    earlier["status"] = "interrupted"
        else:
            self._journal = {"schema_version": _SCHEMA, "sessions": []}
        self._session = {
            "started_at": datetime.now(UTC).isoformat(),
            "ended_at": None,
            "status": "unavailable" if self._unavailable else "running",
            "unavailable_reason": self._unavailable,
            "services": {
                service: {
                    "group_name": definition["group_name"],
                    "hourly_rate_usd": _rate(service.split("@", 1)[0], definition),
                    "gpu_running_seconds": 0.0,
                    "last_running": {},
                    "sample_errors": 0,
                    "successful_samples": 0,
                    "unobserved_instances": 0,
                }
                for service, definition in self._definitions.items()
            },
        }
        self._journal["sessions"].append(self._session)
        _save(self.path, self._journal)
        if self._unavailable is None:
            self._sample()
            self._thread = threading.Thread(
                target=self._poll, name="salad-cost-meter", daemon=True
            )
            self._thread.start()
        return self

    def _poll(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            self._sample()
            self._poll_count += 1
            if self._poll_count % 4 == 0:
                summary = read_salad_costs(self.path)
                if summary is not None and summary["priced_subtotal_usd"] is not None:
                    print(
                        "  Salad LTX cost so far (observed): $"
                        + str(summary["priced_subtotal_usd"]),
                        flush=True,
                    )

    def _observe(
        self, service: str, instances: list[dict[str, object]], now: datetime
    ) -> None:
        entry = self._session["services"][service]
        previous = entry["last_running"]
        current: dict[str, dict[str, str]] = {}
        seen: set[str] = set()
        session_start = _utc(self._session["started_at"])
        if session_start is None:
            raise ValueError("Salad cost session start is invalid")
        for item in instances:
            instance_id = str(item.get("id") or item.get("instance_id") or "")
            if not instance_id or instance_id in seen:
                entry["unobserved_instances"] += 1
                continue
            seen.add(instance_id)
            old = previous.get(instance_id)
            transition = _utc(item.get("update_time"))
            state = str(item.get("state") or "").lower()
            if state == "running":
                if transition is None:
                    entry["unobserved_instances"] += 1
                    since = now
                elif old is None:
                    since = max(session_start, transition)
                else:
                    old_time = _utc(old["observed_until"])
                    if old_time is None:
                        raise ValueError("Salad last observation time is invalid")
                    since = max(old_time, transition)
                entry["gpu_running_seconds"] += max(0.0, (now - since).total_seconds())
                current[instance_id] = {"observed_until": now.isoformat()}
            elif old is not None:
                old_time = _utc(old["observed_until"])
                if old_time is None:
                    raise ValueError("Salad last observation time is invalid")
                if transition is None:
                    entry["unobserved_instances"] += 1
                elif transition > old_time:
                    until = min(now, transition)
                    entry["gpu_running_seconds"] += max(
                        0.0, (until - old_time).total_seconds()
                    )
            if state not in {
                "allocating", "downloading", "creating", "running", "stopping"
            }:
                entry["unobserved_instances"] += 1
        entry["unobserved_instances"] += len(set(previous) - seen)
        entry["last_running"] = current

    def _sample(self) -> None:
        for service, client in self._clients.items():
            try:
                instances = client.list_container_group_instances()
                if not isinstance(instances, list):
                    raise ValueError("Salad instance list is invalid")
                self._observe(service, instances, datetime.now(UTC))
                self._session["services"][service]["successful_samples"] += 1
            except Exception as error:
                self._session["services"][service]["sample_errors"] += 1
                print(
                    f"  Salad cost telemetry unavailable for {service}: {error}",
                    flush=True,
                )
        _save(self.path, self._journal)

    def __exit__(self, _kind: object, _value: object, _traceback: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._sample()
        self._session["ended_at"] = datetime.now(UTC).isoformat()
        if self._unavailable is None:
            self._session["status"] = "completed"
        _save(self.path, self._journal)
        summary = read_salad_costs(self.path)
        if summary is not None:
            amount = summary["priced_subtotal_usd"]
            print(
                "  Salad LTX observed group cost: "
                + ("coste no disponible" if amount is None else "$" + str(amount))
                + " ("
                + str(summary["pricing_status"])
                + "; excludes capacity outside the video stage)",
                flush=True,
            )


def read_salad_costs(path: Path) -> dict[str, Any] | None:
    """Summarize only observed running seconds; never infer an entire invoice."""
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Refusing linked Salad cost journal")
    if not path.is_file():
        return None
    journal = json.loads(path.read_text(encoding="utf-8"))
    if journal.get("schema_version") != _SCHEMA:
        raise ValueError("Unknown Salad cost journal schema")
    totals = {
        service: {"gpu_running_seconds": 0.0, "priced_usd": Decimal(0), "priced_samples": 0}
        for service in _SERVICES
    }
    subtotal = Decimal(0)
    incomplete = False
    successful_samples = 0
    priced_samples = 0
    for session in journal["sessions"]:
        incomplete |= session.get("status") != "completed"
        for lane, entry in session["services"].items():
            service = lane.split("@", 1)[0]
            if service not in totals:
                raise ValueError("Unrecognized Salad LTX cost service in journal")
            successful_samples += entry.get("successful_samples", 0)
            seconds = float(entry["gpu_running_seconds"])
            totals[service]["gpu_running_seconds"] += seconds
            rate = entry["hourly_rate_usd"]
            if (
                rate is None or entry["sample_errors"] or entry["unobserved_instances"]
                or not entry.get("successful_samples")
            ):
                incomplete = True
            if rate is not None and entry.get("successful_samples"):
                priced_samples += entry["successful_samples"]
                totals[service]["priced_samples"] += entry["successful_samples"]
                cost = Decimal(str(seconds)) * Decimal(rate) / Decimal(3600)
                totals[service]["priced_usd"] += cost
                subtotal += cost
            if entry["last_running"]:
                incomplete = True  # The independent controller may still be billing.
    return {
        "currency": "USD",
        "kind": "observed_salad_group_cost_not_invoice",
        "pricing_source": PRICING_URL,
        "pricing_as_of": PRICING_AS_OF,
        "pricing_status": (
            "unavailable" if not successful_samples else
            "partial" if incomplete else "observed_window_complete"
        ),
        "priced_subtotal_usd": _usd(subtotal) if priced_samples else None,
        "estimated_observed_cost_usd": (
            None if incomplete or not successful_samples else _usd(subtotal)
        ),
        "scope": (
            "LTX container groups during instrumented video stages, including running "
            "idle replicas; not a per-job allocation. May include other users of the "
            "shared group. Excludes charges before/after observation and unknown intervals."
        ),
        "services": {
            service: {
                "gpu_running_seconds": round(values["gpu_running_seconds"], 3),
                "priced_subtotal_usd": (
                    _usd(values["priced_usd"]) if values["priced_samples"] else None
                ),
            }
            for service, values in totals.items()
        },
        "session_count": len(journal["sessions"]),
    }
