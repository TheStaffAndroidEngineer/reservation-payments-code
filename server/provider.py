"""Payment-provider simulator: no payment network, cards, money or credentials."""
import argparse
from contextlib import closing
import sqlite3
import uuid
from pathlib import Path
from urllib.parse import unquote
from common import Problem, exact, transaction, http_server


class Provider:
    def __init__(self, database):
        self.database = str(database)
        self.controls = {}
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS captures (
                    key TEXT PRIMARY KEY, bookingId TEXT NOT NULL,
                    amountMinor INTEGER NOT NULL, currency TEXT NOT NULL,
                    state TEXT NOT NULL, captureId TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS refunds (
                    key TEXT PRIMARY KEY, captureKey TEXT NOT NULL UNIQUE,
                    amountMinor INTEGER NOT NULL, currency TEXT NOT NULL,
                    refundId TEXT NOT NULL,
                    FOREIGN KEY(captureKey) REFERENCES captures(key));
            ''')

    def route(self, method, path, body):
        path = unquote(path)
        if path == '/__control':
            if method == 'POST':
                allowed = {'unavailable', 'decline', 'drop_capture_reply', 'drop_refund_reply'}
                if not isinstance(body, dict) or not set(body) <= allowed or any(type(v) is not bool for v in body.values()):
                    raise Problem(400, 'Invalid controls')
                self.controls = dict(body)
            with transaction(self.database) as db:
                return 200, {'controls': self.controls,
                    'captures': [dict(r) for r in db.execute('SELECT * FROM captures')],
                    'refunds': [dict(r) for r in db.execute('SELECT * FROM refunds')]}, False
        if self.controls.get('unavailable'):
            raise Problem(503, 'Provider unavailable')
        if method == 'GET' and (path.startswith('/captures/') or path.startswith('/refunds/')):
            table, key = path.strip('/').split('/', 1)
            with transaction(self.database) as db:
                row = db.execute(f'SELECT * FROM {table} WHERE key=?', (key,)).fetchone()
                if not row: raise Problem(404, 'Operation absent in retained simulator ledger')
                return 200, dict(row), False
        if method == 'POST' and path == '/captures':
            exact(body, ['key', 'bookingId', 'amountMinor', 'currency'])
            self.validate_money(body)
            if body['key'] != 'capture:' + body['bookingId']:
                raise Problem(400, 'Capture identity mismatch')
            with transaction(self.database) as db:
                old = db.execute('SELECT * FROM captures WHERE key=?', (body['key'],)).fetchone()
                if old and any(old[k] != v for k, v in body.items()):
                    raise Problem(409, 'Capture key reused with different intent')
                if not old:
                    state = 'DECLINED' if self.controls.get('decline') else 'CAPTURED'
                    db.execute('INSERT INTO captures VALUES (?,?,?,?,?,?)',
                        (*[body[k] for k in ['key','bookingId','amountMinor','currency']], state, 'cap-' + str(uuid.uuid4())))
                result = dict(db.execute('SELECT * FROM captures WHERE key=?', (body['key'],)).fetchone())
            return 200, result, self.controls.get('drop_capture_reply', False)
        if method == 'POST' and path == '/refunds':
            exact(body, ['key', 'captureKey', 'amountMinor', 'currency'])
            self.validate_money(body)
            if body['key'] != body['captureKey'].replace('capture:', 'refund:', 1):
                raise Problem(400, 'Refund identity mismatch')
            with transaction(self.database) as db:
                cap = db.execute('SELECT * FROM captures WHERE key=?', (body['captureKey'],)).fetchone()
                if not cap or cap['state'] != 'CAPTURED': raise Problem(409, 'No captured payment')
                if cap['amountMinor'] != body['amountMinor'] or cap['currency'] != body['currency']:
                    raise Problem(409, 'Only a full matching refund is supported')
                old = db.execute('SELECT * FROM refunds WHERE key=?', (body['key'],)).fetchone()
                if old and any(old[k] != v for k, v in body.items()): raise Problem(409, 'Refund key conflict')
                if not old:
                    db.execute('INSERT INTO refunds VALUES (?,?,?,?,?)',
                        (*[body[k] for k in ['key','captureKey','amountMinor','currency']], 'ref-' + str(uuid.uuid4())))
                result = dict(db.execute('SELECT * FROM refunds WHERE key=?', (body['key'],)).fetchone())
            return 200, result, self.controls.get('drop_refund_reply', False)
        raise Problem(404, 'Unknown provider route')

    @staticmethod
    def validate_money(body):
        if type(body['amountMinor']) is not int or not 0 < body['amountMinor'] <= 10_000_000 or body['currency'] != 'INR':
            raise Problem(400, 'Invalid money')
        if not all(isinstance(body[k], str) and 0 < len(body[k]) <= 100 for k in body if k != 'amountMinor'):
            raise Problem(400, 'Invalid identity')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--port', type=int, default=8901)
    parser.add_argument('--database', default='runtime/provider.sqlite'); args = parser.parse_args()
    Path(args.database).parent.mkdir(parents=True, exist_ok=True)
    print(f'Simulated provider http://127.0.0.1:{args.port}', flush=True)
    http_server(Provider(args.database), args.port).serve_forever()
