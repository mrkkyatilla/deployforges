from __future__ import annotations

from core.manifest.schema import ManifestService

_HTTP_TYPES = frozenset({"api", "web"})


def pick_primary_service(services: list[ManifestService]) -> str | None:
    """Choose the HTTP service used for Cloud Run / primary artifact alias."""
    if not services:
        return None

    for preferred in ("api", "web"):
        for svc in services:
            if svc.type == preferred and svc.port:
                return svc.name

    for preferred in ("api", "web"):
        for svc in services:
            if svc.type == preferred:
                return svc.name

    for svc in services:
        if svc.type != "database":
            return svc.name

    return services[0].name
