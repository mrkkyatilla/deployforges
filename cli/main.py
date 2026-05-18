from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import click
import httpx

from cli.api_client import DeployForgeClient
from cli.config import get_api_endpoint, get_api_key, load_config, save_config


SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def _detect_source_type(source: str) -> tuple[str, str]:
    parsed = urlparse(source)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return "git_url", source

    path = Path(source).expanduser().resolve()
    if path.is_dir():
        return "local_dir", str(path)
    if path.is_file():
        return "file", str(path)

    if source.endswith(".git") or "@" in source:
        return "git_url", source

    raise click.BadParameter(
        f"Kaynak bulunamadı: {source}\n"
        "Bir git URL'si, dizin yolu veya dosya yolu belirtin."
    )


def _zip_directory(directory: str) -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        base = Path(directory)
        for file_path in sorted(base.rglob("*")):
            if file_path.is_file() and ".git" not in file_path.parts:
                zf.write(file_path, file_path.relative_to(base))
    return tmp.name


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes}B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    return f"{size_bytes / (1024 * 1024):.1f}MB"


def _print_step(status: str, message: str, is_error: bool = False) -> None:
    if status == "pending":
        click.echo(f"  ⠋ {message}", nl=False)
        click.echo("\r", nl=False)
        click.echo(f"  ⠋ {message}")
    elif status == "success":
        click.echo(f"  ✓ {message}")
    elif status == "error":
        click.secho(f"  ✗ {message}", fg="red")
    elif status == "info":
        click.echo(f"  ℹ {message}")


def _handle_event(event: dict[str, Any], fmt: str) -> str | None:
    evt_type = event.get("event", "message")
    data = event.get("data", {})

    if fmt == "quiet":
        return None

    if fmt == "json":
        click.echo(json.dumps(event, ensure_ascii=False))
        return None

    status = data.get("status", "")
    message = data.get("message", "")
    phase = data.get("phase", "")

    if evt_type == "progress":
        if status == "running":
            _print_step("pending", message)
        elif status == "completed":
            _print_step("success", message)
        elif status == "failed":
            _print_step("error", message)

    elif evt_type == "analysis":
        lang = data.get("language", "")
        framework = data.get("framework", "")
        version = data.get("version", "")
        if lang:
            _print_step("success", f"{lang} {version} / {framework} tespit edildi")

    elif evt_type == "build":
        attempt = data.get("attempt", 1)
        max_attempts = data.get("max_attempts", 5)
        if status == "running":
            _print_step("pending", f"Build ediliyor... (deneme {attempt}/{max_attempts})")
        elif status == "completed":
            size = _format_size(data.get("image_size", 0))
            duration = data.get("duration_seconds", 0)
            _print_step("success", f"Build başarılı ({size}, {duration}s)")
        elif status == "failed":
            error_msg = data.get("error", "Bilinmeyen hata")
            _print_step("error", f"Build başarısız — {error_msg}")

    elif evt_type == "fix":
        attempt = data.get("attempt", 1)
        max_attempts = data.get("max_attempts", 5)
        _print_step("pending", f"Düzeltiliyor... (deneme {attempt}/{max_attempts})")

    elif evt_type == "deploy_test":
        if status == "running":
            _print_step("pending", "Test ortamına deploy ediliyor...")
        elif status == "completed":
            _print_step("success", "Health check geçti")

    elif evt_type == "done":
        return "done"

    elif evt_type == "error":
        _print_step("error", message or "Beklenmeyen bir hata oluştu")
        return "error"

    return None


def _print_summary(result: dict[str, Any]) -> None:
    click.echo()
    click.echo("  ══════════════════════════════════")

    status = result.get("status", "unknown")
    if status == "success":
        click.secho("  ✅ HAZIR — Bu proje deploy edilebilir!", fg="green", bold=True)
    else:
        click.secho("  ❌ BAŞARISIZ — Deploy tamamlanamadı.", fg="red", bold=True)

    click.echo()

    dockerfile_path = result.get("dockerfile_path")
    if dockerfile_path:
        click.echo(f"  Dockerfile:  {dockerfile_path}")

    test_url = result.get("test_url")
    if test_url:
        ttl = result.get("test_url_ttl", "5dk")
        click.echo(f"  Test URL:    {test_url} ({ttl} geçerli)")

    cost = result.get("cost")
    attempts = result.get("total_attempts", 1)
    if cost is not None:
        click.echo(f"  Maliyet:     ${cost:.2f} ({attempts} build denemesi)")

    click.echo("  ══════════════════════════════════")
    click.echo()


@click.group()
@click.version_option(version="0.3.0", prog_name="deployforge")
def cli() -> None:
    """DeployForge CLI — AI destekli evrensel proje deploy aracı."""


@cli.group()
def auth() -> None:
    """Kimlik doğrulama komutları."""


