from __future__ import annotations

import asyncio
import tarfile
import zipfile
from dataclasses import dataclass
from functools import partial
from pathlib import Path

_MAX_DECOMPRESSION_RATIO = 10
_ABSOLUTE_SIZE_THRESHOLD = 1_073_741_824  # 1 GB
_MAX_FILE_COUNT = 10_000


@dataclass
class ExtractResult:
    success: bool
    path: Path | None = None
    error_message: str | None = None
    file_count: int = 0
    total_size_bytes: int = 0


class ArchiveHandler:
    async def extract(self, file_path: Path, dest_path: Path) -> ExtractResult:
        suffix = "".join(file_path.suffixes).lower()

        if suffix in (".tar.gz", ".tgz"):
            extractor = partial(self._extract_tar, file_path, dest_path, "gz")
        elif suffix == ".tar":
            extractor = partial(self._extract_tar, file_path, dest_path, "")
        elif suffix == ".zip":
            extractor = partial(self._extract_zip, file_path, dest_path)
        else:
            return ExtractResult(
                success=False,
                error_message=f"Unsupported archive format: {suffix}",
            )

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, extractor)

    def _extract_zip(self, file_path: Path, dest_path: Path) -> ExtractResult:
        compressed_size = file_path.stat().st_size

        try:
            with zipfile.ZipFile(file_path, "r") as zf:
                members = zf.infolist()

                if len(members) > _MAX_FILE_COUNT:
                    return ExtractResult(
                        success=False,
                        error_message=f"Archive contains {len(members)} files (max {_MAX_FILE_COUNT})",
                    )

                total_uncompressed = sum(m.file_size for m in members)
                if not self._check_bomb(compressed_size, total_uncompressed):
                    return ExtractResult(
                        success=False,
                        error_message="Decompression bomb detected: ratio exceeds safe limits",
                    )

                for member in members:
                    target = (dest_path / member.filename).resolve()
                    if not str(target).startswith(str(dest_path.resolve())):
                        return ExtractResult(
                            success=False,
                            error_message=f"Path traversal detected: {member.filename}",
                        )

                dest_path.mkdir(parents=True, exist_ok=True)
                zf.extractall(dest_path)

        except zipfile.BadZipFile as exc:
            return ExtractResult(success=False, error_message=f"Corrupt zip: {exc}")

        file_count, total_size = self._count_files(dest_path)
        return ExtractResult(
            success=True,
            path=dest_path,
            file_count=file_count,
            total_size_bytes=total_size,
        )

    def _extract_tar(
        self, file_path: Path, dest_path: Path, compression: str
    ) -> ExtractResult:
        compressed_size = file_path.stat().st_size
        mode = f"r:{compression}" if compression else "r:"

        try:
            with tarfile.open(file_path, mode) as tf:
                members = tf.getmembers()

                if len(members) > _MAX_FILE_COUNT:
                    return ExtractResult(
                        success=False,
                        error_message=f"Archive contains {len(members)} entries (max {_MAX_FILE_COUNT})",
                    )

                total_uncompressed = sum(m.size for m in members if m.isfile())
                if not self._check_bomb(compressed_size, total_uncompressed):
                    return ExtractResult(
                        success=False,
                        error_message="Decompression bomb detected: ratio exceeds safe limits",
                    )

                for member in members:
                    target = (dest_path / member.name).resolve()
                    if not str(target).startswith(str(dest_path.resolve())):
                        return ExtractResult(
                            success=False,
                            error_message=f"Path traversal detected: {member.name}",
                        )

                dest_path.mkdir(parents=True, exist_ok=True)
                tf.extractall(dest_path, filter="data")

        except (tarfile.TarError, OSError) as exc:
            return ExtractResult(success=False, error_message=f"Corrupt archive: {exc}")

        file_count, total_size = self._count_files(dest_path)
        return ExtractResult(
            success=True,
            path=dest_path,
            file_count=file_count,
            total_size_bytes=total_size,
        )

    @staticmethod
    def _check_bomb(compressed_size: int, uncompressed_size: int) -> bool:
        if uncompressed_size <= _ABSOLUTE_SIZE_THRESHOLD:
            return True
        ratio = uncompressed_size / max(compressed_size, 1)
        return ratio <= _MAX_DECOMPRESSION_RATIO

    @staticmethod
    def _count_files(root: Path) -> tuple[int, int]:
        count = 0
        size = 0
        for p in root.rglob("*"):
            if p.is_file():
                count += 1
                size += p.stat().st_size
        return count, size
