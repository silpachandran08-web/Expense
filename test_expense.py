"""Run: python3 -m unittest -v
Set TEST_DATABASE_URL=postgresql://... to also run the storage tests against Postgres (the table is dropped first)."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

import server
from parse import categorize, parse

RULES = json.loads((Path(__file__).parent / 'rules.json').read_text(encoding='utf-8'))
TOKEN = 'test-token-1234567890'

SAMPLES = [
    # (sms, amount_minor, currency, direction, merchant, category)
    ('Sent Rs.250.00 From HDFC Bank A/C *1234 To SWIGGY On 18/10/26 Ref 628712345678 Not You? Call 18002586161',
     25000, 'INR', 'debit', 'SWIGGY', 'Food'),
    ('Rs 1,499.00 debited from A/c XX5678 on 12-Oct-26 to VPA netflix.upi@hdfcbank (UPI Ref No 6281234567). -ICICI Bank',
     149900, 'INR', 'debit', 'netflix.upi@hdfcbank', 'Subscriptions'),
    ('INR 3,200.50 spent on ICICI Bank Card XX9012 on 15-Oct-26 at AMAZON PAY INDIA. Avl Limit: INR 1,20,000.00',
     320050, 'INR', 'debit', 'AMAZON PAY INDIA', 'Shopping'),
    ('Dear Customer, your A/c XX3456 is credited with Rs.75,000.00 on 01-10-26 by NEFT SALARY ACME LTD. Avl Bal Rs.98,000.00 -SBI',
     7500000, 'INR', 'credit', '', 'Salary'),
    ('Rs.2000 withdrawn at ATM S1AW000123 from A/c XX3456 on 10Oct26. Avl bal Rs 5000',
     200000, 'INR', 'debit', 'ATM S1AW000123', 'Cash'),
    ('₹180 paid to Uber India via Axis Bank UPI on 19-10-2026. Ref 123',
     18000, 'INR', 'debit', 'Uber India', 'Transport'),
    # Saudi banks: English and Arabic, multi-line, Arabic-Indic digits.
    ('PoS Purchase\nAmount: SAR 45.50\nCard: *1234\nAt: HUNGERSTATION\nDate: 2026-10-18 13:45',
     4550, 'SAR', 'debit', 'HUNGERSTATION', 'Food'),
    ('Online Purchase\nBy: mada *5678\nAmount: 1,299.00 SAR\nAt: NOON.COM\nOn: 18/10/26',
     129900, 'SAR', 'debit', 'NOON.COM', 'Shopping'),
    ('شراء عبر نقاط البيع\nبطاقة: ٥٦٧٨\nمبلغ: ٨٧٫٢٥ ريال\nلدى: PANDA\nفي: 2026-10-17',
     8725, 'SAR', 'debit', 'PANDA', 'Groceries'),
    ('حوالة واردة\nمبلغ: SAR 12,000.00\nمن: ACME CO\nإلى: حسابك 1234',
     1200000, 'SAR', 'credit', 'ACME CO', 'Uncategorized'),
    ('ATM Withdrawal\nAmount: SR 500\nCard: *1234\nATM: Al Rajhi Olaya',
     50000, 'SAR', 'debit', '', 'Cash'),
]

NOT_TRANSACTIONS = [
    '123456 is your OTP for txn of Rs 500 at AMAZON. Do not share.',
    'Your credit card bill of Rs 12,000 is due on 25-Oct-26.',
    'You are pre-approved for a loan of Rs 5,00,000 to be credited instantly!',
    'Rs 300 will be debited from your account on 20-Oct for your SIP mandate.',
    'Swiggy has requested money from you on Google Pay. Amount: Rs 120',
    'Hi, your package is out for delivery.',
    'رمز التحقق: 4821 لعملية شراء بمبلغ 200 SAR لدى NOON',
    'Your verification code is 9921 for a purchase of SAR 89.00',
]


class ParseTest(unittest.TestCase):
    def test_transactions(self):
        for sms, minor, currency, direction, merchant, category in SAMPLES:
            with self.subTest(sms=sms):
                t = parse(sms)
                self.assertIsNotNone(t)
                self.assertEqual((t['amount_minor'], t['currency'], t['direction'], t['merchant']),
                                 (minor, currency, direction, merchant))
                self.assertEqual(categorize(t['merchant'], sms, RULES), category)

    def test_non_transactions_skipped(self):
        for sms in NOT_TRANSACTIONS:
            with self.subTest(sms=sms):
                self.assertIsNone(parse(sms))

    def test_account_number_not_amount(self):
        self.assertEqual(parse('A/c XX1234 debited Rs 500 at DMART')['amount_minor'], 50000)

    def test_whole_word_categories(self):
        self.assertEqual(categorize('MOTOROLA STORE', '', RULES), 'Uncategorized')  # not "ola" → Transport


class StoreMixin:
    url = None

    def setUp(self):
        self.db = server.DB(self.url)

    def tearDown(self):
        self.db.close()

    def test_store_dedupes_and_summarizes_per_currency(self):
        db = self.db
        self.assertTrue(server.store_sms(db, 'HDFCBK', SAMPLES[0][0], '2026-10-18T10:00:00+05:30'))
        self.assertIsNone(server.store_sms(db, 'HDFCBK', SAMPLES[0][0], '2026-10-18T10:00:05+05:30'))  # retry
        self.assertIsNone(server.store_sms(db, 'HDFCBK', NOT_TRANSACTIONS[0], '2026-10-18T10:00:00+05:30'))
        server.store_sms(db, 'SBIINB', SAMPLES[3][0], '2026-10-01T09:00:00+05:30')
        server.store_sms(db, 'AlRajhi', SAMPLES[6][0], '2026-09-20T13:45:00+03:00')

        s = server.summary(db, currency='INR')
        self.assertEqual(s['currencies'], ['INR', 'SAR'])
        self.assertEqual((s['month'], s['by_category']), ('2026-10', {'Food': 250.0}))
        self.assertEqual(s['months'], [{'month': '2026-10', 'spent': 250.0, 'received': 75000.0}])
        s = server.summary(db, currency='SAR')
        self.assertEqual((s['month'], s['by_category']), ('2026-09', {'Food': 45.5}))
        self.assertEqual(server.summary(db)['currency'], 'SAR')  # SAR first even when INR is newer
        self.assertEqual([t['currency'] for t in server.transactions(db, '2026-09')], ['SAR'])
        server.store_sms(db, 'AlRajhi', SAMPLES[9][0], '2026-09-21T09:00:00+03:00')  # unmatched incoming transfer
        self.assertEqual(server.transactions(db, '2026-09')[0]['category'], 'Income')


    def test_corrections_follow_the_merchant(self):
        db = self.db
        server.store_sms(db, 'B', 'PoS Purchase\nAmount: SAR 30.00\nAt: ABU SAAD BAKERY\nRef 1', '2026-10-01T09:00:00+03:00')
        server.store_sms(db, 'B', 'PoS Purchase\nAmount: SAR 12.00\nAt: ABU SAAD BAKERY\nRef 2', '2026-10-02T09:00:00+03:00')
        server.store_sms(db, 'B', 'SAR 99.00 paid to NOON.COM', '2026-10-03T09:00:00+03:00')
        cats = lambda: {t['body'][-5:]: t['category'] for t in server.transactions(db)}
        first = next(t for t in server.transactions(db) if t['body'].endswith('Ref 1'))
        self.assertEqual(first['category'], 'Uncategorized')

        server.set_category(db, first['id'], 'Food', True)  # "always use this for ABU SAAD BAKERY"
        server.store_sms(db, 'B', 'PoS Purchase\nAmount: SAR 8.00\nAt: ABU SAAD BAKERY\nRef 3', '2026-10-04T09:00:00+03:00')
        self.assertEqual([c for k, c in cats().items() if k.startswith('Ref')], ['Food', 'Food', 'Food'])  # past and future

        noon = next(t for t in server.transactions(db) if 'NOON' in t['body'])
        server.set_category(db, noon['id'], 'Gifts', False)  # just this one, and a new category
        self.assertEqual(cats()['N.COM'], 'Gifts')
        self.assertIn('Gifts', server.summary(db)['categories'])
        server.set_category(db, first['id'], 'Groceries', True)  # changing your mind updates the rule
        self.assertEqual(cats()['Ref 2'], 'Groceries')
        with self.assertRaises(KeyError):
            server.set_category(db, 999999, 'Food', True)


class SqliteStoreTest(StoreMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.url = os.path.join(self.tmp.name, 't.db')
        super().setUp()

    def tearDown(self):
        super().tearDown()
        self.tmp.cleanup()


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'set TEST_DATABASE_URL to test Postgres')
class PostgresStoreTest(StoreMixin, unittest.TestCase):
    url = os.environ.get('TEST_DATABASE_URL')

    def setUp(self):
        server.DB._schema_ready.discard(self.url)
        cleanup = server.DB(self.url)
        cleanup.run('DROP TABLE IF EXISTS transactions, merchant_categories, txn_categories')
        cleanup.close()
        server.DB._schema_ready.discard(self.url)
        super().setUp()


class AppTest(unittest.TestCase):
    """Drives the WSGI app the way Vercel or the local server would."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.update(EXPENSE_TOKEN=TOKEN, EXPENSE_DB=os.path.join(self.tmp.name, 'app.db'), DATABASE_URL='')

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, method, path, body=b'', auth=f'Bearer {TOKEN}', content_type='application/json', query='', cookie='', ip='198.51.100.1'):
        result = {}
        environ = {'REQUEST_METHOD': method, 'PATH_INFO': path, 'QUERY_STRING': query, 'HTTP_AUTHORIZATION': auth,
                   'HTTP_COOKIE': cookie, 'REMOTE_ADDR': ip, 'CONTENT_TYPE': content_type, 'CONTENT_LENGTH': str(len(body)),
                   'wsgi.input': io.BytesIO(body)}
        out = b''.join(server.app(environ, lambda status, headers: result.update(status=int(status[:3]), headers=dict(headers))))
        self.headers = result['headers']
        return result['status'], out

    def login(self, password):
        status, _ = self.call('POST', '/login', urlencode({'username': 'expense', 'password': password}).encode(),
                              auth='', content_type='application/x-www-form-urlencoded')
        return status, self.headers.get('Location'), self.headers.get('Set-Cookie', '')

    def test_login_flow(self):
        browser = dict(auth='')
        self.assertEqual((self.call('GET', '/', **browser)[0], self.headers['Location']), (303, '/login'))
        self.assertEqual(self.call('GET', '/login', **browser)[0], 200)
        self.assertEqual(self.login('wrong-password-123456'), (303, '/login?error=1', ''))

        status, location, set_cookie = self.login(TOKEN)
        self.assertEqual((status, location), (303, '/'))
        self.assertIn('HttpOnly', set_cookie)
        self.assertIn('Secure', set_cookie)
        cookie = set_cookie.split(';')[0]
        self.assertEqual(self.call('GET', '/', cookie=cookie, **browser)[0], 200)
        self.assertEqual(self.call('GET', '/api/summary', cookie=cookie, **browser)[0], 200)
        # The browser session can't add SMS; only the phone's bearer token can.
        self.assertEqual(self.call('POST', '/sms', b'{"sender":"x","body":"SAR 5 paid"}', cookie=cookie, **browser)[0], 401)

        expires, sig = cookie.removeprefix('session=').split('.')
        tampered = f'session={int(expires) + 1}.{sig}'
        self.assertEqual(self.call('GET', '/api/summary', cookie=tampered, **browser)[0], 401)
        expired = f'session=1.{server._sign("1")}'
        self.assertEqual(self.call('GET', '/api/summary', cookie=expired, **browser)[0], 401)

        self.assertEqual(self.call('POST', '/logout', cookie=cookie, **browser)[0], 303)
        self.assertIn('Max-Age=0', self.headers['Set-Cookie'])
        os.environ['EXPENSE_TOKEN'] = 'a-different-token-0987654321'  # rotating the token ends old sessions
        self.assertEqual(self.call('GET', '/api/summary', cookie=cookie, **browser)[0], 401)
        os.environ['EXPENSE_TOKEN'] = TOKEN

    def test_login_tolerates_spaces_and_reports_missing_setup(self):
        os.environ['EXPENSE_TOKEN'] = f' {TOKEN}\n'  # pasted into Vercel with whitespace
        self.assertEqual(self.login(f'{TOKEN} ')[:2], (303, '/'))  # keyboard added a trailing space
        os.environ['EXPENSE_TOKEN'] = 'short'  # 5 characters: below the minimum of 6
        self.assertEqual(self.login('short')[:2], (303, '/login?error=setup'))
        os.environ['EXPENSE_TOKEN'] = 'abc123'  # 6 characters is allowed
        self.assertEqual(self.login('abc123')[:2], (303, '/'))
        os.environ['EXPENSE_TOKEN'] = TOKEN

    def test_import_batch(self):
        self.assertEqual((self.call('GET', '/import', auth='')[0], self.headers['Location']), (303, '/login'))
        self.assertEqual(self.call('GET', '/import')[0], 200)
        batch = {'messages': [{'sender': 'AlRajhi', 'body': s, 'received_at': '1792002600000'} for s, *_ in SAMPLES[6:]]
                 + [{'sender': 'X', 'body': NOT_TRANSACTIONS[6], 'received_at': '1792002600000'}]}
        body = json.dumps(batch).encode()
        status, out = self.call('POST', '/api/import', body)
        self.assertEqual((status, json.loads(out)), (200, {'received': 6, 'stored': 5}))  # the OTP is skipped
        self.assertEqual(json.loads(self.call('POST', '/api/import', body)[1])['stored'], 0)  # re-import adds nothing
        self.assertEqual(json.loads(self.call('GET', '/api/summary', query='currency=SAR')[1])['month'], '2026-10')
        # JSON only (no cross-site form posts), batches capped, login required.
        self.assertEqual(self.call('POST', '/api/import', body, content_type='application/x-www-form-urlencoded')[0], 400)
        too_many = json.dumps({'messages': batch['messages'][:1] * (server.MAX_IMPORT_BATCH + 1)}).encode()
        self.assertEqual(self.call('POST', '/api/import', too_many)[0], 400)
        self.assertEqual(self.call('POST', '/api/import', body, auth='')[0], 401)

    def test_category_endpoint(self):
        sms = json.dumps({'sender': 'B', 'body': 'SAR 15.00 paid to ABU SAAD BAKERY'}).encode()
        self.call('POST', '/sms', sms)
        txn = json.loads(self.call('GET', '/api/transactions')[1])[0]
        fix = lambda **kw: self.call('POST', '/api/category', json.dumps({'id': txn['id'], **kw}).encode())[0]
        self.assertEqual(fix(category='Food', whole_merchant=True), 200)
        self.assertEqual(json.loads(self.call('GET', '/api/transactions')[1])[0]['category'], 'Food')
        self.assertEqual(fix(category='Uncategorized'), 400)
        self.assertEqual(fix(category='x' * 41), 400)
        self.assertEqual(self.call('POST', '/api/category', b'{"id": 424242, "category": "Food"}')[0], 404)
        form = urlencode({'id': txn['id'], 'category': 'Food'}).encode()  # cross-site form posts are refused
        self.assertEqual(self.call('POST', '/api/category', form, content_type='application/x-www-form-urlencoded')[0], 400)
        self.assertEqual(self.call('POST', '/api/category', b'{}', auth='')[0], 401)

    def test_lockout_after_too_many_wrong_guesses(self):
        for i in range(server.MAX_FAILURES - 1):
            self.assertEqual(self.login(f'wrong-{i}')[1], '/login?error=1')
        # Guessing the phone token counts too.
        self.assertEqual(self.call('GET', '/api/summary', auth='Bearer wrong-guess')[0], 401)
        self.assertEqual(self.login(TOKEN)[1], '/login?error=locked')  # even the right password, while locked
        self.assertEqual(self.call('GET', '/api/summary')[0], 401)
        other_ip = dict(auth='', content_type='application/x-www-form-urlencoded')
        self.call('POST', '/login', urlencode({'password': TOKEN}).encode(), ip='203.0.113.9', **other_ip)
        self.assertEqual(self.headers['Location'], '/')  # other devices are unaffected

    def test_flow(self):
        self.assertEqual(self.call('GET', '/api/summary', auth='Bearer wrong-token-0000000')[0], 401)
        self.assertEqual(self.call('GET', '/api/summary', auth='')[0], 401)
        # Payload shape the "SMS to URL Forwarder" phone app sends.
        sms = json.dumps({'sender': 'AlRajhiBank', 'body': SAMPLES[8][0], 'received_at': '2026-10-17T21:10:00+03:00'})
        status, out = self.call('POST', '/sms', sms.encode())
        self.assertEqual((status, json.loads(out)['transaction']['currency']), (201, 'SAR'))
        self.assertEqual(self.call('POST', '/sms', sms.encode())[0], 200)  # duplicate
        form = urlencode({'sender': 'VM-HDFCBK', 'body': SAMPLES[0][0]}).encode()
        self.assertEqual(self.call('POST', '/sms', form, content_type='application/x-www-form-urlencoded')[0], 201)
        self.assertEqual(self.call('POST', '/sms', b'{"sender":"x","body":"Rs 5 paid","received_at":"yesterday"}')[0], 400)
        self.assertEqual(self.call('POST', '/sms', b'not json')[0], 400)
        os.environ['EXPENSE_TZ'] = 'Asia/Kolkata'  # overrides the Asia/Riyadh default
        sms = json.dumps({'sender': 'X', 'body': SAMPLES[1][0], 'received_at': '1792002600000'})  # epoch ms, as the phone app sends
        self.assertEqual(json.loads(self.call('POST', '/sms', sms.encode())[1])['transaction']['received_at'], '2026-10-15T00:00:00+05:30')

        status, out = self.call('GET', '/api/summary', query='currency=SAR', auth=f'Bearer {TOKEN}')
        self.assertEqual((status, json.loads(out)['by_category']), (200, {'Groceries': 87.25}))
        self.assertEqual(self.call('GET', '/')[0], 200)
        self.assertEqual(self.call('GET', '/nope')[0], 404)


if __name__ == '__main__':
    unittest.main()