@auth.command()
def login() -> None:
    """API anahtarı ile giriş yap."""
    api_key = click.prompt("API Key", hide_input=True)
    if not api_key.strip():
        raise click.ClickException("API anahtarı boş olamaz.")

    config = load_config()
    endpoint = config.get("api_endpoint", "https://api.deployforge.dev")

    click.echo("  ⠋ API anahtarı doğrulanıyor...")
    client = DeployForgeClient(api_key=api_key.strip(), endpoint=endpoint)
    try:
        if not client.check_health():
            raise click.ClickException(
                "API anahtarı doğrulanamadı. Anahtarınızı kontrol edin."
            )
    except httpx.ConnectError:
        raise click.ClickException(
            f"API sunucusuna bağlanılamadı: {endpoint}\n"
            "Ağ bağlantınızı veya endpoint ayarınızı kontrol edin."
        )
    finally:
        client.close()

    config["api_key"] = api_key.strip()
    save_config(config)
    click.secho("  ✓ Giriş başarılı! API anahtarı kaydedildi.", fg="green")


@cli.command()
@click.argument("source")
@click.option("--branch", "-b", default="main", help="Git dalı.")
@click.option("--commit", "-c", default=None, help="Belirli bir commit hash.")
@click.option("--output", "-o", default=None, help="Dockerfile kayıt yolu.")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["human", "json", "quiet"]),
    default=None,
    help="Çıktı formatı.",
)
@click.option("--max-attempts", default=5, help="Maksimum build deneme sayısı.", show_default=True)
@click.option(
    "--api-version",
    type=click.Choice(["v1", "v2"]),
    default="v1",
    help="API sürümü (v2 = DeploymentManifest).",
)
def deploy(
    source: str,
    branch: str,
    commit: str | None,
    output: str | None,
    fmt: str | None,
    max_attempts: int,
    api_version: str,
) -> None:
    """Bir projeyi analiz et, Dockerfile üret ve deploy et.

    SOURCE bir git URL'si, yerel dizin veya dosya yolu olabilir.
    v2 API ile DeploymentManifest (multi-service) döner.
    """
    config = load_config()
    api_key = get_api_key()
    endpoint = get_api_endpoint()
    fmt = fmt or config.get("format", "human")
    output = output or config.get("default_output", "./Dockerfile")

    source_type, source_value = _detect_source_type(source)

    zip_path: str | None = None
    if source_type == "local_dir":
        if fmt == "human":
            _print_step("pending", "Proje arşivleniyor...")
        zip_path = _zip_directory(source_value)
        source_value = zip_path
        source_type = "archive"
        if fmt == "human":
            _print_step("success", "Proje arşivlendi")

    with DeployForgeClient(
        api_key=api_key, endpoint=endpoint, api_version=api_version,
    ) as client:
        try:
            if fmt == "human":
                _print_step("pending", "Proje alınıyor...")

            project = client.create_project(
                source_type=source_type,
                source_url=source_value,
                branch=branch,
                commit=commit,
                options={"max_attempts": max_attempts},
            )
            project_id = project["id"]

            if fmt == "human":
                file_count = project.get("file_count", "?")
                size = _format_size(project.get("size_bytes", 0))
                _print_step("success", f"Proje alındı ({file_count} dosya, {size})")

            final_status = None
            for event in client.stream_events(project_id):
                result = _handle_event(event, fmt)
                if result in ("done", "error"):
                    final_status = result
                    break

            result_data = client.get_result(project_id)

            if fmt == "json":
                click.echo(json.dumps(result_data, indent=2, ensure_ascii=False))
            elif fmt == "quiet":
                sys.exit(0 if result_data.get("status") == "success" else 1)
            else:
                _print_summary(result_data)

                dockerfile_content = result_data.get("dockerfile")
                if dockerfile_content and output:
                    out_path = Path(output)
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_text(dockerfile_content)
                    click.echo(f"  Dockerfile kaydedildi: {out_path}")

                if result_data.get("status") != "success":
                    suggestions = result_data.get("suggestions", [])
                    if suggestions:
                        click.echo()
                        click.secho("  Öneriler:", fg="yellow", bold=True)
                        for s in suggestions:
                            click.echo(f"    • {s}")
                    sys.exit(1)

        except httpx.HTTPStatusError as exc:
            _handle_http_error(exc)
        finally:
            if zip_path:
                Path(zip_path).unlink(missing_ok=True)


