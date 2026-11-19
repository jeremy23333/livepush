"""托管协议端（NapCat / SnowLuma 的 Docker 容器）编排。"""

from .base import (
    Artifact,
    HostPorts,
    HostedBackend,
    HostedContext,
    HostedStatus,
    container_name_for,
)
from .manager import (
    BACKENDS,
    HostedError,
    HostedManager,
    InstallJob,
    backend_of,
    hosted_manager,
)

__all__ = [
    "Artifact",
    "BACKENDS",
    "HostPorts",
    "HostedBackend",
    "HostedContext",
    "HostedError",
    "HostedManager",
    "HostedStatus",
    "InstallJob",
    "backend_of",
    "container_name_for",
    "hosted_manager",
]
