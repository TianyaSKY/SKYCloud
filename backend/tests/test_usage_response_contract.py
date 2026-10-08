"""HTTP contracts for personal and administrator accounting endpoints."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.dependencies import get_current_user
from app.exceptions import register_exception_handlers
from app.features.token_usage import router as usage
from app.infra.extensions import get_db
from app.infra.datetime_utils import utc_now
from app.models.token_usage_log import TokenUsageLog

@pytest.fixture
def contract(session,test_user,admin_user):
    test_user.total_prompt_tokens=2;test_user.total_completion_tokens=3;test_user.total_tokens=5
    log=TokenUsageLog(user_id=test_user.id,action='chat',prompt_tokens=2,completion_tokens=3,total_tokens=5,created_at=utc_now())
    session.add(log);session.commit()
    app=FastAPI();register_exception_handlers(app);app.include_router(usage.router)
    app.dependency_overrides[get_db]=lambda:session
    app.dependency_overrides[get_current_user]=lambda:test_user
    return TestClient(app,raise_server_exceptions=False),app,test_user,admin_user,log

def data(response):
    assert response.status_code==200,response.text
    payload=response.json();assert set(payload)=={'success','data'} and payload['success'] is True
    return payload['data']

def test_personal_stats_logs_daily_and_self_routes_use_actual_sql(contract):
    client,app,user,admin,log=contract
    stats=data(client.get('/token-usage/stats'))
    assert stats['total_tokens']==5
    assert data(client.get(f'/token-usage/stats/{user.id}'))==stats
    page=data(client.get('/token-usage/logs',params={'action':'chat','page_size':1}))
    assert page['total']==1 and page['items'][0]['id']==log.id
    assert data(client.get(f'/token-usage/logs/{user.id}'))['items'][0]['user_id']==user.id
    day=data(client.get('/token-usage/daily'))[0]
    assert day['total_tokens']==5 and day['request_count']==1
    assert data(client.get(f'/token-usage/daily/{user.id}'))==[day]


def test_admin_all_variants_and_access_control(contract):
    client,app,user,admin,log=contract
    assert client.get('/admin/token-usage/users').status_code==403
    for route in ('stats','logs','daily'):
        assert client.get(f'/token-usage/{route}/{admin.id}').status_code==403
    app.dependency_overrides[get_current_user]=lambda:admin
    users=data(client.get('/admin/token-usage/users'))
    assert next(row for row in users if row['user_id']==user.id)['username']==user.username
    logs=data(client.get('/admin/token-usage/logs',params={'user_id':user.id}))
    assert logs['items'][0]['username']==user.username
    assert data(client.get('/admin/token-usage/daily'))[0]['total_tokens']==5
    split=data(client.get('/admin/token-usage/daily/per-user'))
    assert split[0]['user_id']==user.id and split[0]['total_tokens']==5
    response=client.get('/token-usage/stats/99999')
    assert response.status_code==404 and response.json()['error']['code']=='RESOURCE_NOT_FOUND'

@pytest.mark.parametrize('route',['/token-usage/logs','/admin/token-usage/logs'])
@pytest.mark.parametrize('params',[{'start_date':'private-invalid-date'},{'end_date':'bad'}, {'start_date':'2030-01-01','end_date':'2020-01-01'}])
def test_date_filters_fail_explicitly_instead_of_widening_results(contract,route,params):
    client,app,user,admin,log=contract
    app.dependency_overrides[get_current_user]=lambda:admin
    response=client.get(route,params=params)
    assert response.status_code==400 and response.json()['success'] is False
    assert 'private-invalid-date' not in response.text


def test_log_filters_pagination_and_openapi(contract):
    client,app,user,admin,log=contract
    assert data(client.get('/token-usage/logs',params={'start_date':'2099-01-01'}))['total']==0
    assert data(client.get('/token-usage/logs',params={'action':'embedding'}))['total']==0
    assert client.get('/token-usage/logs',params={'page_size':101}).status_code==422
    schema=client.get('/openapi.json').json()
    for ops in schema['paths'].values():
        for op in ops.values():
            ref=op['responses']['200']['content']['application/json']['schema']['$ref']
            assert schema['components']['schemas'][ref.split('/')[-1]]['properties']['success']['const'] is True


def test_stats_output_private_field_filtered_and_invalid_output_safe(contract,monkeypatch):
    client,app,user,admin,log=contract
    stats=data(client.get('/token-usage/stats'))
    monkeypatch.setattr(usage.token_usage_service,'get_user_token_stats',lambda *a:{**stats,'credential':'private-secret'})
    assert data(client.get('/token-usage/stats'))==stats
    monkeypatch.setattr(usage.token_usage_service,'get_user_token_stats',lambda *a:{'user_id':'private-secret'})
    response=client.get('/token-usage/stats')
    assert response.status_code==500 and response.json()['error']['code']=='INTERNAL_ERROR'
    assert 'private-secret' not in response.text
