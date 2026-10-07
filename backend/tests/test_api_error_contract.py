"""REST errors have one documented shape without private validator context."""
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, field_validator
from app.api.contracts import ErrorResponse
from app.exceptions import register_exception_handlers, PermissionDeniedError


@pytest.fixture
def api():
    app=FastAPI();register_exception_handlers(app)
    @app.get('/http/{status}')
    def fail(status:int):
        raise HTTPException(status,detail='Allowed public explanation',headers={'WWW-Authenticate':'Bearer'})
    @app.get('/domain')
    def domain(): raise PermissionDeniedError('Current role denied')
    @app.get('/unexpected')
    def unexpected(): raise RuntimeError('private credential')
    class Input(BaseModel):
        password:str
        @field_validator('password')
        @classmethod
        def validate(cls,value): raise ValueError('private credential '+value)
    @app.post('/validate')
    def validate(body:Input): return body
    return app


@pytest.mark.parametrize('status,code',[(400,'INVALID_REQUEST'),(401,'AUTHENTICATION_REQUIRED'),(403,'PERMISSION_DENIED'),(409,'CONFLICT'),(413,'PAYLOAD_TOO_LARGE'),(429,'RATE_LIMITED'),(503,'SERVICE_UNAVAILABLE')])
def test_http_error_contract_and_headers(api,status,code):
    response=TestClient(api).get(f'/http/{status}')
    parsed=ErrorResponse.model_validate(response.json())
    assert parsed.success is False and parsed.error.code==code
    assert response.headers['www-authenticate']=='Bearer'
    assert parsed.error.message==('Internal server error' if status>=500 else 'Allowed public explanation')
    assert set(response.json())=={'success','error'}


def test_framework_missing_route_and_method_share_contract(api):
    client=TestClient(api)
    assert client.get('/missing').json()['error']['code']=='NOT_FOUND'
    response=client.post('/domain')
    assert response.json()['error']['code']=='METHOD_NOT_ALLOWED'
    assert response.headers['allow']=='GET'


def test_domain_and_unexpected_error_contract(api):
    client=TestClient(api,raise_server_exceptions=False)
    assert client.get('/domain').json()['error']=={'code':'PERMISSION_DENIED','message':'Current role denied'}
    response=client.get('/unexpected')
    assert response.status_code==500 and 'private credential' not in response.text
    assert response.json()['error']['code']=='INTERNAL_ERROR'


def test_validation_contract_does_not_echo_input_or_context(api):
    response=TestClient(api).post('/validate',json={'password':'submitted-secret'})
    assert response.status_code==422
    assert 'submitted-secret' not in response.text and 'private credential' not in response.text
    assert response.json()['error']['issues']==[{'loc':['body','password'],'msg':'Invalid value','type':'value_error'}]


def test_openapi_documents_canonical_errors(api):
    schema=api.openapi()
    assert schema['paths']['/domain']['get']['responses']['403']['content']['application/json']['schema']['$ref'].endswith('/ErrorResponse')
    assert schema['paths']['/validate']['post']['responses']['422']['content']['application/json']['schema']['$ref'].endswith('/ErrorResponse')


def test_upload_limit_uses_same_error_contract_before_router_auth(api):
    from app.infra.upload_limits import UploadBodyLimitMiddleware
    api.add_middleware(UploadBodyLimitMiddleware,max_bytes=2)
    response=TestClient(api).post('/api/files',content=b'oversized')
    assert response.status_code==413
    assert response.json()=={'success':False,'error':{'code':'PAYLOAD_TOO_LARGE','message':'Upload request too large'}}
