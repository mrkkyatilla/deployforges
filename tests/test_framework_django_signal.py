"""Django vs library repos: ``wsgi.py`` alone must not imply Django."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.analysis.fingerprint import FileNode, FileTree
from core.analysis.framework_detector import FrameworkDetector


def test_django_fixture_still_detected_via_manage_py() -> None:
    root = Path(__file__).resolve().parent / "fixtures" / "python_django" / "helloworld"
    if not root.is_dir():
        pytest.skip("django fixture missing")
    nodes: list[FileNode] = []
    total = 0
    for p in root.rglob("*"):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            nodes.append(
                FileNode(
                    path=rel,
                    size_bytes=p.stat().st_size,
                    extension=p.suffix.lower(),
                    is_config=p.suffix in (".toml", ".ini", ".cfg"),
                    is_source=p.suffix == ".py",
                )
            )
            total += p.stat().st_size
    ft = FileTree(root_path=str(root), total_files=len(nodes), total_size_bytes=total, nodes=nodes)
    det = FrameworkDetector().detect(ft, "python", str(root))
    assert det.name == "django"


def test_wsgi_py_without_manage_py_is_not_django(tmp_path: Path) -> None:
    (tmp_path / "wsgi.py").write_text("# not django\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'alib'\n", encoding="utf-8")
    nodes = [
        FileNode("wsgi.py", 12, ".py", False, True),
        FileNode("pyproject.toml", 24, ".toml", True, False),
    ]
    ft = FileTree(
        root_path=str(tmp_path),
        total_files=len(nodes),
        total_size_bytes=36,
        nodes=nodes,
    )
    det = FrameworkDetector().detect(ft, "python", str(tmp_path))
    assert det.name != "django"
