"""Actual Uvicorn exception logging through the production logging setup."""
import os
import socket
import subprocess
import sys
import time
import httpx
import pytest

pytestmark=pytest.mark.skipif(os.getenv('RUN_SERVER_DIAGNOSTIC_TESTS')!='1',reason='Opt-in isolated actual Uvicorn diagnostics')


def test_uvicorn_unexpected_domain_and_http_failures_have_no_private_traceback(tmp_path):
    private='private-server-exception-sentinel'
    script='''
import sys
from fastapi import FastAPI, HTTPException
import uvicorn
from app.infra.logging_setup import setup_logging
from app.exceptions import register_exception_handlers, ServiceOperationError
setup_logging()
app=FastAPI();register_exception_handlers(app)
@app.get('/ready')
def ready():return {'ready':True}
@app.get('/failure/{kind}')
def failure(kind:str):
    if kind=='domain':raise ServiceOperationError('private-server-exception-sentinel')
    if kind=='http':raise HTTPException(502,detail='private-server-exception-sentinel')
    raise RuntimeError('private-server-exception-sentinel')
uvicorn.run(app,host='127.0.0.1',port=int(sys.argv[1]),log_config=None)
'''
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
    output=tmp_path/'uvicorn.log'
    with output.open('w') as log:
        process=subprocess.Popen([sys.executable,'-c',script,str(port)],stdout=log,stderr=log)
        try:
            base=f'http://127.0.0.1:{port}'
            deadline=time.monotonic()+20
            with httpx.Client(trust_env=False,timeout=1) as client:
                while True:
                    assert process.poll() is None,output.read_text()
                    try:
                        if client.get(base+'/ready').status_code==200:break
                    except httpx.HTTPError:pass
                    assert time.monotonic()<deadline,output.read_text()
                    time.sleep(.05)
                for kind,status in [('unexpected',500),('domain',500),('http',502)]:
                    response=client.get(base+'/failure/'+kind)
                    assert response.status_code==status
                    assert response.json()['error']['message']=='Internal server error'
                    assert private not in response.text
        finally:
            process.terminate()
            try:process.wait(10)
            except subprocess.TimeoutExpired:process.kill();process.wait(5)
    rendered=output.read_text()
    assert private not in rendered
    assert 'error_type=RuntimeError' in rendered
    assert 'Library failure in uvicorn.error' in rendered
    assert 'Traceback' not in rendered
