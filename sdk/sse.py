from __future__ import annotations

import json
from collections.abc import Generator
from typing import Any

import httpx


def parse_sse_stream(response: httpx.Response) -> Generator[dict[str, Any], None, None]:
    """Parse Server-Sent Events from an httpx streaming response.

    Yields parsed JSON data dicts for each ``data:`` line in the SSE stream.
    Lines prefixed with ``event:`` or ``id:`` are included as metadata keys
    in the yielded dict when present.
    """
    event_type: str | None = None
    event_id: str | None = None
    data_buf: list[str] = []

    for line in response.iter_lines():
        if not line:
            if data_buf:
                raw = "\n".join(data_buf)
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = {"raw": raw}
                if event_type:
                    payload.setdefault("event", event_type)
                if event_id:
                    payload.setdefault("id", event_id)
                yield payload
                data_buf = []
                event_type = None
                event_id = None
            continue

        if line.startswith("data:"):
            data_buf.append(line[5:].strip())
        elif line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("id:"):
            event_id = line[3:].strip()
