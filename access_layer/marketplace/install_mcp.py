"""把 MCP Registry server.json 译成本地 stdio/http 配置。"""

from __future__ import annotations

from shutil import which
from typing import Any

from access_layer.skills.archive import normalize_imported_skill_id
from access_layer.marketplace.match import last_segment


def map_install(server: dict[str, Any]) -> dict[str, Any]:
    """返回 installPreview + 可写入 mcp.json 的草稿字段。"""

    if "server_config" in server:
        return _map_modelscope(server)

    remotes = [item for item in (server.get("remotes") or []) if isinstance(item, dict)]
    http_remote = _pick_remote(remotes)
    if http_remote is not None:
        return _map_http(server, http_remote)

    packages = [item for item in (server.get("packages") or []) if isinstance(item, dict)]
    npm = next((item for item in packages if str(item.get("registryType") or "") == "npm"), None)
    if npm is not None:
        return _map_npm(server, npm)
    pypi = next((item for item in packages if str(item.get("registryType") or "") == "pypi"), None)
    if pypi is not None:
        return _map_pypi(server, pypi)
    if packages:
        return _blocked(server, "unsupported_package")
    return _blocked(server, "no_install_target")


def suggested_local_id(source_id: str, override: str | None = None) -> str:
    return normalize_imported_skill_id(override or last_segment(source_id.lstrip("@")))


def _map_modelscope(server: dict[str, Any]) -> dict[str, Any]:
    _name, cfg = _first_modelscope_config(server)
    title = _modelscope_title(server)
    description = str(server.get("description") or "")
    if not cfg:
        return _blocked({**server, "title": title}, "no_install_target")
    fields = _schema_fields(server.get("env_schema") if isinstance(server.get("env_schema"), dict) else {})
    env_in_cfg = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
    for key, value in env_in_cfg.items():
        if any(item["key"] == key for item in fields):
            continue
        fields.append(
            {
                "key": str(key),
                "kind": "env",
                "description": "",
                "required": not str(value or "").strip(),
                "secret": True,
                "default": None if value in (None, "") else str(value),
            }
        )
    env_keys = [item["key"] for item in fields]
    required = [item["key"] for item in fields if item["required"] and not item.get("default")]
    url = str(cfg.get("url") or "").strip()
    command = str(cfg.get("command") or "").strip()
    args = [str(item) for item in (cfg.get("args") or [])] if isinstance(cfg.get("args"), list) else []
    transport_hint = str(cfg.get("type") or "").lower()
    if url or transport_hint in {"sse", "http", "streamable-http", "streamable_http"}:
        return {
            "transport": "http" if transport_hint != "sse" else "sse",
            "command": None,
            "args": [],
            "url": url,
            "envKeys": env_keys,
            "headerKeys": [],
            "secretKeys": [item["key"] for item in fields if item["secret"]],
            "fieldMeta": fields,
            "missingEnvKeys": required,
            "blockedReason": None if url else "no_install_target",
            "draft": {
                "type": "http",
                "url": url,
                "command": None,
                "args": [],
                "env": {item["key"]: str(item["default"]) for item in fields if item.get("default") is not None},
                "headers": {},
            },
            "title": title,
            "description": description,
        }
    if command == "uvx" and not which("uvx"):
        return _blocked({**server, "title": title}, "unsupported_runtime")
    if not command:
        return _blocked({**server, "title": title}, "no_install_target")
    return {
        "transport": "stdio",
        "command": command,
        "args": args,
        "url": None,
        "envKeys": env_keys,
        "headerKeys": [],
        "secretKeys": [item["key"] for item in fields if item["secret"]],
        "fieldMeta": fields,
        "missingEnvKeys": required,
        "blockedReason": None,
        "draft": {
            "type": "stdio",
            "command": command,
            "args": args,
            "url": None,
            "env": {item["key"]: str(item["default"]) for item in fields if item.get("default") is not None},
            "headers": {},
        },
        "title": title,
        "description": description,
    }


