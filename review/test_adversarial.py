"""Independent Chapter 33 review checks. Temporary stores and ephemeral loopback only.

Run: python3 -m unittest discover -s projects/reservation-payments/review -v
These checks do not launch Android, contact a hosted payment provider or change lab data.
"""
import concurrent.futures
from contextlib import closing
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from booking import BookingService
from common import Problem, http_server
from provider import Provider


class CaptureGate:
    """Stop exactly the first POST, either before commit or after commit/before reply."""
    def __init__(self, delegate, after_commit):
        self.delegate, self.after_commit = delegate, after_commit
        self.entered, self.release = threading.Event(), threading.Event()
        self.lock, self.used = threading.Lock(), False

    def route(self, method, path, body):
        with self.lock:
            gated = method == 'POST' and path == '/captures' and not self.used
            if gated:
                self.used = True
        if not gated:
            return self.delegate.route(method, path, body)
        result = self.delegate.route(method, path, body) if self.after_commit else None
        self.entered.set()
        if not self.release.wait(8):
            raise RuntimeError('Review gate was not released')
        return result if self.after_commit else self.delegate.route(method, path, body)


class AlterReceipt:
    def __init__(self, delegate, replacement, resource='captures'):
        self.delegate, self.replacement, self.resource = delegate, replacement, resource

    def route(self, method, path, body):
        status, result, drop = self.delegate.route(method, path, body)
        if path == '/' + self.resource or path.startswith('/' + self.resource + '/'):
            result = dict(result, **self.replacement)
        return status, result, drop


class DropFirstCreateReply:
    def __init__(self, delegate):
        self.delegate, self.used = delegate, False

    def route(self, method, path, body):
        status, result, drop = self.delegate.route(method, path, body)
        if method == 'POST' and path == '/bookings' and not self.used:
            self.used = True
            return status, result, True
        return status, result, drop


class DelayFirstCreate:
    def __init__(self, delegate):
        self.delegate, self.used = delegate, False
        self.entered, self.release = threading.Event(), threading.Event()

    def route(self, method, path, body):
        if method == 'POST' and path == '/bookings' and not self.used:
            self.used = True
            self.entered.set()
            if not self.release.wait(8):
                raise RuntimeError('Review create gate was not released')
        return self.delegate.route(method, path, body)


