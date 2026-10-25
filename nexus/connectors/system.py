"""Read-only system statistics via psutil."""

from __future__ import annotations

import asyncio
import json
import platform
import time
from typing import Annotated, Any, Literal

import psutil
from pydantic import Field

from nexus.connectors.base import Connector, resource, tool


def _gb(n: float) -> float:
    return round(n / 1024**3, 2)


class SystemConnector(Connector):
    name = "system"
    description = "CPU, memory, disk and process statistics for this machine (read-only)."

    @tool(read_only=True)
    async def stats(self) -> dict[str, Any]:
        """Current CPU, memory, disk and uptime summary."""

        def collect() -> dict[str, Any]:
            vm = psutil.virtual_memory()
            disks = []
            for part in psutil.disk_partitions(all=False):
                try:
                    du = psutil.disk_usage(part.mountpoint)
                except (PermissionError, OSError):
                    continue
                disks.append(
                    {
                        "mount": part.mountpoint,
                        "total_gb": _gb(du.total),
                        "used_gb": _gb(du.used),
                        "percent": du.percent,
                    }
                )
            return {
                "os": f"{platform.system()} {platform.release()}",
                "machine": platform.machine(),
                "python": platform.python_version(),
                "cpu_percent": psutil.cpu_percent(interval=0.3),
                "cpu_count": psutil.cpu_count(logical=True),
                "memory": {
                    "total_gb": _gb(vm.total),
                    "used_gb": _gb(vm.used),
                    "available_gb": _gb(vm.available),
                    "percent": vm.percent,
                },
                "disks": disks,
                "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
            }

        return await asyncio.to_thread(collect)

    @tool(read_only=True)
    async def top_processes(
        self,
        sort_by: Literal["memory", "cpu"] = "memory",
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> list[dict]:
        """The processes using the most memory or CPU."""

        def collect() -> list[dict]:
            procs = list(psutil.process_iter(["pid", "name", "memory_info"]))
            for p in procs:  # prime cpu_percent counters
                try:
                    p.cpu_percent(None)
                except psutil.Error:
                    pass
            time.sleep(0.3)
            rows = []
            for p in procs:
                try:
                    rows.append(
                        {
                            "pid": p.info["pid"],
                            "name": p.info["name"],
                            "memory_mb": round(
                                (p.info["memory_info"].rss if p.info["memory_info"] else 0)
                                / 1024**2,
                                1,
                            ),
                            "cpu_percent": p.cpu_percent(None),
                        }
                    )
                except psutil.Error:
                    continue
            key = "memory_mb" if sort_by == "memory" else "cpu_percent"
            return sorted(rows, key=lambda r: r[key], reverse=True)[:limit]

        return await asyncio.to_thread(collect)

    @resource("nexus://system/stats", name="system_stats", mime_type="application/json")
    async def stats_resource(self) -> str:
        """Live system statistics as JSON."""
        return json.dumps(await self.stats())
