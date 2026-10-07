"""Recovery must bind completed output to the newly submitted user message."""
import pytest
from app.features.assistant.engines.expert_engine import _current_reply


def user(identity='new-user',query='question'):
    return {'info':{'id':identity,'role':'user'},'parts':[{'type':'text','text':query}]}


def answer(identity='new-answer',parent='new-user',text='current',**changes):
    info={'id':identity,'role':'assistant','parentID':parent,'finish':'stop','time':{'completed':123},'tokens':{'output':7}}
    info.update(changes)
    return {'info':info,'parts':[{'type':'text','text':text}]}


def test_repeated_query_does_not_recover_prior_round_or_its_usage():
    prior=[user('old-user'),answer('old-answer','old-user','old')]
    ids={'old-user','old-answer'}
    assert _current_reply(prior,ids,'question') == ('',None,{},None)
    assert _current_reply(prior+[user(),answer()],ids,'question') == ('current','new-answer',{'output':7},None)


@pytest.mark.parametrize('changes',[{'id':None},{'parentID':'old-user'},{'parentID':'foreign'}, {'time':{}},{'finish':'tool-calls'},{'finish':None},{'error':{'message':'failed'}}])
def test_incomplete_failed_or_unrelated_answer_is_not_success(changes):
    assert _current_reply([user(),answer(**changes)],set(),'question') == ('',None,{},None)


def test_different_query_and_malformed_user_parts_do_not_bind():
    assert _current_reply([user(query='other'),answer()],set(),'question') == ('',None,{},None)
    malformed=user();malformed['parts']=None
    assert _current_reply([malformed,answer()],set(),'question') == ('',None,{},None)
