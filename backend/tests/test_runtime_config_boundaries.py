"""配置策略保持独立且不变更源状态测试."""
import copy
from typing import Any, cast
import pytest
from app.features.workspace import runtime_config
from runtime_common.json_types import JsonObject


def test_composed_config_preserves_unrelated_nested_data_without_aliasing() -> None:
    original: JsonObject = {
        'mcp': {'other': {'headers': {'custom': 'keep'}}},
        'provider': {'other': {'models': {'existing': {'name': 'keep'}}}},
        'agent': {'custom': {'permission': {'edit': 'deny'}}},
    }
    snapshot = copy.deepcopy(original)
    cloud: JsonObject = {'headers': {'Authorization': 'Bearer scoped'}}
    result = runtime_config.merge_opencode_mcp_config(original, cloud)
    result = runtime_config.merge_opencode_chat_provider(
        result,
        {'base_url': 'http://gateway/v1', 'model': 'current', 'api_key': 'scoped-model'},
        provider_id='cloud',
        output_limit=4096,
        context_limit=65536,
    )
    result = runtime_config.merge_opencode_expert_agent(result, agent_name='workspace-expert')
    res = cast(dict[str, Any], result)
    assert res['model'] == 'cloud/current'
    assert res['agent']['workspace-expert']['permission']['bash']['git push *'] == 'deny'
    assert res['agent']['workspace-expert']['permission']['edit'] == 'ask'
    res['mcp']['other']['headers']['custom'] = 'changed'
    res['mcp']['SKYCLOUD']['headers']['Authorization'] = 'changed'
    res['provider']['other']['models']['existing']['name'] = 'changed'
    res['agent']['custom']['permission']['edit'] = 'changed'
    assert original == snapshot
    assert cast(dict[str, Any], cloud)['headers']['Authorization'] == 'Bearer scoped'


@pytest.mark.parametrize('output,context', [(0, 65536), (-1, 65536), (4096, -1)])
def test_explicit_invalid_token_budget_cannot_form_runtime_provider(output: int, context: int) -> None:
    with pytest.raises(ValueError, match='positive'):
        runtime_config.merge_opencode_chat_provider(
            {},
            {'base_url': 'http://gateway/v1', 'model': 'current', 'api_key': 'scoped'},
            provider_id='cloud',
            output_limit=output,
            context_limit=context,
        )
