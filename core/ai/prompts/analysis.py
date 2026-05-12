import json

AI_ANALYSIS_SYSTEM_PROMPT = """\
You are a Senior Software Architect analyzing a project's source code structure. \
Your goal is to determine the correct technology stack, framework, entry point, \
and runtime requirements to containerize this project.

## Rules

1. Only report what you can confidently determine from the provided files.
2. Do not guess or hallucinate package names, versions, or configurations.
3. If uncertain, say so in the warnings field.

## Output Format

Respond ONLY with valid JSON:
{
  "language": "python",
  "language_version": "3.11",
  "framework": "fastapi",
  "framework_version": "0.104.1",
  "entrypoint_file": "main.py",
  "start_command": "uvicorn main:app --host 0.0.0.0 --port 8000",
  "port": 8000,
  "additional_system_deps": ["libpq-dev"],
  "warnings": [],
  "confidence": 0.9
}
"""


def build_ai_analysis_prompt(file_tree: list[str], critical_files: dict[str, str]) -> str:
    sections = ["## Project File Tree\n"]
    sections.append("```")
    sections.append("\n".join(file_tree[:200]))
    if len(file_tree) > 200:
        sections.append(f"... and {len(file_tree) - 200} more files")
    sections.append("```\n")

    sections.append("## Critical File Contents\n")
    for filename, content in critical_files.items():
        truncated = content[:3000]
        if len(content) > 3000:
            truncated += "\n... (truncated)"
        sections.append(f"### {filename}\n```\n{truncated}\n```\n")

    sections.append(
        "## Task\n"
        "Analyze this project and determine the technology stack, framework, "
        "entry point, start command, port, and any system dependencies needed "
        "for containerization. Respond with valid JSON."
    )

    return "\n".join(sections)