@cli.command()
@click.argument("source")
def analyze(source: str) -> None:
    """Bir projeyi analiz et (build yapmadan).

    SOURCE bir git URL'si, yerel dizin veya dosya yolu olabilir.
    Sadece analiz yapar, build denemez — daha hızlı ve ucuz.
    """
    config = load_config()
    api_key = get_api_key()
    endpoint = get_api_endpoint()
    fmt = config.get("format", "human")

    source_type, source_value = _detect_source_type(source)

    zip_path: str | None = None
    if source_type == "local_dir":
        if fmt == "human":
            _print_step("pending", "Proje arşivleniyor...")
        zip_path = _zip_directory(source_value)
        source_value = zip_path
        source_type = "archive"
        if fmt == "human":
            _print_step("success", "Proje arşivlendi")

    with DeployForgeClient(api_key=api_key, endpoint=endpoint) as client:
        try:
            if fmt == "human":
                _print_step("pending", "Proje alınıyor...")

            project = client.create_project(
                source_type=source_type,
                source_url=source_value,
                options={"skip_deploy_test": True, "max_attempts": 0},
            )
            project_id = project["id"]

            if fmt == "human":
                _print_step("success", "Proje alındı")
                _print_step("pending", "Analiz ediliyor...")

            for event in client.stream_events(project_id):
                result = _handle_event(event, fmt)
                if result in ("done", "error"):
                    break

            result_data = client.get_result(project_id)

            if fmt == "json":
                click.echo(json.dumps(result_data, indent=2, ensure_ascii=False))
            elif fmt == "human":
                analysis = result_data.get("analysis", {})
                click.echo()
                click.echo("  ══════════════════════════════════")
                click.secho("  📋 Analiz Sonucu", bold=True)
                click.echo()

                lang = analysis.get("language", "Bilinmiyor")
                version = analysis.get("version", "")
                framework = analysis.get("framework", "")
                click.echo(f"  Dil:        {lang} {version}")
                if framework:
                    click.echo(f"  Framework:  {framework}")

                deps = analysis.get("dependency_count", 0)
                click.echo(f"  Bağımlılık: {deps} paket")

                entry = analysis.get("entrypoint", "")
                if entry:
                    click.echo(f"  Giriş:     {entry}")

                port = analysis.get("port")
                if port:
                    click.echo(f"  Port:       {port}")

                dockerfile = result_data.get("dockerfile")
                if dockerfile:
                    click.echo()
                    click.secho("  Üretilen Dockerfile:", bold=True)
                    click.echo()
                    for line in dockerfile.splitlines():
                        click.echo(f"    {line}")

                click.echo("  ══════════════════════════════════")
                click.echo()

        except httpx.HTTPStatusError as exc:
            _handle_http_error(exc)
        finally:
            if zip_path:
                Path(zip_path).unlink(missing_ok=True)


@cli.command()
def usage() -> None:
    """Kullanım bilgileri ve bakiye göster."""
    api_key = get_api_key()
    endpoint = get_api_endpoint()

    with DeployForgeClient(api_key=api_key, endpoint=endpoint) as client:
        try:
            data = client.get_usage()
        except httpx.HTTPStatusError as exc:
            _handle_http_error(exc)

    click.echo()
    click.echo("  ══════════════════════════════════")
    click.secho("  💰 Kullanım Bilgileri", bold=True)
    click.echo()

    credits_used = data.get("credits_used", 0)
    credits_remaining = data.get("credits_remaining", 0)
    total_cost = data.get("total_cost", 0)
    project_count = data.get("project_count", 0)
    period = data.get("billing_period", "")

    click.echo(f"  Kullanılan:  {credits_used} kredi")
    click.echo(f"  Kalan:       {credits_remaining} kredi")
    click.echo(f"  Toplam:      ${total_cost:.2f}")
    click.echo(f"  Proje sayısı: {project_count}")
    if period:
        click.echo(f"  Dönem:       {period}")

    click.echo("  ══════════════════════════════════")
    click.echo()


@cli.command()
@click.option("--limit", default=10, help="Gösterilecek proje sayısı.", show_default=True)
def history(limit: int) -> None:
    """Geçmiş projeleri listele."""
    api_key = get_api_key()
    endpoint = get_api_endpoint()

    with DeployForgeClient(api_key=api_key, endpoint=endpoint) as client:
        try:
            resp = client._client.get("/api/v1/projects", params={"limit": limit})
            resp.raise_for_status()
            projects = resp.json()
        except httpx.HTTPStatusError as exc:
            _handle_http_error(exc)

    if not projects:
        click.echo("  Henüz proje bulunmuyor.")
        return

    click.echo()
    click.echo("  ══════════════════════════════════")
    click.secho("  📜 Proje Geçmişi", bold=True)
    click.echo()

    items = projects if isinstance(projects, list) else projects.get("items", [])
    for p in items:
        pid = p.get("id", "?")[:8]
        status_icon = "✓" if p.get("status") == "success" else "✗"
        source = p.get("source_url", "?")
        created = p.get("created_at", "?")
        cost = p.get("cost", 0)
        click.echo(f"  {status_icon} [{pid}] {source}")
        click.echo(f"           {created}  ${cost:.2f}")

    click.echo("  ══════════════════════════════════")
    click.echo()


def _handle_http_error(exc: httpx.HTTPStatusError) -> None:
    status = exc.response.status_code
    if status == 401:
        raise click.ClickException(
            "Kimlik doğrulama başarısız (401). API anahtarınızı kontrol edin:\n"
            "  deployforge auth login"
        )
    if status == 403:
        raise click.ClickException("Bu işlem için yetkiniz yok (403).")
    if status == 404:
        raise click.ClickException("Kaynak bulunamadı (404).")
    if status == 429:
        raise click.ClickException(
            "Rate limit aşıldı (429). Lütfen biraz bekleyip tekrar deneyin."
        )

    try:
        detail = exc.response.json().get("detail", exc.response.text)
    except Exception:
        detail = exc.response.text
    raise click.ClickException(f"API hatası ({status}): {detail}")


if __name__ == "__main__":
    cli()
