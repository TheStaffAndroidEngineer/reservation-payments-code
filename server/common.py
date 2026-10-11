"""Small loopback HTTP harness. The databases, not Python locks, own business state."""
import json
import sqlite3
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Problem(Exception):
    def __init__(self, status, message, kind="request_error"):
        self.status, self.message, self.kind = status, message, kind


@contextmanager
def transaction(path):
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        db.execute('BEGIN IMMEDIATE')
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def exact(body, keys):
    if not isinstance(body, dict) or set(body) != set(keys):
        raise Problem(400, 'Unexpected request fields')


def handler(service):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *_): pass
        def do_GET(self): self.serve('GET')
        def do_POST(self): self.serve('POST')
        def serve(self, method):
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length < 0 or length > 16_384:
                    raise Problem(413, 'Request too large')
                body = json.loads(self.rfile.read(length)) if length else {}
                code, result, drop = service.route(method, self.path, body)
                if drop:
                    self.close_connection = True
                    return
            except Problem as problem:
                code, result = problem.status, {'error': problem.message, 'code': problem.kind}
            except (ValueError, UnicodeError):
                code, result = 400, {'error': 'Invalid JSON or length'}
            except Exception:
                # Internal errors never become successful protocol responses.
                import traceback
                traceback.print_exc()
                code, result = 500, {'error': 'Internal fixture error'}
            raw = json.dumps(result, separators=(',', ':')).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Connection', 'close')
            self.end_headers()
            try: self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError): pass
            self.close_connection = True
    return Handler


def http_server(service, port):
    return ThreadingHTTPServer(('127.0.0.1', port), handler(service))
