"""Test-only private-network Docker proxy delaying one already received create."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading
import time


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(30)
        self.sock.connect('/var/run/docker.sock')


held = False
lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def forward(self):
        global held
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        delay = False
        if self.command == 'POST' and '/containers/create' in self.path:
            with lock:
                if not held:
                    held, delay = True, True
        if delay:
            print('CREATE_HELD', flush=True)
            deadline = time.monotonic() + 90
            while not Path('/tmp/release-create').exists() and time.monotonic() < deadline:
                time.sleep(0.05)
        connection = UnixConnection('localhost')
        try:
            connection.request(self.command, self.path, body=body,
                               headers={'Content-Type': self.headers.get('Content-Type', 'application/json')})
            response = connection.getresponse()
            data = response.read()
            if delay and response.status == 201:
                print('CREATE_COMPLETED:' + json.loads(data)['Id'], flush=True)
            try:
                self.send_response(response.status)
                self.send_header('Content-Type', response.getheader('Content-Type', 'application/json'))
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # The killed Controller cannot consume the daemon response.
        finally:
            connection.close()

    do_GET = do_POST = do_DELETE = do_PUT = forward


ThreadingHTTPServer(('0.0.0.0', 2375), Handler).serve_forever()
