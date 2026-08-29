"""Optional MCP adapters with bounded concurrency, retries, rate limits and TTL cache.

The default profile does not require an MCP server.  Hermes-native MCP tools can
be used by the Markdown skill and evidence can be submitted through the CLI.
When a Python-side adapter is configured, this module provides a small stable
interface without adding aiohttp, requests or structlog dependencies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import timedelta
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Protocol, Sequence

import core

from .exceptions import MCPError
from .logging_config import log_extra
from .metrics import Metrics

LOGGER = logging.getLogger("researchagen.bottom_detection.mcp")


class MCPTransport(Protocol):
    """Minimal transport contract used by the evaluator layer."""

    async def search(self, tool: str, query: str) -> List[Dict[str, Any]]:
        ...


class CallableMCPTransport:
    """Adapter useful for Hermes RPC bridges and deterministic tests."""

    def __init__(
        self,
        function: Callable[[str, str], Awaitable[List[Dict[str, Any]]]],
    ) -> None:
        self.function = function

    async def search(self, tool: str, query: str) -> List[Dict[str, Any]]:
        return await self.function(tool, query)


class JsonCommandMCPTransport:
    """Call a configured one-shot JSON command for each MCP query.

    The command receives ``{"tool": ..., "query": ...}`` on stdin and must
    return either a JSON list or ``{"results": [...]}``.  This is intentionally
    an adapter boundary, not a second MCP server implementation.
    """

    def __init__(self, command: Sequence[str], timeout_seconds: float = 60.0) -> None:
        self.command = list(command)
        self.timeout_seconds = float(timeout_seconds)

    async def search(self, tool: str, query: str) -> List[Dict[str, Any]]:
        if not self.command:
            return []
        process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        request = json.dumps({"tool": tool, "query": query}, ensure_ascii=False).encode()
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(request), timeout=self.timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise MCPError(f"MCP command timed out: {' '.join(self.command)}") from exc
        if process.returncode != 0:
            detail = stderr.decode("utf-8", "replace")[-400:]
            raise MCPError(f"MCP command failed ({process.returncode}): {detail}")
        try:
            payload = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MCPError("MCP command returned invalid JSON") from exc
        return _normalise_results(payload)


class HTTPMCPTransport:
    """POST JSON to a configured HTTP adapter using stdlib urllib."""

    def __init__(self, endpoints: Dict[str, str], timeout_seconds: float = 60.0) -> None:
        self.endpoints = dict(endpoints)
        self.timeout_seconds = float(timeout_seconds)

    async def search(self, tool: str, query: str) -> List[Dict[str, Any]]:
        endpoint = self.endpoints.get(tool) or self.endpoints.get("*")
        if not endpoint:
            return []

        def request() -> List[Dict[str, Any]]:
            body = json.dumps({"tool": tool, "query": query}, ensure_ascii=False).encode()
            req = urllib.request.Request(
                endpoint,
                data=body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
                raise MCPError(f"MCP HTTP request failed for {tool}: {exc}") from exc
            return _normalise_results(payload)

        return await asyncio.to_thread(request)


class RateLimiter:
    """Sliding-window limiter shared by all MCP tools in one skill run."""

    def __init__(self, calls_per_hour: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.calls_per_hour = max(1, int(calls_per_hour))
        self.clock = clock
        self.calls: Deque[float] = deque()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        window = 3600.0
        while True:
            async with self.lock:
                now = self.clock()
                while self.calls and now - self.calls[0] >= window:
                    self.calls.popleft()
                if len(self.calls) < self.calls_per_hour:
                    self.calls.append(now)
                    return
                wait_for = max(0.01, window - (now - self.calls[0]))
            await asyncio.sleep(wait_for)


class TTLCache:
    """SQLite-backed response cache; expired rows are removed on access."""

    def __init__(self, conn: Any, namespace: str, ttl_hours: float = 24.0) -> None:
        self.conn = conn
        self.namespace = namespace
        self.ttl_hours = max(0.0, float(ttl_hours))

    def get(self, key: str) -> Optional[Any]:
        row = self.conn.execute(
            "SELECT payload, expires_at FROM bd_cache WHERE namespace=? AND cache_key=?",
            (self.namespace, key),
        ).fetchone()
        if row is None:
            return None
        expiry = core.parse_iso(row["expires_at"])
        if expiry is None or expiry <= core.now():
            self.conn.execute(
                "DELETE FROM bd_cache WHERE namespace=? AND cache_key=?",
                (self.namespace, key),
            )
            self.conn.commit()
            return None
        try:
            return json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            self.conn.execute(
                "DELETE FROM bd_cache WHERE namespace=? AND cache_key=?",
                (self.namespace, key),
            )
            self.conn.commit()
            return None

    def set(self, key: str, value: Any) -> None:
        expires = core.iso(core.now() + timedelta(hours=self.ttl_hours))
        self.conn.execute(
            "INSERT INTO bd_cache(namespace,cache_key,payload,expires_at) VALUES (?,?,?,?) "
            "ON CONFLICT(namespace,cache_key) DO UPDATE SET payload=excluded.payload, "
            "expires_at=excluded.expires_at",
            (
                self.namespace,
                key,
                json.dumps(value, ensure_ascii=False, default=str),
                expires,
            ),
        )
        self.conn.commit()


async def retry_async(
    operation: Callable[[], Awaitable[Any]],
    attempts: int = 3,
    base_seconds: float = 0.25,
) -> Any:
    """Retry transient adapter failures with exponential backoff."""

    last_error: Optional[BaseException] = None
    for attempt in range(max(1, int(attempts))):
        try:
            return await operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - adapter boundary
            last_error = exc
            if attempt + 1 >= max(1, int(attempts)):
                break
            delay = max(0.0, float(base_seconds)) * (2**attempt)
            if delay:
                await asyncio.sleep(delay)
    raise MCPError(str(last_error) if last_error else "MCP operation failed") from last_error


class MCPClient:
    """Fan-out client used by LiteratureMCPEvaluator."""

    def __init__(
        self,
        conn: Any,
        namespace: str,
        tools: Sequence[str],
        rate_limit: int = 100,
        cache_ttl_hours: float = 24.0,
        retry_attempts: int = 3,
        retry_base_seconds: float = 0.25,
        transport: Optional[MCPTransport] = None,
        metrics: Optional[Metrics] = None,
    ) -> None:
        self.tools = [str(tool) for tool in tools if str(tool)]
        self.transport = transport
        self.rate_limiter = RateLimiter(rate_limit)
        self.cache = TTLCache(conn, namespace, cache_ttl_hours)
        self.retry_attempts = retry_attempts
        self.retry_base_seconds = retry_base_seconds
        self.metrics = metrics or Metrics()

    async def search_all(self, query: str) -> List[Dict[str, Any]]:
        """Query configured tools concurrently while preserving tool provenance."""

        if self.transport is None or not self.tools:
            self.metrics.inc("mcp_unconfigured")
            return []
        tasks = [self._search_one(tool, query) for tool in self.tools]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        merged: List[Dict[str, Any]] = []
        for tool, result in zip(self.tools, results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                self.metrics.inc("mcp_errors")
                LOGGER.error(
                    "MCP tool failed",
                    extra=log_extra(event="mcp_error", error=str(result)),
                )
                continue
            merged.extend(result)
            self.metrics.inc("mcp_successes")
        return merged

    async def _search_one(self, tool: str, query: str) -> List[Dict[str, Any]]:
        key = f"{tool}:{query.strip().lower()}"
        cached = self.cache.get(key)
        if cached is not None:
            self.metrics.inc("mcp_cache_hits")
            return _normalise_results(cached, default_tool=tool)
        self.metrics.inc("mcp_cache_misses")
        await self.rate_limiter.acquire()

        async def operation() -> List[Dict[str, Any]]:
            if self.transport is None:
                return []
            return await self.transport.search(tool, query)

        try:
            results = await retry_async(
                operation,
                attempts=self.retry_attempts,
                base_seconds=self.retry_base_seconds,
            )
        except MCPError:
            raise
        except Exception as exc:  # noqa: BLE001 - adapter boundary
            raise MCPError(f"MCP search failed for {tool}: {exc}") from exc
        normalised = _normalise_results(results, default_tool=tool)
        self.cache.set(key, normalised)
        return normalised


def _normalise_results(payload: Any, default_tool: str = "") -> List[Dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("results", payload.get("data", []))
    if not isinstance(payload, list):
        return []
    output: List[Dict[str, Any]] = []
    for item in payload:
        if isinstance(item, dict):
            row = dict(item)
        elif isinstance(item, str) and item.strip():
            row = {"claim": item}
        else:
            # Numeric/null/object rows are malformed adapter output, not
            # evidence.  Do not manufacture a scientific claim from them.
            continue
        if default_tool and not row.get("tool"):
            row["tool"] = default_tool
        output.append(row)
    return output
