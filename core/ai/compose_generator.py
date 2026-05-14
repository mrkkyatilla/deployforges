from __future__ import annotations

import copy
import logging
from dataclasses import dataclass

import yaml

from api.config import settings
from core.ai.gemini_client import GeminiClient
from core.ai.gemini_json_repair import repair_model_json
from core.ai.gemini_schemas import schema_compose_generation, schema_compose_service_patches
from core.ai.json_response import parse_model_json_from_ai_response
from core.ai.token_manager import TokenBudget, estimate_tokens, select_model_for_step
from core.security.compose_policy import check_compose_policy

logger = logging.getLogger(__name__)

_KNOWN_DB_CONFIGS: dict[str, dict] = {
    "postgres": {
        "image": "postgres:16-alpine",
        "environment": {
            "POSTGRES_USER": "${DB_USER:-postgres}",
            "POSTGRES_PASSWORD": "${DB_PASSWORD:-postgres}",
            "POSTGRES_DB": "${DB_NAME:-app}",
        },
        "volumes": ["pgdata:/var/lib/postgresql/data"],
        "healthcheck": {
            "test": ["CMD-SHELL", "pg_isready -U ${DB_USER:-postgres}"],
            "interval": "10s",
            "timeout": "5s",
            "retries": 5,
        },
        "port": 5432,
        "volume_name": "pgdata",
    },
    "mysql": {
        "image": "mysql:8",
        "environment": {
            "MYSQL_ROOT_PASSWORD": "${DB_ROOT_PASSWORD:-root}",
            "MYSQL_DATABASE": "${DB_NAME:-app}",
            "MYSQL_USER": "${DB_USER:-app}",
            "MYSQL_PASSWORD": "${DB_PASSWORD:-app}",
        },
        "volumes": ["mysqldata:/var/lib/mysql"],
        "healthcheck": {
            "test": ["CMD", "mysqladmin", "ping", "-h", "localhost"],
            "interval": "10s",
            "timeout": "5s",
            "retries": 5,
        },
        "port": 3306,
        "volume_name": "mysqldata",
    },
    "mongodb": {
        "image": "mongo:7",
        "environment": {
            "MONGO_INITDB_ROOT_USERNAME": "${MONGO_USER:-root}",
            "MONGO_INITDB_ROOT_PASSWORD": "${MONGO_PASSWORD:-root}",
        },
        "volumes": ["mongodata:/data/db"],
        "healthcheck": {
            "test": ["CMD", "mongosh", "--eval", "db.adminCommand('ping')"],
            "interval": "10s",
            "timeout": "5s",
            "retries": 5,
        },
        "port": 27017,
        "volume_name": "mongodata",
    },
    "redis": {
        "image": "redis:7-alpine",
        "volumes": ["redisdata:/data"],
        "healthcheck": {
            "test": ["CMD", "redis-cli", "ping"],
            "interval": "10s",
            "timeout": "5s",
            "retries": 5,
        },
        "port": 6379,
        "volume_name": "redisdata",
    },
}

_FRONTEND_FRAMEWORKS = frozenset({
    "react", "vue", "angular", "svelte", "nextjs", "nuxt", "gatsby",
})

_API_FRAMEWORKS = frozenset({
    "express", "fastapi", "flask", "django", "rails", "spring",
    "gin", "fiber", "echo", "actix", "nestjs", "koa", "hapi",
    "fastify", "laravel", "phoenix",
})


@dataclass
class ComposeResult:
    compose_yml: str
    warnings: list[str]
    tokens_used: int
    services_included: list[str]


