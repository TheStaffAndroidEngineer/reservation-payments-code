"""Booking authority. Database commits do not span the external payment boundary."""
import argparse
from contextlib import closing
import json
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from common import Problem, exact, transaction, http_server

ACTIVE = ('HELD', 'PAYMENT_PENDING', 'CANCEL_PENDING', 'CONFIRMED', 'REFUND_PENDING')
PENDING = ('PAYMENT_PENDING', 'CANCEL_PENDING', 'REFUND_PENDING')
TERMINAL = ('CANCELLED', 'EXPIRED', 'PAYMENT_FAILED')


class BookingService:
    def __init__(self, database, provider_url, capacity=1, clock=time.time):
        if type(capacity) is not int or not 1 <= capacity <= 100: raise ValueError("Invalid workshop capacity")
        self.database, self.provider_url = str(database), provider_url
        self.clock, self.controls = clock, {}
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS workshop (id TEXT PRIMARY KEY, capacity INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS rejected_holds (bookingId TEXT PRIMARY KEY, workshopId TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bookings (
                    bookingId TEXT PRIMARY KEY, workshopId TEXT NOT NULL,
                    state TEXT NOT NULL, priceMinor INTEGER NOT NULL,
                    currency TEXT NOT NULL, expiresAt INTEGER NOT NULL,
                    captureId TEXT NOT NULL DEFAULT '', refundId TEXT NOT NULL DEFAULT '');
            ''')
            db.execute('INSERT OR IGNORE INTO workshop VALUES (?,?)', ('clay-01', capacity))

    def now(self): return int(self.clock()) + self.controls.get('advance_seconds', 0)
    def expire(self, db):
        db.execute("UPDATE bookings SET state='EXPIRED' WHERE state='HELD' AND expiresAt<=?", (self.now(),))
    def row(self, db, booking_id):
        row = db.execute('SELECT * FROM bookings WHERE bookingId=?', (booking_id,)).fetchone()
        if not row: raise Problem(404, 'Booking absent')
        return dict(row)
    def snapshot(self, booking_id):
        with transaction(self.database) as db:
            self.expire(db); result = self.row(db, booking_id)
        return dict(result, serverNow=self.now())

    def route(self, method, path, body):
        if path == '/__control':
            if method == 'POST':
                if not isinstance(body, dict) or not set(body) <= {'advance_seconds', 'provider_unavailable'}:
                    raise Problem(400, 'Invalid controls')
                if type(body.get('advance_seconds', 0)) is not int or not 0 <= body.get('advance_seconds', 0) <= 86400:
                    raise Problem(400, 'Invalid clock advance')
                if type(body.get('provider_unavailable', False)) is not bool: raise Problem(400, 'Invalid provider control')
                self.controls = dict(body)
            with transaction(self.database) as db:
                self.expire(db)
                return 200, {'controls': self.controls, 'bookings':[dict(r) for r in db.execute('SELECT * FROM bookings')]}, False
        if method == 'GET' and path == '/workshop':
            with transaction(self.database) as db:
                self.expire(db)
                capacity = db.execute('SELECT capacity FROM workshop WHERE id=?', ('clay-01',)).fetchone()[0]
                occupied = db.execute('SELECT COUNT(*) FROM bookings WHERE state IN (?,?,?,?,?)', ACTIVE).fetchone()[0]
                return 200, {'workshopId':'clay-01', 'title':'Clay workshop', 'studio':'North studio',
                    'durationMinutes':90, 'priceMinor':149900, 'currency':'INR', 'capacity':capacity,
                    'available':capacity-occupied, 'serverNow':self.now()}, False
        if method == 'POST' and path == '/bookings':
            exact(body, ['bookingId', 'workshopId'])
            try:
                if str(uuid.UUID(body['bookingId'])) != body['bookingId']: raise ValueError()
            except (ValueError, TypeError, AttributeError): raise Problem(400, 'Invalid booking UUID')
            if body['workshopId'] != 'clay-01': raise Problem(409, 'Unknown or changed workshop')
            rejected = False
            with transaction(self.database) as db:
                self.expire(db)
                old = db.execute('SELECT * FROM bookings WHERE bookingId=?', (body['bookingId'],)).fetchone()
                rejected = db.execute('SELECT 1 FROM rejected_holds WHERE bookingId=?', (body['bookingId'],)).fetchone() is not None
                if not old and not rejected:
                    occupied = db.execute('SELECT COUNT(*) FROM bookings WHERE state IN (?,?,?,?,?)', ACTIVE).fetchone()[0]
                    capacity = db.execute('SELECT capacity FROM workshop WHERE id=?', ('clay-01',)).fetchone()[0]
                    if occupied >= capacity:
                        # Commit a negative admission decision too: an older in-flight
                        # request with this UUID must not create a hold after rejection.
                        db.execute('INSERT INTO rejected_holds VALUES (?,?)', (body['bookingId'],'clay-01'))
                        rejected = True
                    else:
                        db.execute('INSERT INTO bookings (bookingId,workshopId,state,priceMinor,currency,expiresAt) VALUES (?,?,?,?,?,?)',
                            (body['bookingId'], 'clay-01', 'HELD', 149900, 'INR', self.now()+600))
            if rejected: raise Problem(409, 'No seats remain for this rejected request', 'hold_rejected')
            return 200, self.snapshot(body['bookingId']), False
        pieces = path.strip('/').split('/')
        if len(pieces) not in (2,3) or pieces[0] != 'bookings': raise Problem(404, 'Unknown route')
        booking_id = pieces[1]
        if method == 'GET' and len(pieces) == 2: return 200, self.snapshot(booking_id), False
        if method != 'POST' or len(pieces) != 3: raise Problem(404, 'Unknown operation')
        exact(body, [])
        action = pieces[2]
        if action not in ('pay','cancel','reconcile'): raise Problem(404, 'Unknown operation')
        with transaction(self.database) as db:
            self.expire(db); row = self.row(db, booking_id)
            if action == 'pay' and row['state'] == 'HELD':
                db.execute("UPDATE bookings SET state='PAYMENT_PENDING' WHERE bookingId=?", (booking_id,))
            if action == 'cancel':
                next_state = {'HELD':'CANCELLED','PAYMENT_PENDING':'CANCEL_PENDING','CONFIRMED':'REFUND_PENDING'}.get(row['state'])
                if next_state: db.execute('UPDATE bookings SET state=? WHERE bookingId=?', (next_state,booking_id))
        self.reconcile(booking_id)
        result = self.snapshot(booking_id)
        return (202 if result['state'] in PENDING else 200), result, False

    def provider(self, method, path, body=None):
        if self.controls.get('provider_unavailable'): raise OSError('Provider access disabled')
        request = Request(self.provider_url+path, data=None if body is None else json.dumps(body).encode(),
            headers={'Content-Type':'application/json'}, method=method)
        try:
            with urlopen(request, timeout=3) as response:
                data = response.read(16_385)
                if len(data)>16_384: raise OSError('Provider response too large')
                return json.loads(data)
        except HTTPError as error:
            if error.code == 404: return None
            raise OSError('Provider outcome not established') from error

    def reconcile(self, booking_id):
        row = self.snapshot(booking_id)
        try:
            if row['state'] in ('PAYMENT_PENDING','CANCEL_PENDING'):
                key = 'capture:'+booking_id
                capture = self.provider('GET', '/captures/'+key)
                if capture is None:
                    # Even a cancellation must settle an admitted capture intent. A concurrent
                    # worker may already be sending it; absence alone cannot fence that worker.
                    capture = self.provider('POST', '/captures', {'key':key,'bookingId':booking_id,
                        'amountMinor':row['priceMinor'],'currency':row['currency']})
                self.validate_capture(row, capture)
                with transaction(self.database) as db:
                    current = self.row(db,booking_id)
                    if current['state'] in ('PAYMENT_PENDING','CANCEL_PENDING'):
                        cancellation = current['state'] == 'CANCEL_PENDING'
                        if capture['state'] == 'DECLINED':
                            state = 'CANCELLED' if cancellation else 'PAYMENT_FAILED'
                        elif cancellation:
                            state = 'REFUND_PENDING'
                        else:
                            state = 'CONFIRMED'
                        db.execute('UPDATE bookings SET state=?, captureId=? WHERE bookingId=?', (state,capture['captureId'],booking_id))
            row = self.snapshot(booking_id)
            if row['state'] == 'REFUND_PENDING':
                key = 'refund:'+booking_id
                refund = self.provider('GET','/refunds/'+key)
                if refund is None:
                    refund = self.provider('POST','/refunds', {'key':key,'captureKey':'capture:'+booking_id,
                        'amountMinor':row['priceMinor'],'currency':row['currency']})
                expected = {'key':key,'captureKey':'capture:'+booking_id,'amountMinor':row['priceMinor'],'currency':row['currency']}
                if not isinstance(refund,dict) or type(refund.get('amountMinor')) is not int or any(refund.get(k)!=v for k,v in expected.items()) or not isinstance(refund.get('refundId'),str) or not refund['refundId']:
                    raise OSError('Invalid refund receipt')
                with transaction(self.database) as db:
                    db.execute("UPDATE bookings SET state='CANCELLED',refundId=? WHERE bookingId=? AND state='REFUND_PENDING'", (refund['refundId'],booking_id))
        except (OSError, ValueError, KeyError, TypeError):
            # Unknown is deliberately nonterminal. Durable pending rows are the worker inventory.
            return

    @staticmethod
    def validate_capture(row, capture):
        expected = {'key':'capture:'+row['bookingId'],'bookingId':row['bookingId'],
            'amountMinor':row['priceMinor'],'currency':row['currency']}
        if not isinstance(capture,dict) or type(capture.get('amountMinor')) is not int or any(capture.get(k)!=v for k,v in expected.items()) or capture.get('state') not in ('CAPTURED','DECLINED') or not isinstance(capture.get('captureId'),str) or not capture['captureId']:
            raise OSError('Invalid capture receipt')

    def recover_all(self):
        with transaction(self.database) as db:
            ids = [r[0] for r in db.execute('SELECT bookingId FROM bookings WHERE state IN (?,?,?)', PENDING)]
        for booking_id in ids: self.reconcile(booking_id)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--port',type=int,default=8900)
    parser.add_argument('--provider',default='http://127.0.0.1:8901');parser.add_argument('--database',default='runtime/booking.sqlite')
    parser.add_argument('--capacity',type=int,default=1);parser.add_argument('--recover-once',action='store_true')
    args=parser.parse_args();Path(args.database).parent.mkdir(parents=True,exist_ok=True)
    service=BookingService(args.database,args.provider,args.capacity)
    if args.recover_once: service.recover_all()
    else:
        print(f'Seatlight http://127.0.0.1:{args.port}',flush=True)
        http_server(service,args.port).serve_forever()
