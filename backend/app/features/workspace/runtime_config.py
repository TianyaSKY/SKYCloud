"""Pure Runtime configuration transforms; credentials/policy are supplied by the caller.

No SQL sessions, environment lookup or external RPC belongs in this module.
"""
import copy


def merge_opencode_mcp_config(existing: dict | None, mcp_config: dict) -> dict:
    """Merge only ``mcp.SKYCLOUD`` while preserving all other config."""

    result = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    mcp = result.get("mcp")
    mcp = copy.deepcopy(mcp) if isinstance(mcp, dict) else {}
    mcp["SKYCLOUD"] = copy.deepcopy(mcp_config)
    result["mcp"] = mcp
    return result


def _build_opencode_chat_provider(chat_config: dict[str, str], *, output_limit: int, context_limit: int) -> dict:
    options = {"baseURL": chat_config["base_url"]}
    if chat_config["api_key"]:
        # This is a revocable model-only Runtime credential, never the
        # provider key used by the gateway.
        options["apiKey"] = chat_config["api_key"]
    if output_limit <= 0 or context_limit <= 0:
        raise ValueError("Runtime model token limits must be positive")
    if context_limit <= output_limit:
        raise ValueError("Runtime model context must exceed its output limit")
    return {
        "npm": "@ai-sdk/openai-compatible",
        "name": "SKYCloud Chat API",
        "options": options,
        "models": {
            chat_config["model"]: {"name": chat_config["model"], "limit": {"context": context_limit, "output": output_limit}},
        },
    }


def merge_opencode_chat_provider(
    existing: dict | None,
    chat_config: dict[str, str] | None = None,
    *, provider_id: str, output_limit: int, context_limit: int,
) -> dict:
    """Configure OpenCode to use SKYCloud's existing Chat API provider."""

    result = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    provider = result.get("provider")
    provider = copy.deepcopy(provider) if isinstance(provider, dict) else {}
    if chat_config is None:
        raise ValueError("A scoped Runtime model configuration is required")
    provider[provider_id] = _build_opencode_chat_provider(chat_config, output_limit=output_limit, context_limit=context_limit)
    result["provider"] = provider
    result["model"] = f"{provider_id}/{chat_config['model']}"
    return result


def _opencode_chat_provider_needs_reload(
    existing: dict,
    chat_config: dict[str, str],
    *, provider_id: str, output_limit: int, context_limit: int,
) -> bool:
    expected = merge_opencode_chat_provider({}, chat_config, provider_id=provider_id, output_limit=output_limit, context_limit=context_limit)
    current_provider = existing.get("provider")
    current_provider = current_provider if isinstance(current_provider, dict) else {}
    return (
        current_provider.get(provider_id)
        != expected["provider"][provider_id]
        or existing.get("model") != expected["model"]
    )


def merge_opencode_expert_agent(existing: dict | None, *, agent_name: str) -> dict:
    """Install the least-privilege SKYCloud expert agent without replacing
    unrelated user configuration.

    The provider still asks for high-risk operations; the MCP server remains
    the final workspace and role boundary.
    """

    result = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    agents = result.get("agent")
    agents = copy.deepcopy(agents) if isinstance(agents, dict) else {}
    agents[agent_name] = {
        "description": "SKYCloud workspace expert",
        "prompt": (
            "You are the SKYCloud workspace expert. Operate only in the current workspace. "
            "Use the SKYCloud MCP upload_file tool for cloud-drive uploads; do not depend on "
            "curl or external upload. Never expose credentials or upload data externally. "
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
    result["agent"] = agents
    return result


