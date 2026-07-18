"""Docker SDK wrapper for Dockmon."""

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


if __name__ == "__main__":
    client = get_client()
    print("Connected to Docker")
    print(f"Server version: {client.version()['Version']}")
    print()
    for c in client.containers.list():
        print(f"  {c.name:30s} {c.status:12s} {c.image.tags[0] if c.image.tags else 'untagged'}")
