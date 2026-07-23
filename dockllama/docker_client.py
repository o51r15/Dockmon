"""Docker SDK wrapper for DockLlama."""

from __future__ import annotations

import docker
from docker.models.containers import Container


def get_client() -> docker.DockerClient:
    """Connect to Docker via the mounted socket."""
    return docker.from_env()


def list_containers(client: docker.DockerClient, names: list[str] | None = None) -> list[Container]:
    """List running containers, optionally filtered by name."""
    containers = client.containers.list()
    if names:
        name_set = set(names)
        containers = [c for c in containers if c.name in name_set]
    return containers


def get_logs(container: Container, tail: int = 200) -> str:
    """Get recent logs from a container."""
    return container.logs(tail=tail, timestamps=True).decode("utf-8", errors="replace")



def get_container_stats(container: Container) -> dict[str, float | None]:
    """Fetch CPU and memory usage for a container.

    Returns dict with cpu_percent and mem_percent (floats), or None if unavailable.
    Uses Docker's delta calculation for accurate CPU percentage.
    """
    try:
        stats = container.stats(stream=False)

        # Memory
        mem_stats = stats.get("memory_stats", {})
        mem_usage = mem_stats.get("usage")
        mem_limit = mem_stats.get("limit")
        mem_percent = None
        if mem_usage is not None and mem_limit and mem_limit > 0:
            # Subtract cache if available for more accurate reading
            cache = mem_stats.get("stats", {}).get("cache", 0)
            mem_percent = round(((mem_usage - cache) / mem_limit) * 100.0, 1)

        # CPU (delta between current and previous reading)
        cpu_stats = stats.get("cpu_stats", {})
        precpu_stats = stats.get("precpu_stats", {})
        cpu_percent = None

        cpu_total = cpu_stats.get("cpu_usage", {}).get("total_usage", 0)
        precpu_total = precpu_stats.get("cpu_usage", {}).get("total_usage", 0)
        system_total = cpu_stats.get("system_cpu_usage", 0)
        presystem_total = precpu_stats.get("system_cpu_usage", 0)

        cpu_delta = cpu_total - precpu_total
        system_delta = system_total - presystem_total

        if system_delta > 0 and cpu_delta >= 0:
            num_cpus = cpu_stats.get("online_cpus") or len(
                cpu_stats.get("cpu_usage", {}).get("percpu_usage", [1])
            )
            cpu_percent = round((cpu_delta / system_delta) * num_cpus * 100.0, 1)

        return {"cpu_percent": cpu_percent, "mem_percent": mem_percent}
    except Exception:
        return {"cpu_percent": None, "mem_percent": None}

if __name__ == "__main__":
    client = get_client()
    print("Connected to Docker")
    print(f"Server version: {client.version()['Version']}")
    print()
    for c in client.containers.list():
        print(f"  {c.name:30s} {c.status:12s} {c.image.tags[0] if c.image.tags else 'untagged'}")
