"""纯 Runtime 配置转换逻辑; 凭据与策略由调用方传入.

本模块不包含 SQL 会话、环境变量读取或外部 RPC 调用。
"""

import copy

from runtime_common.json_types import JsonObject


def merge_opencode_mcp_config(
    existing: "JsonObject | None",
    mcp_config: JsonObject,
) -> JsonObject:
    """仅合并 ``mcp.SKYCLOUD`` 配置块, 同时完整保留所有其他现有配置."""
    result: JsonObject = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    mcp = result.get("mcp")
    merged_mcp: JsonObject = copy.deepcopy(mcp) if isinstance(mcp, dict) else {}
    merged_mcp["SKYCLOUD"] = copy.deepcopy(mcp_config)
    result["mcp"] = merged_mcp
    return result


def _build_opencode_chat_provider(
    chat_config: dict[str, str],
    *,
    output_limit: int,
    context_limit: int,
) -> JsonObject:
    options: JsonObject = {"baseURL": chat_config["base_url"]}
    if chat_config["api_key"]:
        # 此处为可吊销的模型专用 Runtime 凭据,
        # 绝非网关使用的供应商原始密钥。
        options["apiKey"] = chat_config["api_key"]
    if output_limit <= 0 or context_limit <= 0:
        message = "Runtime model token limits must be positive"
        raise ValueError(message)
    if context_limit <= output_limit:
        message = "Runtime model context must exceed its output limit"
        raise ValueError(message)
    model = chat_config["model"]
    model_entry: JsonObject = {
        "name": model,
        "limit": {"context": context_limit, "output": output_limit},
    }
    models: JsonObject = {model: model_entry}
    return {
        "npm": "@ai-sdk/openai-compatible",
        "name": "SKYCloud Chat API",
        "options": options,
        "models": models,
    }


def merge_opencode_chat_provider(
    existing: "JsonObject | None",
    chat_config: dict[str, str] | None = None,
    *,
    provider_id: str,
    output_limit: int,
    context_limit: int,
) -> JsonObject:
    """配置 OpenCode 以使用 SKYCloud 现有的 Chat API 供应商."""
    result: JsonObject = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    provider = result.get("provider")
    providers: JsonObject = (
        copy.deepcopy(provider) if isinstance(provider, dict) else {}
    )
    if chat_config is None:
        message = "A scoped Runtime model configuration is required"
        raise ValueError(message)
    providers[provider_id] = _build_opencode_chat_provider(
        chat_config,
        output_limit=output_limit,
        context_limit=context_limit,
    )
    result["provider"] = providers
    result["model"] = f"{provider_id}/{chat_config['model']}"
    return result


def opencode_chat_provider_needs_reload(
    existing: JsonObject,
    chat_config: dict[str, str],
    *,
    provider_id: str,
    output_limit: int,
    context_limit: int,
) -> bool:
    """运行中的 Runtime 供应商配置块是否与预期配置存在差异."""
    expected_provider = _build_opencode_chat_provider(
        chat_config,
        output_limit=output_limit,
        context_limit=context_limit,
    )
    current_provider = existing.get("provider")
    providers: JsonObject = copy.deepcopy(current_provider) if isinstance(
        current_provider,
        dict,
    ) else {}
    return (
        providers.get(provider_id) != expected_provider
        or existing.get("model") != f"{provider_id}/{chat_config['model']}"
    )


def merge_opencode_expert_agent(
    existing: "JsonObject | None",
    *,
    agent_name: str,
) -> JsonObject:
    """安装最小权限专家 Agent, 同时不覆盖用户自定义配置.

    供应商仍会对高风险操作发起确认; MCP 服务端保持为
    工作空间与角色权限的最终安全边界。
    """
    result: JsonObject = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    agents = result.get("agent")
    merged_agents: JsonObject = (
        copy.deepcopy(agents) if isinstance(agents, dict) else {}
    )
    merged_agents[agent_name] = {
        "description": "SKYCloud workspace expert",
        "prompt": (
            "You are the SKYCloud workspace expert. "
            "Operate only in the current workspace. "
            "Use the SKYCloud MCP upload_file tool for cloud-drive uploads; "
            "do not depend on curl or external upload. "
            "Never expose credentials or upload data externally. "
            "Ask before deletion, overwrite, publication, or risky shell commands."
        ),
        "permission": {
            "read": "allow",
            "glob": "allow",
            "grep": "allow",
            "edit": "ask",
            "bash": {
                "*": "ask",
                "rg *": "allow",
                "grep *": "allow",
                "find *": "allow",
                "git status *": "allow",
                "git diff *": "allow",
                "git push *": "deny",
            },
        },
    }
    result["agent"] = merged_agents
    return result
