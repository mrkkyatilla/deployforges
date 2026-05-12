from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CloneResult:
    success: bool
    path: Path | None = None
    error_message: str | None = None
    duration_ms: int = 0
    file_count: int = 0
    total_size_bytes: int = 0


_HTTPS_URL_RE = re.compile(r"^https://[^\s]+$")
_CLONE_TIMEOUT = 120


class GitHandler:
    async def clone(
        self,
        url: str,
        dest_path: Path,
        branch: str = "main",
        commit: str | None = None,
    ) -> CloneResult:
        start = time.perf_counter_ns()

        if not _HTTPS_URL_RE.match(url):
            return CloneResult(
                success=False,
                error_message=f"Invalid URL: must start with https:// — got {url!r}",
            )

        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    "git", "clone", "--depth", "1", "--branch", branch, url, str(dest_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=_CLONE_TIMEOUT,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_CLONE_TIMEOUT
            )

            if proc.returncode != 0:
                err = stderr.decode(errors="replace").strip()
                return self._error_result(err, start)

            if commit:
                fetch_proc = await asyncio.create_subprocess_exec(
                    "git", "-C", str(dest_path), "fetch", "--depth", "1", "origin", commit,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, fetch_err = await asyncio.wait_for(
                    fetch_proc.communicate(), timeout=_CLONE_TIMEOUT
                )
                if fetch_proc.returncode != 0:
                    return self._error_result(
                        fetch_err.decode(errors="replace").strip(), start
                    )

                checkout_proc = await asyncio.create_subprocess_exec(
                    "git", "-C", str(dest_path), "checkout", commit,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, co_err = await asyncio.wait_for(
                    checkout_proc.communicate(), timeout=_CLONE_TIMEOUT
                )
                if checkout_proc.returncode != 0:
                    return self._error_result(
                        co_err.decode(errors="replace").strip(), start
                    )

            file_count, total_size = self._count_files(dest_path)
            duration_ms = (time.perf_counter_ns() - start) // 1_000_000

            return CloneResult(
                success=True,
                path=dest_path,
                duration_ms=duration_ms,
                file_count=file_count,
                total_size_bytes=total_size,
            )

        except asyncio.TimeoutError:
            return CloneResult(
                success=False,
                error_message=f"Clone timed out after {_CLONE_TIMEOUT}s",
                duration_ms=(time.perf_counter_ns() - start) // 1_000_000,
            )
        except FileNotFoundError:
            return CloneResult(
                success=False,
                error_message="git executable not found",
                duration_ms=(time.perf_counter_ns() - start) // 1_000_000,
            )

    def _error_result(self, stderr_text: str, start_ns: int) -> CloneResult:
        if "Authentication" in stderr_text or "could not read Username" in stderr_text:
            msg = "Authentication required — provide credentials or use a public repo"
        elif "not found" in stderr_text or "does not exist" in stderr_text:
            msg = "Repository not found"
        else:
            msg = stderr_text[:500]

        return CloneResult(
            success=False,
            error_message=msg,
            duration_ms=(time.perf_counter_ns() - start_ns) // 1_000_000,
        )

    @staticmethod
    def _count_files(root: Path) -> tuple[int, int]:
        count = 0
        size = 0
        for p in root.rglob("*"):
            if p.is_file() and ".git" not in p.parts:
                count += 1
                size += p.stat().st_size
        return count, size