class Review(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='seatlight-review-')
        self.addCleanup(self.tmp.cleanup)
        self.provider_path = Path(self.tmp.name) / 'provider.sqlite'
        self.booking_path = Path(self.tmp.name) / 'booking.sqlite'
        self.provider = Provider(self.provider_path)
        self.servers = []
        self.addCleanup(self.stop_servers)
        self.url = self.start(self.provider)
        self.clock = [1000]
        self.a = BookingService(self.booking_path, self.url, clock=lambda: self.clock[0])
        self.b = BookingService(self.booking_path, self.url, clock=lambda: self.clock[0])

    def stop_servers(self):
        for server, thread in self.servers:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def start(self, service):
        server = http_server(service, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return f'http://127.0.0.1:{server.server_port}'

    def new(self, service=None):
        identity = str(uuid.uuid4())
        (service or self.a).route('POST', '/bookings', {'bookingId': identity, 'workshopId': 'clay-01'})
        return identity

    def action(self, identity, action, service=None):
        return (service or self.a).route('POST', f'/bookings/{identity}/{action}', {})[1]

    def available(self, service=None):
        return (service or self.a).route('GET', '/workshop', {})[1]['available']

    def counts(self):
        with closing(sqlite3.connect(self.provider_path)) as db:
            return tuple(db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                         for table in ('captures', 'refunds'))

    def post_provider(self, path, body):
        req = Request(self.url + path, data=json.dumps(body).encode(), method='POST',
                      headers={'Content-Type': 'application/json'})
        with urlopen(req, timeout=4) as response:
            return json.load(response)

    def capture_body(self, identity):
        return {'key': 'capture:' + identity, 'bookingId': identity,
                'amountMinor': 149900, 'currency': 'INR'}

    def test_two_service_instances_serialize_the_last_seat(self):
        barrier = threading.Barrier(2)
        def reserve(service):
            barrier.wait()
            try:
                return self.new(service)
            except Problem as error:
                return error.status
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(reserve, (self.a, self.b)))
        self.assertEqual(1, outcomes.count(409))
        self.assertEqual(0, self.available())
        with closing(sqlite3.connect(self.booking_path)) as db:
            self.assertEqual(1, db.execute('SELECT count(*) FROM bookings').fetchone()[0])

    def test_exact_expiry_and_terminal_create_replay_do_not_reopen(self):
        identity = self.new()
        self.clock[0] = 1600
        self.assertEqual('EXPIRED', self.action(identity, 'pay')['state'])
        replay = self.b.route('POST', '/bookings', {'bookingId': identity, 'workshopId': 'clay-01'})[1]
        self.assertEqual('EXPIRED', replay['state'])
        self.assertEqual((0, 0), self.counts())
        self.new(self.b)
        self.assertEqual(0, self.available())

    def test_admitted_unknown_pins_capacity_across_expiry_and_service_restart(self):
        identity = self.new()
        self.clock[0] = 1599
        self.provider.controls = {'unavailable': True}
        self.assertEqual('PAYMENT_PENDING', self.action(identity, 'pay')['state'])
        self.clock[0] = 1700
        reopened = BookingService(self.booking_path, self.url, clock=lambda: self.clock[0])
        self.assertEqual('PAYMENT_PENDING', reopened.snapshot(identity)['state'])
        with self.assertRaises(Problem) as rejected:
            self.new(reopened)
        self.assertEqual(409, rejected.exception.status)
        self.provider.controls = {}
        reopened.recover_all()
        self.assertEqual('CONFIRMED', reopened.snapshot(identity)['state'])
        self.assertEqual((1, 0), self.counts())

    def test_decline_is_durable_even_after_controls_change_and_provider_restart(self):
        identity = self.new()
        self.provider.controls = {'decline': True}
        self.assertEqual('PAYMENT_FAILED', self.action(identity, 'pay')['state'])
        reopened = Provider(self.provider_path)
        self.assertEqual('DECLINED', reopened.route('POST', '/captures', self.capture_body(identity))[1]['state'])
        self.assertEqual(1, self.available())
        self.new(self.b)
        self.provider.controls = {}
        self.assertEqual('PAYMENT_FAILED', self.action(identity, 'pay')['state'])
        self.assertEqual('DECLINED', self.post_provider('/captures', self.capture_body(identity))['state'])
        self.assertEqual((1, 0), self.counts())

    def exercise_capture_cancel_race(self, after_commit):
        gate = CaptureGate(self.provider, after_commit)
        url = self.start(gate)
        self.a.provider_url = self.b.provider_url = url
        identity = self.new()
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            pending_pay = pool.submit(self.action, identity, 'pay', self.a)
            try:
                self.assertTrue(gate.entered.wait(2), 'First capture must reach the gate')
                self.assertEqual('PAYMENT_PENDING', self.b.snapshot(identity)['state'])
                self.assertEqual(0, self.available(self.b))
                cancelled = self.action(identity, 'cancel', self.b)
                self.assertEqual('CANCELLED', cancelled['state'])
                self.assertTrue(cancelled['refundId'])
                self.assertEqual((1, 1), self.counts())
                # Reallocate before the old response lands: it must never re-confirm b1.
                second = self.new(self.b)
            finally:
                gate.release.set()
            self.assertEqual('CANCELLED', pending_pay.result(4)['state'])
        self.assertEqual('HELD', self.b.snapshot(second)['state'])
        self.assertEqual(0, self.available())
        self.assertEqual((1, 1), self.counts())

    def test_cancel_while_capture_has_not_committed_cannot_use_get404_as_a_fence(self):
        self.exercise_capture_cancel_race(after_commit=False)

    def test_late_committed_capture_response_cannot_overwrite_cancellation(self):
        self.exercise_capture_cancel_race(after_commit=True)

    def test_lost_refund_reply_keeps_capacity_until_reopened_service_resolves_receipt(self):
        identity = self.new()
        self.assertEqual('CONFIRMED', self.action(identity, 'pay')['state'])
        self.provider.controls = {'drop_refund_reply': True}
        self.assertEqual('REFUND_PENDING', self.action(identity, 'cancel')['state'])
        self.assertEqual((1, 1), self.counts())
        self.assertEqual(0, self.available())
        reopened_provider = Provider(self.provider_path)
        reopened = BookingService(self.booking_path, self.start(reopened_provider), clock=lambda: self.clock[0])
        reopened.recover_all()
        self.assertEqual('CANCELLED', reopened.snapshot(identity)['state'])
        self.assertEqual(1, self.available())
        self.assertEqual((1, 1), self.counts())

    def test_lost_capture_reply_reopens_one_intent_and_one_captured_receipt(self):
        identity = self.new()
        self.provider.controls = {'drop_capture_reply': True}
        self.assertEqual('PAYMENT_PENDING', self.action(identity, 'pay')['state'])
        self.assertEqual((1, 0), self.counts())
        self.assertEqual(0, self.available())
        reopened = BookingService(self.booking_path, self.start(Provider(self.provider_path)),
                                  clock=lambda: self.clock[0])
        reopened.recover_all()
        self.assertEqual('CONFIRMED', reopened.snapshot(identity)['state'])
        self.assertEqual((1, 0), self.counts())

    def test_malformed_refund_receipt_does_not_release_but_valid_lookup_recovers(self):
        identity = self.new()
        self.assertEqual('CONFIRMED', self.action(identity, 'pay')['state'])
        self.a.provider_url = self.start(AlterReceipt(self.provider, {'amountMinor': 149900.0}, 'refunds'))
        self.assertEqual('REFUND_PENDING', self.action(identity, 'cancel')['state'])
        self.assertEqual(0, self.available())
        self.assertEqual((1, 1), self.counts())
        self.a.provider_url = self.url
        self.assertEqual('CANCELLED', self.action(identity, 'reconcile')['state'])
        self.assertEqual(1, self.available())
        self.assertEqual((1, 1), self.counts())

    def test_oversized_capture_receipt_is_unknown_not_failure_or_confirmation(self):
        self.a.provider_url = self.start(AlterReceipt(self.provider, {'irrelevant': 'x' * 17000}))
        identity = self.new()
        self.assertEqual('PAYMENT_PENDING', self.action(identity, 'pay')['state'])
        self.assertEqual(0, self.available())
        self.assertEqual((1, 0), self.counts())
        self.a.provider_url = self.url
        self.assertEqual('CONFIRMED', self.action(identity, 'reconcile')['state'])
        self.assertEqual((1, 0), self.counts())

    def test_many_workers_converge_on_one_capture_and_one_refund(self):
        identity = self.new()
        self.provider.controls = {'unavailable': True}
        self.action(identity, 'pay')
        self.assertEqual('CANCEL_PENDING', self.action(identity, 'cancel')['state'])
        self.provider.controls = {}
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            list(pool.map(lambda n: (self.a if n % 2 else self.b).reconcile(identity), range(8)))
        self.assertEqual('CANCELLED', self.a.snapshot(identity)['state'])
        self.assertEqual((1, 1), self.counts())
        self.assertEqual(1, self.available())

    def test_provider_rejects_changed_intent_and_partial_or_renamed_refund(self):
        identity = self.new()
        original = self.post_provider('/captures', self.capture_body(identity))
        with self.assertRaises(HTTPError) as changed:
            self.post_provider('/captures', dict(self.capture_body(identity), amountMinor=149901))
        self.assertEqual(409, changed.exception.code)
        refund = {'key': 'refund:' + identity, 'captureKey': 'capture:' + identity,
                  'amountMinor': 149900, 'currency': 'INR'}
        with self.assertRaises(HTTPError) as partial:
            self.post_provider('/refunds', dict(refund, amountMinor=1))
        self.assertEqual(409, partial.exception.code)
        returned = self.post_provider('/refunds', refund)
        self.assertEqual(returned, self.post_provider('/refunds', refund))
        with self.assertRaises(HTTPError) as renamed:
            self.post_provider('/refunds', dict(refund, key='refund:another-key'))
        self.assertEqual(400, renamed.exception.code)
        self.assertEqual(original, self.post_provider('/captures', self.capture_body(identity)))
        self.assertEqual((1, 1), self.counts())

    def test_wrong_receipt_identity_or_money_never_confirms(self):
        for replacement in ({'bookingId': 'someone-else'}, {'amountMinor': 1},
                            {'currency': 'USD'}, {'state': 'PROCESSING'},
                            {'captureId': ''}, {'amountMinor': 149900.0}):
            with self.subTest(replacement=replacement):
                name = str(uuid.uuid4())
                service = BookingService(Path(self.tmp.name) / (name + '.sqlite'),
                                         self.start(AlterReceipt(self.provider, replacement)),
                                         clock=lambda: self.clock[0])
                identity = self.new(service)
                self.assertEqual('PAYMENT_PENDING', self.action(identity, 'pay', service)['state'])
                self.assertEqual(0, self.available(service))

    def test_external_http_lost_create_capture_refund_and_replay_history(self):
        first_url, second_url = self.start(DropFirstCreateReply(self.a)), self.start(self.b)
        def request(base, path, body=None):
            req = Request(base + path, data=None if body is None else json.dumps(body).encode(),
                          headers={'Content-Type': 'application/json'})
            with urlopen(req, timeout=4) as response:
                return response.status, json.load(response)
        identity = str(uuid.uuid4())
        body = {'bookingId': identity, 'workshopId': 'clay-01'}
        with self.assertRaises(OSError):
            request(first_url, '/bookings', body)
        self.clock[0] = 1010
        status, held = request(second_url, '/bookings', body)
        self.assertEqual(200, status)
        self.assertEqual(('HELD', 1600), (held['state'], held['expiresAt']))
        self.provider.controls = {'drop_capture_reply': True}
        status, pending = request(second_url, f'/bookings/{identity}/pay', {})
        self.assertEqual((202, 'PAYMENT_PENDING'), (status, pending['state']))
        self.provider.controls = {'drop_refund_reply': True}
        status, cancelling = request(first_url, f'/bookings/{identity}/cancel', {})
        self.assertEqual((202, 'REFUND_PENDING'), (status, cancelling['state']))
        self.assertEqual(0, request(second_url, '/workshop')[1]['available'])
        self.provider.controls = {}
        status, cancelled = request(second_url, f'/bookings/{identity}/reconcile', {})
        self.assertEqual((200, 'CANCELLED'), (status, cancelled['state']))
        self.assertTrue(cancelled['refundId'])
        self.assertEqual(cancelled, request(first_url, f'/bookings/{identity}')[1])
        self.assertEqual('CANCELLED', request(second_url, '/bookings', body)[1]['state'])
        self.assertEqual(1, request(first_url, '/workshop')[1]['available'])
        self.assertEqual((1, 1), self.counts())

    def test_external_http_rejects_malformed_and_conflicting_creation(self):
        url = self.start(self.a)
        cases = [(b'[]', 400), (b'{"unexpected":1}', 400), (b'\xff', 400), (b'x' * 16385, 413)]
        for raw, expected in cases:
            with self.subTest(expected=expected, length=len(raw)):
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(Request(url + '/bookings', data=raw, method='POST'), timeout=4)
                self.assertEqual(expected, rejected.exception.code)
                self.assertEqual(1, self.available())
        identity = self.new()
        body = {'bookingId': identity, 'workshopId': 'changed-workshop'}
        with self.assertRaises(HTTPError) as conflict:
            urlopen(Request(url + '/bookings', data=json.dumps(body).encode(), method='POST'), timeout=4)
        self.assertEqual(409, conflict.exception.code)
        self.assertEqual('HELD', self.a.snapshot(identity)['state'])
        self.assertEqual((0, 0), self.counts())

    def test_capacity_rejection_fences_a_delayed_original_after_inventory_is_freed(self):
        occupied = self.new()
        identity = str(uuid.uuid4())
        body = {'bookingId': identity, 'workshopId': 'clay-01'}
        gate = DelayFirstCreate(self.a)
        delayed_url, retry_url = self.start(gate), self.start(self.b)
        def post(base):
            req = Request(base + '/bookings', data=json.dumps(body).encode(), method='POST')
            try:
                with urlopen(req, timeout=4) as response:
                    return response.status, json.load(response)
            except HTTPError as error:
                with error:
                    return error.code, json.load(error)
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            original = pool.submit(post, delayed_url)
            try:
                self.assertTrue(gate.entered.wait(2))
                code, rejection = post(retry_url)
                self.assertEqual((409, 'hold_rejected'), (code, rejection['code']))
                self.assertEqual('CANCELLED', self.action(occupied, 'cancel', self.b)['state'])
                self.assertEqual(1, self.available())
            finally:
                gate.release.set()
            code, rejection = original.result(4)
        self.assertEqual((409, 'hold_rejected'), (code, rejection['code']))
        self.assertEqual(1, self.available())
        with self.assertRaises(Problem) as absent:
            self.a.snapshot(identity)
        self.assertEqual(404, absent.exception.status)
        self.assertEqual((0, 0), self.counts())

    def test_rejected_uuid_remains_rejected_after_service_restart_but_new_intent_can_hold(self):
        occupied = self.new()
        identity = str(uuid.uuid4())
        body = {'bookingId': identity, 'workshopId': 'clay-01'}
        with self.assertRaises(Problem) as rejected:
            self.b.route('POST', '/bookings', body)
        self.assertEqual((409, 'hold_rejected'), (rejected.exception.status, rejected.exception.kind))
        self.action(occupied, 'cancel')
        reopened = BookingService(self.booking_path, self.url, clock=lambda: self.clock[0])
        with self.assertRaises(Problem) as replay:
            reopened.route('POST', '/bookings', body)
        self.assertEqual((409, 'hold_rejected'), (replay.exception.status, replay.exception.kind))
        self.assertEqual(1, self.available(reopened))
        with self.assertRaises(Problem) as conflict:
            reopened.route('POST', '/bookings', dict(body, workshopId='changed'))
        self.assertEqual('request_error', conflict.exception.kind)
        self.new(reopened)
        self.assertEqual(0, self.available())
        self.assertEqual((0, 0), self.counts())


if __name__ == '__main__':
    unittest.main()