def _first_modelscope_config(server: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    for block in server.get("server_config") or []:
        if not isinstance(block, dict):
            continue
        servers = block.get("mcpServers")
        if not isinstance(servers, dict):
            continue
        for name, cfg in servers.items():
            if isinstance(cfg, dict):
                return str(name), cfg
    return None, None


def _modelscope_title(server: dict[str, Any]) -> str:
    locales = server.get("locales") if isinstance(server.get("locales"), dict) else {}
    zh = locales.get("zh") if isinstance(locales.get("zh"), dict) else {}
    return str(zh.get("name") or server.get("chinese_name") or server.get("name") or server.get("id") or "")


def _schema_fields(schema: dict[str, Any]) -> list[dict[str, Any]]:
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = {str(item) for item in (schema.get("required") or [])}
    fields: list[dict[str, Any]] = []
    for key, spec in properties.items():
        info = spec if isinstance(spec, dict) else {}
        fields.append(
            {
                "key": str(key),
                "kind": "env",
                "description": str(info.get("description") or ""),
                "required": str(key) in required,
                "secret": True,
                "default": None,
            }
        )
    return fields


def _map_http(server: dict[str, Any], remote: dict[str, Any]) -> dict[str, Any]:
    fields = _header_fields(remote.get("headers") or [])
    env_keys = [item["key"] for item in fields]
    required = [item["key"] for item in fields if item["required"]]
    return {
        "transport": "http",
        "command": None,
        "args": [],
        "url": str(remote.get("url") or ""),
        "envKeys": env_keys,
        "headerKeys": env_keys,
        "secretKeys": [item["key"] for item in fields if item["secret"]],
        "fieldMeta": fields,
        "missingEnvKeys": required,
        "blockedReason": None if remote.get("url") else "no_install_target",
        "draft": {
            "type": "http",
            "url": str(remote.get("url") or ""),
            "command": None,
            "args": [],
            "env": {},
            "headers": {},
        },
        "title": _title(server),
        "description": str(server.get("description") or ""),
    }


def _map_npm(server: dict[str, Any], package: dict[str, Any]) -> dict[str, Any]:
    identifier = str(package.get("identifier") or "").strip()
    version = str(package.get("version") or "").strip()
    spec = f"{identifier}@{version}" if identifier and version else identifier
    args = ["-y", spec] if spec else ["-y"]
    args.extend(_literal_args(package.get("runtimeArguments")))
    args.extend(_literal_args(package.get("packageArguments")))
    fields = _env_fields(package.get("environmentVariables") or [])
    env_keys = [item["key"] for item in fields]
    required = [item["key"] for item in fields if item["required"] and not item.get("default")]
    return {
        "transport": "stdio",
        "command": "npx",
        "args": args,
        "url": None,
        "envKeys": env_keys,
        "headerKeys": [],
        "secretKeys": [item["key"] for item in fields if item["secret"]],
        "fieldMeta": fields,
        "missingEnvKeys": required,
        "blockedReason": None if identifier else "no_install_target",
        "draft": {
            "type": "stdio",
            "command": "npx",
            "args": args,
            "url": None,
            "env": {item["key"]: str(item["default"]) for item in fields if item.get("default") is not None},
            "headers": {},
        },
        "title": _title(server),
        "description": str(server.get("description") or ""),
    }


def _map_pypi(server: dict[str, Any], package: dict[str, Any]) -> dict[str, Any]:
    if not which("uvx"):
        return _blocked(server, "unsupported_runtime")
    identifier = str(package.get("identifier") or "").strip()
    args = [identifier] if identifier else []
    args.extend(_literal_args(package.get("runtimeArguments")))
    args.extend(_literal_args(package.get("packageArguments")))
    fields = _env_fields(package.get("environmentVariables") or [])
    env_keys = [item["key"] for item in fields]
    required = [item["key"] for item in fields if item["required"] and not item.get("default")]
    return {
        "transport": "stdio",
        "command": "uvx",
        "args": args,
        "url": None,
        "envKeys": env_keys,
        "headerKeys": [],
        "secretKeys": [item["key"] for item in fields if item["secret"]],
        "fieldMeta": fields,
        "missingEnvKeys": required,
        "blockedReason": None if identifier else "no_install_target",
        "draft": {
            "type": "stdio",
            "command": "uvx",
            "args": args,
            "url": None,
            "env": {item["key"]: str(item["default"]) for item in fields if item.get("default") is not None},
            "headers": {},
        },
        "title": _title(server),
        "description": str(server.get("description") or ""),
    }


def _blocked(server: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "transport": None,
        "command": None,
        "args": [],
        "url": None,
        "envKeys": [],
        "headerKeys": [],
        "secretKeys": [],
        "fieldMeta": [],
        "missingEnvKeys": [],
        "blockedReason": reason,
        "draft": None,
        "title": _title(server),
        "description": str(server.get("description") or ""),
    }


def _pick_remote(remotes: list[dict[str, Any]]) -> dict[str, Any] | None:
    for preferred in ("streamable-http", "http", "sse"):
        for remote in remotes:
            if str(remote.get("type") or "") == preferred and remote.get("url"):
                return remote
    return next((item for item in remotes if item.get("url")), None)


def _literal_args(items: Any) -> list[str]:
    args: list[str] = []
    if not isinstance(items, list):
        return args
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if value is None or "{" in str(value):
            continue
        text = str(value)
        if str(item.get("type") or "") == "named":
            name = str(item.get("name") or "").strip()
            if name:
                args.extend([name, text])
        else:
            args.append(text)
    return args


def _env_fields(items: Any) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return fields
    for item in items:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        default = item.get("default")
        fields.append(
            {
                "key": str(item["name"]),
                "kind": "env",
                "description": str(item.get("description") or ""),
                "required": bool(item.get("isRequired")),
                "secret": bool(item.get("isSecret")),
                "default": None if default is None else str(default),
            }
        )
    return fields


def _header_fields(items: Any) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return fields
    for item in items:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        fields.append(
            {
                "key": str(item["name"]),
                "kind": "header",
                "description": str(item.get("description") or item.get("value") or ""),
                "required": bool(item.get("isRequired", True)),
                "secret": bool(item.get("isSecret", True)),
                "default": None,
            }
        )
    return fields


def _title(server: dict[str, Any]) -> str:
    return str(server.get("title") or server.get("name") or "")
