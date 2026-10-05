"""Opt-in browser harness; serves only isolated fixture data on loopback."""
import json
import socket
import subprocess
import threading
import time
from pathlib import Path

import uvicorn
from fastapi.responses import HTMLResponse, Response


def exercise_browser(app, monkeypatch, user_id, workspace_id, auth, temporary):
    root = Path(__file__).resolve().parents[3]
    listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen(128)
    port = listener.getsockname()[1]
    parent = f'http://app.skycloud.localhost:{port}'
    monkeypatch.setenv('APP_ORIGIN', parent)
    monkeypatch.setenv('RUNTIME_UI_ORIGIN_TEMPLATE', f'http://r{{runtime_id}}.skycloud.localhost:{port}')
    bundle = temporary / 'client.js'
    # Exercise the production renewal utility. Only its API transport is
    # substituted to attach this disposable fixture's signed authentication.
    script = '''
      const { createRequire } = await import('node:module');
      const req = createRequire(process.argv[1] + '/frontend/package.json');
      const esbuild = req('esbuild');
      await esbuild.build({stdin: {contents: 'export * from "' + process.argv[1] + '/frontend/src/utils/runtimeUi.ts"',
        resolveDir: process.argv[1], loader: 'ts'}, bundle:true, format:'iife', globalName:'SkyUi',
        outfile:process.argv[2], plugins:[{name:'fixture-api', setup(build) {
          build.onResolve({filter:/api\\/workspace$/}, () => ({path:'fixture-api', namespace:'fixture'}));
          build.onLoad({filter:/.*/, namespace:'fixture'}, () => ({contents:
            `export async function issueRuntimeUiTicket(id) {
              const response = await fetch('/api/workspace/'+id+'/opencode/ui-ticket',
                {method:'POST', headers:{Authorization:window.fixtureAuth}});
              if (!response.ok) throw new Error('denied'); return response.json(); }`}));
        }}]});
    '''
    subprocess.run(['node', '--input-type=module', '-e', script, str(root), str(bundle)], check=True)
    @app.get('/__test/client.js')
    def client_script():
        return Response(bundle.read_text(), media_type='application/javascript')
    @app.get('/')
    def parent_page():
        return HTMLResponse('''<!doctype html><title>Isolated SKYCloud runtime verification</title>
          <p id="status">Opening runtime</p><button id="renew">Renew session</button>
          <iframe id="runtime" style="width:98vw;height:85vh" sandbox="allow-scripts allow-same-origin allow-downloads allow-forms"
            referrerpolicy="no-referrer"></iframe><script src="/__test/client.js"></script><script>
          window.fixtureAuth = AUTH;
          let loads=0,renewals=0; const frame=document.querySelector('#runtime');
          frame.addEventListener('load',()=>{loads++;document.querySelector('#status').textContent='Main loads: '+loads;});
          SkyUi.runtimeUiLaunchUrl(WS).then(url=>{frame.src=url;SkyUi.startRuntimeUiRenewal(WS,url,
            ()=>document.querySelector('#status').textContent='Renewal rejected');});
          document.querySelector('#renew').onclick=()=>document.dispatchEvent(new Event('visibilitychange'));
          window.addEventListener('message',event=>{if(event.data?.type==='skycloud-runtime-ui-renewed') {
            renewals++;document.querySelector('#status').textContent='Renewed '+renewals+'; main loads: '+loads;}});
          </script>'''.replace('AUTH', json.dumps(auth['Authorization'])).replace('WS', str(workspace_id)))
    async def ingress(scope, receive, send):
        host = dict(scope.get('headers', [])).get(b'host', b'').decode().split(':', 1)[0]
        if host.startswith('r') and host.endswith('.skycloud.localhost'):
            scope = {**scope, 'path': '/runtime-ui' + scope['path'],
                     'raw_path': b'/runtime-ui' + scope.get('raw_path', b'/')}
        await app(scope, receive, send)
    server = uvicorn.Server(uvicorn.Config(ingress, log_level='warning', access_log=False,
                                         ws_max_size=1024 * 1024, ws_max_queue=4))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    metadata = Path('/private/tmp/skycloud-runtime-browser.json')
    signal = temporary / 'browser-finished'
    metadata.write_text(json.dumps({'url': parent, 'completion_file': str(signal)}))
    try:
        deadline = time.monotonic() + 900
        while not signal.exists() and time.monotonic() < deadline:
            assert thread.is_alive(), 'Browser harness server stopped unexpectedly'
            time.sleep(.2)
        assert signal.exists(), 'Browser verification did not finish within fifteen minutes'
        assert signal.read_text().strip() == 'verified', 'Browser verification failed'
    finally:
        server.should_exit = True
        thread.join(5); listener.close(); metadata.unlink(missing_ok=True)
