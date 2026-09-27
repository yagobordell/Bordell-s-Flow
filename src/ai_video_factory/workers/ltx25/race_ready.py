"""Protect the FIRST model-ready LTX worker from voluntary Salad scale-in.

Only the protected monk race enables this guard. Readiness stays 503 until the
worker owns a dedicated Postgres advisory election lock AND its own local Salad
IMDS GET confirms deletion_cost=100000 after a PUT. A second worker never sets
a competing high cost and cannot pass /ready while the leader is alive.

Do not also set deletion cost through the external Salad API: Salad explicitly
warns that mixing the two writers may cause unexpected behavior.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from typing import Any
from urllib.request import ProxyHandler, Request, build_opener

_LOGGER = logging.getLogger(__name__)
_IMDS_URL = "http://169.254.169.254/v1/deletion-cost"
_HEADERS = {"Metadata": "true"}
_LOCK_NAMESPACE = 1_497_450_319
_LOCK_KEY = 27_0926
_PROTECTED_COST = 100000


class LTXRaceReadyGuard:
    """Hold the first-ready election lock and verify local instance protection."""

    def __init__(
        self,
        dsn: str,
        *,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        if not dsn.strip():
            raise ValueError("LTX race ready guard requires a Postgres DSN")
        if connect is None:
            import psycopg

            connect = psycopg.connect
        self._dsn = dsn
        self._connect = connect
        self._connection: Any | None = None
        self._mutex = threading.RLock()
        # Ignore external HTTP(S)_PROXY when accessing link-local metadata.
        self._opener = build_opener(ProxyHandler({}))

    def _request(self, method: str, payload: bytes | None = None) -> tuple[int, bytes]:
        headers = dict(_HEADERS)
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            _IMDS_URL, data=payload, headers=headers, method=method
        )
        with self._opener.open(request, timeout=3) as response:
            return int(response.status), response.read(4096)

    def _get_cost(self) -> int:
        status, data = self._request("GET")
        if status != 200:
            raise RuntimeError("Salad IMDS deletion-cost GET did not return 200")
        try:
            decoded = json.loads(data)
        except (ValueError, TypeError) as error:
            raise RuntimeError("Salad IMDS returned malformed deletion-cost JSON") from error
        value = decoded.get("deletion_cost") if isinstance(decoded, dict) else None
        if type(value) is not int:
            raise RuntimeError("Salad IMDS did not expose an integer deletion cost")
        return value

    def _put_cost(self, cost: int) -> None:
        status, _ = self._request(
            "PUT", json.dumps({"deletion_cost": cost}).encode("utf-8")
        )
        if status != 204:
            raise RuntimeError("Salad IMDS deletion-cost PUT was not confirmed (204)")

    def _check_leadership(self) -> None:
        if self._connection is None:
            connection = self._connect(self._dsn, autocommit=True)
            try:
                row = connection.execute(
                    "SELECT pg_try_advisory_lock(%s, %s)",
                    (_LOCK_NAMESPACE, _LOCK_KEY),
                ).fetchone()
                if row is None or row[0] is not True:
                    raise RuntimeError(
                        "another LTX worker became ready first; this worker remains unready"
                    )
            except Exception:
                connection.close()
                raise
            self._connection = connection
            _LOGGER.info("LTX_RACE elected first-ready worker")
        if self._connection.closed:
            raise RuntimeError("LTX first-ready election Postgres session was lost")
        row = self._connection.execute(
            """
            SELECT 1 FROM pg_locks
            WHERE locktype = 'advisory'
              AND pid = pg_backend_pid()
              AND classid = %s::oid
              AND objid = %s::oid
              AND objsubid = 2
            """,
            (_LOCK_NAMESPACE, _LOCK_KEY),
        ).fetchone()
        if row is None:
            raise RuntimeError("LTX first-ready worker no longer owns the election lock")

    def ensure_protected(self) -> None:
        with self._mutex:
            try:
                self._check_leadership()
            except Exception:
                # If this worker lost leadership after being protected, avoid
                # leaving an unready former winner with a high deletion cost.
                if self._connection is not None:
                    try:
                        self._put_cost(0)
                    except Exception:
                        _LOGGER.exception("LTX could not clear lost-leader deletion cost")
                    self.close()
                raise

            cost = self._get_cost()
            if cost != _PROTECTED_COST:
                self._put_cost(_PROTECTED_COST)
                cost = self._get_cost()
                if cost != _PROTECTED_COST:
                    raise RuntimeError(
                        "Salad IMDS did not persist first-ready worker deletion cost"
                    )
                _LOGGER.info("LTX_RACE_IMDS_PROTECTED deletion_cost=%s", cost)

    def close(self) -> None:
        with self._mutex:
            connection, self._connection = self._connection, None
            if connection is not None:
                connection.close()