class ComposeGenerator:
    """Generates docker-compose.yml for multi-service projects."""

    def __init__(self) -> None:
        self._gemini = GeminiClient()

    async def generate(
        self,
        services: list[dict],
        fingerprints: dict[str, dict],
        token_budget: TokenBudget,
    ) -> ComposeResult:
        warnings: list[str] = []
        service_names = [s["name"] for s in services]

        non_db_services = [
            s for s in services if s.get("type") != "database"
        ]
        db_services = [s for s in services if s.get("type") == "database"]

        pattern = self._classify_pattern(non_db_services, db_services)
        logger.info("Compose pattern: %s (%d services)", pattern, len(services))

        if pattern != "complex":
            compose_yml = self._generate_template(
                services, fingerprints, pattern, warnings,
            )
            return ComposeResult(
                compose_yml=compose_yml,
                warnings=warnings,
                tokens_used=0,
                services_included=service_names,
            )

        can_spend, allowed = token_budget.can_spend("compose", minimum=2000)
        if not can_spend:
            logger.warning(
                "Insufficient token budget for AI compose generation; "
                "falling back to template",
            )
            warnings.append(
                "Token budget too low for AI generation; used template fallback",
            )
            compose_yml = self._generate_template(
                services, fingerprints, "generic", warnings,
            )
            return ComposeResult(
                compose_yml=compose_yml,
                warnings=warnings,
                tokens_used=0,
                services_included=service_names,
            )

        compose_yml, tokens_used = await self._generate_with_ai(
            services, fingerprints, allowed, warnings, token_budget,
        )
        pol = check_compose_policy(compose_yml)
        if pol:
            for v in pol:
                warnings.append(f"{v.code}: {v.message} ({v.path})")
            if settings.compose_policy_fail_on_violations:
                warnings.append("Compose policy failed; reverting to template-only compose")
                compose_yml = self._generate_template(
                    services, fingerprints, "generic", warnings,
                )
        token_budget.record("compose", tokens_used)

        return ComposeResult(
            compose_yml=compose_yml,
            warnings=warnings,
            tokens_used=tokens_used,
            services_included=service_names,
        )

    # ------------------------------------------------------------------
    # Pattern classification
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_pattern(
        non_db: list[dict],
        db: list[dict],
    ) -> str:
        if len(non_db) + len(db) <= 2:
            types = {s.get("type") for s in non_db}
            if types <= {"web", "api"} and len(db) <= 1:
                return "simple"
        if len(non_db) <= 2 and len(db) <= 1:
            return "simple"
        return "complex"

    # ------------------------------------------------------------------
    # Template-based generation
    # ------------------------------------------------------------------

    def _generate_template(
        self,
        services: list[dict],
        fingerprints: dict[str, dict],
        pattern: str,
        warnings: list[str],
    ) -> str:
        compose: dict = {"services": {}}
        volumes: dict[str, dict | None] = {}
        has_frontend = False
        api_name: str | None = None
        db_name: str | None = None

        for svc in services:
            name = svc["name"]
            svc_type = svc.get("type", "unknown")
            port = svc.get("port")
            root_path = svc.get("root_path", ".")
            depends = svc.get("depends_on", [])
            fp = fingerprints.get(name, {})

            if svc_type == "database":
                db_config = self._build_db_service(name, svc, fp)
                if db_config:
                    compose["services"][name] = db_config["service"]
                    if db_config.get("volume_name"):
                        volumes[db_config["volume_name"]] = None
                    db_name = name
                continue

            service_def: dict = {
                "build": {
                    "context": f"./{root_path}" if root_path != "." else ".",
                    "dockerfile": "Dockerfile",
                },
                "restart": "unless-stopped",
            }

            if port:
                service_def["ports"] = [f"{port}:{port}"]

            env = self._build_env_vars(svc, fp, db_name)
            if env:
                service_def["environment"] = env

            if depends:
                dep_block: dict[str, dict] = {}
                for dep in depends:
                    dep_block[dep] = {"condition": "service_healthy"}
                service_def["depends_on"] = dep_block

            compose["services"][name] = service_def

            framework = svc.get("framework", "")
            if framework and framework.lower() in _FRONTEND_FRAMEWORKS:
                has_frontend = True
            if framework and framework.lower() in _API_FRAMEWORKS:
                api_name = name

        if has_frontend and api_name and db_name:
            for name, svc_def in compose["services"].items():
                svc = next((s for s in services if s["name"] == name), None)
                if not svc:
                    continue
                fw = (svc.get("framework") or "").lower()
                if fw in _FRONTEND_FRAMEWORKS and api_name not in svc.get("depends_on", []):
                    existing = svc_def.get("depends_on", {})
                    existing[api_name] = {"condition": "service_started"}
                    svc_def["depends_on"] = existing

            if api_name in compose["services"]:
                api_def = compose["services"][api_name]
                existing = api_def.get("depends_on", {})
                if db_name not in existing:
                    existing[db_name] = {"condition": "service_healthy"}
                    api_def["depends_on"] = existing

        if volumes:
            compose["volumes"] = {k: v for k, v in volumes.items()}

        compose["networks"] = {
            "default": {
                "driver": "bridge",
            },
        }

        yml = yaml.dump(
            compose, default_flow_style=False, sort_keys=False, width=120,
        )
        return yml

    @staticmethod
    def _build_db_service(
        name: str,
        svc: dict,
        fp: dict,
    ) -> dict | None:
        framework = (svc.get("framework") or name).lower()
        for db_key, config in _KNOWN_DB_CONFIGS.items():
            if db_key in framework or db_key in name.lower():
                service_def: dict = {
                    "image": config["image"],
                    "restart": "unless-stopped",
                }
                if "environment" in config:
                    service_def["environment"] = dict(config["environment"])
                if "volumes" in config:
                    service_def["volumes"] = list(config["volumes"])
                if "healthcheck" in config:
                    service_def["healthcheck"] = dict(config["healthcheck"])
                port = config["port"]
                service_def["ports"] = [f"{port}:{port}"]
                return {
                    "service": service_def,
                    "volume_name": config.get("volume_name"),
                }
        return None

    @staticmethod
    def _build_env_vars(
        svc: dict,
        fp: dict,
        db_name: str | None,
    ) -> dict[str, str]:
        env: dict[str, str] = {}
        env["NODE_ENV"] = "production"

        svc_type = svc.get("type", "unknown")
        if svc_type in ("api", "web") and svc.get("port"):
            env["PORT"] = str(svc["port"])

        if db_name:
            env["DATABASE_HOST"] = db_name
            env["DATABASE_PORT"] = "5432"

        return env

    @staticmethod
    def _merge_compose_patches(base: dict, patches: list[dict]) -> None:
        services = base.setdefault("services", {})
        for p in patches:
            if not isinstance(p, dict):
                continue
            name = p.get("service")
            if not name or not isinstance(name, str) or name not in services:
                continue
            tgt = services[name]
            env_pairs = p.get("environment") or []
            if isinstance(env_pairs, list) and env_pairs:
                env = dict(tgt.get("environment") or {})
                for pair in env_pairs:
                    if isinstance(pair, dict) and pair.get("name"):
                        env[str(pair["name"])] = str(pair.get("value", ""))
                tgt["environment"] = env
            ports = p.get("ports")
            if isinstance(ports, list) and ports:
                existing = [str(x) for x in (tgt.get("ports") or [])]
                tgt["ports"] = existing + [str(x) for x in ports]

    @staticmethod
    def _build_compose_patch_prompt(
        services: list[dict],
        fingerprints: dict[str, dict],
    ) -> str:
        lines = [
            "A deterministic docker-compose template was already generated in code.",
            "Return **only** JSON per schema: `patches` (array) and `warnings` (array).",
            "Each patch targets an existing `service` name and may add `environment` name/value pairs "
            "and/or extra `ports` entries. Do not invent new top-level services.",
            "",
            "Services:",
        ]
        for svc in services:
            name = svc["name"]
            fp = fingerprints.get(name, {})
            lines.append(f"- {name}: type={svc.get('type')} port={svc.get('port')} root={svc.get('root_path', '.')}")
            lines.append(f"  fingerprint subset: language={fp.get('language')} framework={fp.get('framework')}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # AI-based generation
    # ------------------------------------------------------------------

    async def _generate_with_ai_json_patch(
        self,
        services: list[dict],
        fingerprints: dict[str, dict],
        max_tokens: int,
        warnings: list[str],
        token_budget: TokenBudget,
    ) -> tuple[str, int]:
        base_yml = self._generate_template(services, fingerprints, "generic", warnings)
        try:
            base = yaml.safe_load(base_yml) or {}
        except yaml.YAMLError:
            base = {"services": {}}
        if not isinstance(base, dict):
            base = {"services": {}}

        prompt = self._build_compose_patch_prompt(services, fingerprints)
        prompt_tokens = estimate_tokens(prompt)
        output_limit = min(max_tokens - prompt_tokens, 4096)
        if output_limit < 800:
            output_limit = 800

        model = select_model_for_step("compose")
        system_instruction = (
            "You output compact JSON only. Patches merge into an existing compose dict produced by DeployForge."
        )
        schema = schema_compose_service_patches()
        try:
            response = await self._gemini.generate_json(
                prompt=prompt,
                system_instruction=system_instruction,
                model=model,
                temperature=0.1,
                max_output_tokens=output_limit,
                response_schema=schema,
                io_log_label="compose_generation_patch",
            )
        except Exception:
            logger.error("AI compose patch generation failed", exc_info=True)
            warnings.append("AI compose patch failed; using template compose only")
            return base_yml, 0

        pr = parse_model_json_from_ai_response(response.text, response.parsed_dict)
        data = pr.data
        total_tok = response.total_tokens
        repair_resp = None
        if data is None:
            hint = "patches (array of objects with service, optional environment array, optional ports array), warnings"
            repaired, repair_resp = await repair_model_json(
                self._gemini,
                broken_text=response.text,
                key_hint=hint,
                token_budget=token_budget,
                spend_step="compose",
                response_schema=schema,
                io_log_label="compose_generation_patch",
                parsed_dict=response.parsed_dict,
            )
            if repaired is not None:
                data = repaired
                if repair_resp is not None:
                    total_tok += repair_resp.total_tokens

        if not isinstance(data, dict):
            warnings.append("Compose patch JSON invalid; using template only")
            return base_yml, total_tok

        raw_patches = data.get("patches")
        patches = raw_patches if isinstance(raw_patches, list) else []
        for w in data.get("warnings") or []:
            if isinstance(w, str):
                warnings.append(w)

        merged = copy.deepcopy(base)
        self._merge_compose_patches(merged, patches)
        yml = yaml.dump(merged, default_flow_style=False, sort_keys=False, width=120)
        try:
            yaml.safe_load(yml)
        except yaml.YAMLError:
            warnings.append("Merged compose YAML may have syntax issues")
        return yml, total_tok

    async def _generate_with_ai(
        self,
        services: list[dict],
        fingerprints: dict[str, dict],
        max_tokens: int,
        warnings: list[str],
        token_budget: TokenBudget,
    ) -> tuple[str, int]:
        if settings.ai_compose_json_patch_enabled:
            return await self._generate_with_ai_json_patch(
                services, fingerprints, max_tokens, warnings, token_budget,
            )
        prompt = self._build_ai_prompt(services, fingerprints)
        prompt_tokens = estimate_tokens(prompt)
        output_limit = min(max_tokens - prompt_tokens, 8192)
        if output_limit < 1000:
            output_limit = 2000

        model = select_model_for_step("compose")
        system_instruction = (
            "You are an expert DevOps engineer. Generate production-ready "
            "docker-compose.yml configurations. Return valid JSON with two "
            'fields: "compose_yml" (a string containing valid YAML for '
            'docker-compose) and "warnings" (a list of strings with any '
            "caveats or recommendations)."
        )
        schema = schema_compose_generation()

        try:
            response = await self._gemini.generate_json(
                prompt=prompt,
                system_instruction=system_instruction,
                model=model,
                temperature=0.1,
                max_output_tokens=output_limit,
                response_schema=schema,
                io_log_label="compose_generation",
            )
        except Exception:
            logger.error("AI compose generation failed", exc_info=True)
            warnings.append(
                "AI generation failed; fell back to template-based generation",
            )
            yml = self._generate_template(
                services, fingerprints, "generic", warnings,
            )
            return yml, 0

        pr = parse_model_json_from_ai_response(response.text, response.parsed_dict)
        data = pr.data
        total_tok = response.total_tokens
        repair_resp = None

        if data is None:
            hint = 'compose_yml (string, YAML body), warnings (array of strings)'
            repaired, repair_resp = await repair_model_json(
                self._gemini,
                broken_text=response.text,
                key_hint=hint,
                token_budget=token_budget,
                spend_step="compose",
                response_schema=schema,
                io_log_label="compose_generation",
                parsed_dict=response.parsed_dict,
            )
            if repaired is not None:
                data = repaired
                if repair_resp is not None:
                    total_tok += repair_resp.total_tokens

        if not isinstance(data, dict):
            logger.warning("Failed to parse AI compose response as JSON")
            warnings.append("AI compose JSON parse failed; using template fallback")
            yml = self._generate_template(
                services, fingerprints, "generic", warnings,
            )
            return yml, total_tok

        compose_yml = str(data.get("compose_yml", "") or "")
        ai_warnings = data.get("warnings", [])
        if isinstance(ai_warnings, list):
            warnings.extend(str(w) for w in ai_warnings)

        if not compose_yml.strip():
            warnings.append("Empty compose_yml from AI; using template fallback")
            compose_yml = self._generate_template(
                services, fingerprints, "generic", warnings,
            )
            return compose_yml, total_tok

        try:
            yaml.safe_load(compose_yml)
        except yaml.YAMLError:
            warnings.append("Generated compose YAML may have syntax issues")

        return compose_yml, total_tok

    @staticmethod
    def _build_ai_prompt(
        services: list[dict],
        fingerprints: dict[str, dict],
    ) -> str:
        svc_descriptions: list[str] = []
        for svc in services:
            name = svc["name"]
            fp = fingerprints.get(name, {})
            lang = fp.get("language", {})
            fw = fp.get("framework", {})
            port_info = fp.get("port", {})
            env_info = fp.get("environment", {})

            lang_primary = (
                lang.get("primary", "unknown") if isinstance(lang, dict) else lang
            )
            fw_name = fw.get("name", "none") if isinstance(fw, dict) else fw
            port_val = (
                port_info.get("value", "unknown")
                if isinstance(port_info, dict)
                else port_info
            )
            has_env = (
                env_info.get("requires_env_vars", False)
                if isinstance(env_info, dict)
                else False
            )

            desc = (
                f"- {name}:\n"
                f"    type: {svc.get('type', 'unknown')}\n"
                f"    root_path: {svc.get('root_path', '.')}\n"
                f"    language: {lang_primary}\n"
                f"    framework: {fw_name}\n"
                f"    port: {port_val}\n"
                f"    has_env_vars: {has_env}\n"
                f"    depends_on: {svc.get('depends_on', [])}"
            )
            svc_descriptions.append(desc)

        return (
            "Generate a production-ready docker-compose.yml for a multi-service project.\n\n"
            "Services:\n"
            + "\n".join(svc_descriptions)
            + "\n\n"
            "Requirements:\n"
            "- Each service with a root_path should have a build context "
            "pointing to that directory\n"
            "- Include appropriate port mappings\n"
            "- Include depends_on with health checks where applicable\n"
            "- Add environment variables with sensible defaults using ${VAR:-default} syntax\n"
            "- Add named volumes for any databases\n"
            "- Use a bridge network\n"
            "- Add restart: unless-stopped to all services\n"
            "- Add healthchecks for databases\n"
            "- Add helpful inline YAML comments\n\n"
            'Return JSON: {"compose_yml": "<yaml string>", "warnings": ["..."]}'
        )
