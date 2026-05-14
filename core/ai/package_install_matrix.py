"""Single source of truth for Python dependency install RUN patterns (prompts + templates + fix).

Referenced from Dockerfile prompts so plan / generation / fix stay aligned.
"""

from __future__ import annotations

from pathlib import Path

# Keys: dependency manager as reported by AnalysisEngine (DependencyInfo.manager).
PYTHON_INSTALL_BLOCKS: dict[str, str] = {
    "pip": (
        "COPY requirements.txt .\n"
        "RUN pip install --no-cache-dir --prefix=/install -r requirements.txt\n"
    ),
    "pipenv": (
        "COPY Pipfile Pipfile.lock* ./\n"
        "RUN pip install --no-cache-dir pipenv && pipenv install --system --deploy\n"
    ),
    "pyproject": (
        "# Prefer lockfile when present (uv > poetry > pdm)\n"
        "COPY pyproject.toml ./\n"
        "COPY uv.lock* poetry.lock* pdm.lock* ./\n"
        "RUN if [ -f uv.lock ]; then pip install --no-cache-dir uv && uv sync --frozen --no-dev; "
        "elif [ -f poetry.lock ]; then pip install --no-cache-dir poetry && poetry install --no-dev --no-interaction; "
        "elif [ -f pdm.lock ]; then pip install --no-cache-dir pdm && pdm install --prod; "
        "else pip install --no-cache-dir .; fi\n"
    ),
}


def resolve_python_install_block(project_root: str | Path, manager: str) -> str:
    """Return the canonical install snippet for prompts; falls back to pip-style."""
    root = Path(project_root)
    m = (manager or "pip").lower()
    if m == "pip" and (root / "requirements.txt").is_file():
        return PYTHON_INSTALL_BLOCKS["pip"]
    if m == "pipenv" and (root / "Pipfile").is_file():
        return PYTHON_INSTALL_BLOCKS["pipenv"]
    if (root / "pyproject.toml").is_file():
        return PYTHON_INSTALL_BLOCKS["pyproject"]
    return PYTHON_INSTALL_BLOCKS["pip"]


def format_matrix_for_prompt(project_root: str, fingerprint: dict | None) -> str:
    """Compact block embedded in Dockerfile generation / fix prompts."""
    fp = fingerprint or {}
    deps = fp.get("dependencies") or {}
    manager = str(deps.get("manager") or "pip")
    block = resolve_python_install_block(project_root, manager)
    return (
        "## Python dependency install matrix (canonical; follow for RUN/COPY)\n"
        f"- Detected manager: `{manager}`\n"
        "```dockerfile\n"
        f"{block}"
        "```\n"
    )
