"""Pure Runtime configuration transforms; credentials/policy are supplied by the caller.

No SQL sessions, environment lookup or external RPC belongs in this module.
"""

import copy

from runtime_common.json_types import JsonObject


def merge_opencode_mcp_config(
    existing: "JsonObject | None",
    mcp_config: JsonObject,
) -> JsonObject:
    """Merge only ``mcp.SKYCLOUD`` while preserving all other config."""
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
        # This is a revocable model-only Runtime credential, never the
        # provider key used by the gateway.
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
    """Configure OpenCode to use SKYCloud's existing Chat API provider."""
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
    """Whether the live Runtime provider block differs from the desired one."""
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
    """Install the least-privilege expert agent without replacing user config.

    The provider still asks for high-risk operations; the MCP server remains
    the final workspace and role boundary.
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
