"""Configuration policy remains independent and does not mutate source state."""
import copy
import pytest
from app.features.workspace import runtime_config


def test_composed_config_preserves_unrelated_nested_data_without_aliasing():
    original = {'mcp': {'other': {'headers': {'custom': 'keep'}}},
                'provider': {'other': {'models': {'existing': {'name': 'keep'}}}},
                'agent': {'custom': {'permission': {'edit': 'deny'}}}}
    snapshot = copy.deepcopy(original)
    cloud = {'headers': {'Authorization': 'Bearer scoped'}}
    result = runtime_config.merge_opencode_mcp_config(original, cloud)
    result = runtime_config.merge_opencode_chat_provider(result, {'base_url':'http://gateway/v1', 'model':'current', 'api_key':'scoped-model'},
                provider_id='cloud', output_limit=4096, context_limit=65536)
    result = runtime_config.merge_opencode_expert_agent(result, agent_name='workspace-expert')
    assert result['model'] == 'cloud/current'
    assert result['agent']['workspace-expert']['permission']['bash']['git push *'] == 'deny'
    assert result['agent']['workspace-expert']['permission']['edit'] == 'ask'
    result['mcp']['other']['headers']['custom'] = 'changed'
    result['mcp']['SKYCLOUD']['headers']['Authorization'] = 'changed'
    result['provider']['other']['models']['existing']['name'] = 'changed'
    result['agent']['custom']['permission']['edit'] = 'changed'
    assert original == snapshot
    assert cloud['headers']['Authorization'] == 'Bearer scoped'


@pytest.mark.parametrize('output,context', [(0,65536), (-1,65536), (4096,-1)])
def test_explicit_invalid_token_budget_cannot_form_runtime_provider(output, context):
    with pytest.raises(ValueError, match='positive'):
        runtime_config.merge_opencode_chat_provider({}, {'base_url':'http://gateway/v1', 'model':'current', 'api_key':'scoped'},
            provider_id='cloud', output_limit=output, context_limit=context)
