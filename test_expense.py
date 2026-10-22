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
from parse import categorize, compile_rules, parse

RULES = compile_rules(json.loads((Path(__file__).parent / 'rules.json').read_text(encoding='utf-8')))
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
    # The amount line comes first; the date on the line after 'Balance: … SAR' must not be read as the amount.
    ('Online Purchase\nBy:1596 ;Visa-Apple Pay\nAmount:57.5 SAR\nAt:Mobily\nBalance:181573.84 SAR\n22/2/26 16:08',
     5750, 'SAR', 'debit', 'Mobily', 'Bills & Utilities'),
    ('Balance: SAR 9,000.00\nPoS Purchase\nAmount: SAR 45.00\nAt: PANDA',
     4500, 'SAR', 'debit', 'PANDA', 'Groceries'),
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
    # Adverts
    'Purchase 20GB data for SAR 99 now! Dial *1100# . Valid until 31/10',
    'Get 50% discount: recharge SAR 30 and enjoy unlimited calls. To unsubscribe send STOP',
    'عرض خاص: اشترك الآن في باقة 25 جيجا بـ 99 ريال لفترة محدودة',
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

    def test_merchant_stops_before_for(self):
        # "for SAR ..." is the amount, not part of the name; otherwise each purchase is a different "merchant".
        self.assertEqual(parse('Online purchase at Amazon SA for SAR 5,699.00 using card *1234')['merchant'], 'Amazon SA')
        self.assertEqual(parse('Purchase at Temu.com for SAR 198.85 on 17/10')['merchant'], 'Temu.com')

    def test_most_specific_rule_wins(self):
        for order in (['jarir', 'jarir medical co'], ['jarir medical co', 'jarir']):
            rules = {k: {'jarir': 'Shopping', 'jarir medical co': 'Health'}[k] for k in order}
            find = server.rule_finder(rules)
            self.assertEqual((find('JARIR MEDICAL CO'), find('JARIR BOOKSTORE'), find('PANDA')), ('Health', 'Shopping', None))

    def test_markets_are_groceries_and_eating_out_is_food(self):
        cases = {'LULU HYPERMARKET': 'Groceries', 'AL SADHAN SUPER MARKET': 'Groceries', 'Danube Fresh Foods': 'Groceries',
                 'BAQALA AL NOOR': 'Groceries', 'تموينات الخير': 'Groceries', 'MINI MARKET 24': 'Groceries',
                 'AL BAIK RESTAURANTS': 'Food', 'HALF MILLION COFFEE': 'Food', 'DOSE CAFE': 'Food', 'KUDU REST': 'Food',
                 'ABU SAAD BAKERY': 'Food', 'MAESTRO PIZZA': 'Food', 'Fresh Juice Corner': 'Food', 'مطعم البيك': 'Food',
                 'BARNS COFFEES': 'Food', 'MCDONALDS': 'Food'}
        self.assertEqual({m: categorize(m, '', RULES) for m in cases}, cases)

    def test_whole_word_categories(self):
        self.assertEqual(categorize('MOTOROLA STORE', '', RULES), 'Uncategorized')  # not "ola" → Transport


class StoreMixin:
    url = None

    def setUp(self):
        self.db = server.DB(self.url)

    def tearDown(self):
        self.db.close()

    def test_adverts_stored_before_are_ignored_on_start_unless_you_chose(self):
        db = self.db
        for sender, body in (('STC-AD', 'PoS Purchase\nAmount: SAR 30.00\nAt: STC'), ('Zain', 'PoS Purchase\nAmount: SAR 50.00\nAt: ZAIN SHOP 1'),
                             ('Zain', 'PoS Purchase\nAmount: SAR 70.00\nAt: ZAIN SHOP 2')):
            db.run("INSERT INTO transactions (received_at, sender, body, amount_minor, currency, direction, merchant) "
                   "VALUES ('2026-10-01T10:00:00+03:00', :s, :b, 1, 'SAR', 'debit', '')", s=sender, b=body + ' recharge now')
        shop2 = next(t for t in server.transactions(db) if 'SHOP 2' in t['body'])
        server.set_category(db, shop2['id'], 'Bills & Utilities', False)  # you decided this one counts
        db.reread_amounts()
        self.assertEqual(sorted(t['category'] for t in server.transactions(db)), ['Bills & Utilities', 'Ignored', 'Ignored'])

    def test_misread_amounts_are_fixed_on_start(self):
        sms = 'Online Purchase\nBy:1596 ;Visa-Apple Pay\nAmount:57.5 SAR\nAt:Mobily\nBalance:181573.84 SAR\n22/2/26 16:08'
        server.store_sms(self.db, 'AlRajhiBank', sms, '2026-02-22T16:08:00+03:00')
        self.db.run('UPDATE transactions SET amount_minor = 2200')  # what the old parser stored (the date's "22")
        self.db.reread_amounts()
        t = server.transactions(self.db)[0]
        self.assertEqual((t['amount'], t['merchant'], t['account']), (57.5, 'Mobily', 'AlRajhiBank · Card •1596'))

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


    def test_ignored_merchant_stops_counting(self):
        db = self.db
        server.store_sms(db, 'B', 'Outgoing Transfer\nAmount: SAR 2,000.00\nTo: MY OTHER ACCOUNT\nRef 1', '2026-10-01T09:00:00+03:00')
        server.store_sms(db, 'B', 'PoS Purchase\nAmount: SAR 50.00\nAt: PANDA\nRef 2', '2026-10-02T09:00:00+03:00')
        transfer = next(t for t in server.transactions(db) if 'OTHER' in t['body'])
        self.assertEqual(server.summary(db)['months'][0]['spent'], 2050.0)

        server.set_category(db, transfer['id'], server.IGNORED, True)
        server.store_sms(db, 'B', 'Outgoing Transfer\nAmount: SAR 500.00\nTo: MY OTHER ACCOUNT\nRef 3', '2026-10-05T09:00:00+03:00')
        s = server.summary(db)
        self.assertEqual(s['months'][0]['spent'], 50.0)  # neither the old nor the new transfer counts
        self.assertEqual((s['by_category'], list(s['top_merchants'])), ({'Groceries': 50.0}, ['PANDA']))
        self.assertEqual(sum(t['category'] == server.IGNORED for t in s['transactions']), 2)  # still listed, to undo
        self.assertNotIn(server.IGNORED, s['categories'])

        server.set_category(db, transfer['id'], 'Transfers', True)  # un-ignore
        self.assertEqual(server.summary(db)['months'][0]['spent'], 2550.0)

    def test_rule_for_messages_without_a_merchant_matches_similar_wording(self):
        db = self.db
        salary = 'Dear Customer, your A/c XX3456 is credited with Rs.{} on {} by NEFT SAL ACME. Avl Bal Rs.{} -SBI'
        server.store_sms(db, 'SBI', salary.format('75,000.00', '01-09-26', '98,000.00'), '2026-09-01T09:00:00+05:30')
        server.store_sms(db, 'SBI', 'Rs.2000 withdrawn at  from A/c XX3456 on 10Oct26', '2026-10-10T09:00:00+05:30')
        first = server.transactions(db, '2026-09')[0]
        self.assertEqual((first['merchant'], first['category']), ('', 'Income'))
        server.set_category(db, first['id'], 'Salary', True)  # "apply to similar messages"
        server.store_sms(db, 'SBI', salary.format('80,500.50', '01-10-26', '1,02,000.00'), '2026-10-01T09:00:00+05:30')
        cats = {t['body'][:20]: t['category'] for t in server.transactions(db)}
        self.assertEqual(cats['Dear Customer, your '], 'Salary')  # next month's salary follows the rule
        self.assertNotEqual(cats['Rs.2000 withdrawn at'], 'Salary')  # different wording does not

    def test_transfers_between_own_accounts_are_not_counted(self):
        db = self.db
        store = lambda sender, body, at: server.store_sms(db, sender, body, f'2026-10-{at}:00+03:00')
        store('AlRajhi', 'Outgoing Transfer\nAmount: SAR 2,000.00\nTo: SA44*9911\nRef 1', '05T10:00')
        store('SNB', 'حوالة واردة\nمبلغ: SAR 2,000.00\nمن: SA03*1234', '05T10:03')       # the other side, 3 min later
        store('AlRajhi', 'Outgoing Transfer\nAmount: SAR 500.00\nTo: SA44*9911\nRef 2', '06T10:00')
        store('SNB', 'Incoming Transfer\nAmount: SAR 500.00\nFrom: SA03*1234\nRef 3', '06T15:00')  # 5 hours later: no pair
        store('AlRajhi', 'PoS Purchase\nAmount: SAR 300.00\nAt: PANDA', '07T10:00')
        store('SNB', 'Incoming Transfer\nAmount: SAR 300.00\nFrom: AHMED', '07T10:10')  # same amount, but the purchase isn't a transfer
        own = lambda: sorted(t['amount'] for t in server.transactions(db) if t['category'] == server.OWN_TRANSFER)
        self.assertEqual(own(), [2000.0, 2000.0])
        m = server.summary(db)['months'][0]
        self.assertEqual((m['spent'], m['received']), (800.0, 800.0))  # 500 + 300 out, 500 + 300 in; the 2,000 pair is not counted
        self.assertNotIn(server.OWN_TRANSFER, server.summary(db)['by_category'])

        # Marking the unpaired 500 by hand counts it out too; correcting one side of a pair breaks the pair.
        sent500 = next(t for t in server.transactions(db) if 'Ref 2' in t['body'])
        server.set_category(db, sent500['id'], server.OWN_TRANSFER, False)
        sent2000 = next(t for t in server.transactions(db) if 'Ref 1' in t['body'])
        server.set_category(db, sent2000['id'], 'Rent & EMI', False)
        self.assertEqual(own(), [500.0])
        self.assertEqual(server.summary(db)['months'][0]['spent'], 2300.0)

    def test_corrections_follow_the_merchant(self):
        db = self.db
        server.store_sms(db, 'B', 'PoS Purchase\nAmount: SAR 30.00\nAt: ABU SAAD TRADING\nRef 1', '2026-10-01T09:00:00+03:00')
        server.store_sms(db, 'B', 'PoS Purchase\nAmount: SAR 12.00\nAt: ABU SAAD TRADING\nRef 2', '2026-10-02T09:00:00+03:00')
        server.store_sms(db, 'B', 'SAR 99.00 paid to NOON.COM', '2026-10-03T09:00:00+03:00')
        cats = lambda: {t['body'][-5:]: t['category'] for t in server.transactions(db)}
        first = next(t for t in server.transactions(db) if t['body'].endswith('Ref 1'))
        self.assertEqual(first['category'], 'Uncategorized')

        self.assertEqual(len(server.summary(db)['to_review']), 2)  # both bakery visits; NOON is Shopping already
        server.set_category(db, first['id'], 'Food', True)  # "always use this for ABU SAAD TRADING"
        self.assertEqual(server.summary(db)['to_review'], [])  # saving one cleared both from the list
        server.store_sms(db, 'B', 'PoS Purchase\nAmount: SAR 8.00\nAt: ABU SAAD TRADING\nRef 3', '2026-10-04T09:00:00+03:00')
        self.assertEqual([c for k, c in cats().items() if k.startswith('Ref')], ['Food', 'Food', 'Food'])  # past and future

        noon = next(t for t in server.transactions(db) if 'NOON' in t['body'])
        server.set_category(db, noon['id'], 'Gifts', False)  # just this one, and a new category
        self.assertEqual(cats()['N.COM'], 'Gifts')
        self.assertIn('Gifts', server.summary(db)['categories'])
        server.set_category(db, first['id'], 'Groceries', True)  # changing your mind updates the rule
        self.assertEqual(cats()['Ref 2'], 'Groceries')
        with self.assertRaises(KeyError):
            server.set_category(db, 999999, 'Food', True)


class HealthTest(unittest.TestCase):
    def test_region_advice(self):
        from types import SimpleNamespace as NS
        neon = NS(pg=True, reused=False, url='postgresql://u:p@ep-cool-123-pooler.eu-central-1.aws.neon.tech/neondb')
        os.environ['VERCEL_REGION'] = 'iad1'
        h = server.health(neon, 420)
        self.assertEqual((h['server_region'], h['database_region']), ('iad1', 'eu-central-1'))
        self.assertIn('fra1', h['advice'])
        os.environ['VERCEL_REGION'] = 'fra1'
        self.assertEqual(server.health(neon, 30)['advice'], 'Server and database are in the same place.')
        del os.environ['VERCEL_REGION']


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
        self.assertEqual((status, json.loads(out)), (200, {'received': len(SAMPLES[6:]) + 1, 'stored': len(SAMPLES[6:])}))  # the OTP is skipped
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
        status = self.call('POST', '/api/category', json.dumps({'id': txn['id'], 'category': 'Food', 'whole_merchant': True}).encode())
        self.assertEqual((status[0], json.loads(status[1])['category']), (200, 'Food'))  # reports what it shows as now
        self.assertEqual(json.loads(status[1])['summary']['by_category'], {'Food': 15.0})  # and the updated dashboard
        self.assertEqual(json.loads(self.call('GET', '/api/transactions')[1])[0]['category'], 'Food')
        self.assertEqual(fix(category='Uncategorized'), 400)
        # The reply carries every transaction in the request's currency (all months), so the page needs no refetch.
        self.call('POST', '/sms', json.dumps({'sender': 'X', 'body': SAMPLES[0][0], 'received_at': '2026-08-01T10:00:00+05:30'}).encode())
        self.call('POST', '/sms', json.dumps({'sender': 'B', 'body': 'SAR 9.00 paid to ABU SAAD BAKERY', 'received_at': '2026-01-05T10:00:00+03:00'}).encode())
        reply = json.loads(self.call('POST', '/api/category', json.dumps({'id': txn['id'], 'category': 'Groceries'}).encode(),
                                     query='currency=SAR&month=2026-10')[1])
        self.assertEqual(sorted((t['currency'], t['amount'], t['category']) for t in reply['all']),
                         [('SAR', 9.0, 'Groceries'), ('SAR', 15.0, 'Groceries')])  # the merchant rule covers both months
        self.assertEqual(json.loads(self.call('POST', '/api/category', json.dumps({'id': txn['id'], 'category': 'Food'}).encode(),
                                              query='currency=INR')[1])['all'][0]['currency'], 'INR')
        self.assertEqual(fix(category='x' * 41), 400)
        self.assertEqual(self.call('POST', '/api/category', b'{"id": 424242, "category": "Food"}')[0], 404)
        form = urlencode({'id': txn['id'], 'category': 'Food'}).encode()  # cross-site form posts are refused
        self.assertEqual(self.call('POST', '/api/category', form, content_type='application/x-www-form-urlencoded')[0], 400)
        self.assertEqual(self.call('POST', '/api/category', b'{}', auth='')[0], 401)

    def test_search_all_months(self):
        for body, at in ((SAMPLES[6][0], '2026-08-02T10:00:00+03:00'), (SAMPLES[7][0], '2026-10-05T10:00:00+03:00'),
                         (SAMPLES[8][0], '2026-09-17T10:00:00+03:00'), (SAMPLES[0][0], '2026-10-01T10:00:00+05:30')):
            self.call('POST', '/sms', json.dumps({'sender': 'AlRajhiBank', 'body': body, 'received_at': at}).encode())
        find = lambda q, cur='SAR': json.loads(self.call('GET', '/api/search', query=urlencode({'q': q, 'currency': cur}))[1])
        self.assertEqual([t['merchant'] for t in find('hungerstation')['results']], ['HUNGERSTATION'])  # August, not this month
        self.assertEqual(find('card 5678')['count'], 2)                     # all words must match: NOON and the Arabic PANDA SMS
        self.assertEqual(find('٨٧٫٢٥')['results'][0]['merchant'], 'PANDA')  # Arabic digits find Arabic-digit SMS
        self.assertEqual(find('87.25')['count'], 1)                         # and Western digits do too
        self.assertEqual((find('swiggy')['count'], find('swiggy', 'INR')['count']), (0, 1))  # one currency at a time
        self.assertEqual(find('groceries')['spent'], 87.25)
        self.assertEqual(find('   ')['count'], 0)
        self.assertEqual(self.call('GET', '/api/search', query='q=panda', auth='')[0], 401)

    def test_install_files_are_public_and_nothing_else_is(self):
        for path, kind in (('/manifest.webmanifest', 'application/manifest+json'), ('/icon-192.png', 'image/png'), ('/icon-512.png', 'image/png')):
            status, body = self.call('GET', path, auth='')
            self.assertEqual((status, self.headers['Content-Type']), (200, kind))
        manifest = json.loads(self.call('GET', '/manifest.webmanifest', auth='')[1])
        self.assertEqual((manifest['display'], manifest['start_url']), ('standalone', '/'))
        self.assertEqual(self.call('GET', '/server.py', auth='')[0], 401)  # only the listed files are public
        self.assertEqual(self.call('GET', '/api/summary', auth='')[0], 401)

    def test_refund_from_unknown_merchant_is_not_income(self):
        for body in ('Refund\nAmount: SAR 40.00\nCard: *1234\nAt: ZYXW TRADING EST', 'Incoming Transfer\nAmount: SAR 900.00\nFrom: ZYXW TRADING EST'):
            self.call('POST', '/sms', json.dumps({'sender': 'AlRajhiBank', 'body': body}).encode())
        s = json.loads(self.call('GET', '/api/summary')[1])
        cats = {t['amount']: t['category'] for t in s['transactions']}
        self.assertEqual(cats, {40.0: 'Uncategorized', 900.0: 'Income'})  # the refund waits for a category; a transfer in is income
        self.assertEqual([t['amount'] for t in s['to_review']], [40.0])

    def test_latest_marker_changes_when_an_sms_arrives(self):
        before = json.loads(self.call('GET', '/api/latest')[1])
        self.call('POST', '/sms', json.dumps({'sender': 'AlRajhiBank', 'body': SAMPLES[6][0]}).encode())
        after = json.loads(self.call('GET', '/api/latest')[1])
        self.assertEqual((before['count'], after['count']), (0, 1))
        self.assertNotEqual(before['last_id'], after['last_id'])
        self.assertEqual(self.call('GET', '/api/latest', auth='')[0], 401)

    def test_adverts_are_not_counted(self):
        send = lambda sender, body: self.call('POST', '/sms', json.dumps({'sender': sender, 'body': body}).encode())
        send('Mobily-AD', 'PoS Purchase\nAmount: SAR 99.00\nAt: MOBILY STORE')  # looks like a purchase, but the sender is an advert
        send('VM-AMAZON-P', 'Rs 499 paid at AMAZON')                            # Indian promotional sender
        send('AlRajhiBank', 'PoS Purchase\nAmount: SAR 45.00\nAt: PANDA')
        s = json.loads(self.call('GET', '/api/summary')[1])
        self.assertEqual([t['merchant'] for t in s['transactions']], ['PANDA'])
        self.assertEqual([d['outcome'] for d in s['deliveries']][::-1], ['advert, not counted', 'advert, not counted', 'saved'])

    def test_phone_delivery_receipts(self):
        send = lambda body, auth=f'Bearer {TOKEN}': self.call('POST', '/sms', json.dumps({'sender': 'AlRajhiBank', 'body': body}).encode(), auth=auth)
        send(SAMPLES[6][0]); send(SAMPLES[6][0]); send(NOT_TRANSACTIONS[0]); send(SAMPLES[7][0], auth='Bearer wrong-password')
        got = json.loads(self.call('GET', '/api/summary')[1])['deliveries']
        self.assertEqual([d['outcome'] for d in got][::-1],
                         ['saved', 'already had it', 'not a transaction', 'refused: wrong password in the phone app'])
        self.assertEqual([d['body'] for d in got], [None] * 4)  # nothing kept: saved, duplicate, an OTP, refused

    def test_save_a_skipped_sms_as_a_transaction(self):
        send = lambda body: self.call('POST', '/sms', json.dumps({'sender': 'STCPay', 'body': body, 'received_at': '2026-10-21T09:30:00+03:00'}).encode())
        send('Your wallet was topped up with 150.00 SAR from Card ending 1234')  # no spent/received word: skipped
        send('Your OTP is 4821 for SAR 99')                                     # skipped, and never kept
        got = json.loads(self.call('GET', '/api/summary')[1])['deliveries']
        topup, otp = got[1], got[0]
        self.assertEqual((topup['outcome'], otp['body']), ('not a transaction', None))
        self.assertIn('150.00 SAR', topup['body'])
        save = lambda **kw: self.call('POST', '/api/deliveries/save', json.dumps({'id': topup['id'], **kw}).encode())[0]
        self.assertEqual(save(amount='abc', direction='debit', currency='SAR'), 400)
        self.assertEqual(save(amount='150', direction='sideways', currency='SAR'), 400)
        self.assertEqual(save(amount='150.00', direction='debit', currency='SAR'), 200)
        s = json.loads(self.call('GET', '/api/summary', query='month=2026-10&currency=SAR')[1])
        self.assertEqual([(t['amount'], t['direction']) for t in s['transactions']], [(150.0, 'debit')])
        self.assertEqual(s['deliveries'][1]['outcome'], 'saved by you')
        self.assertEqual(save(amount='150', direction='debit', currency='SAR'), 404)  # already saved
        self.assertEqual(self.call('POST', '/api/deliveries/save', b'{}', auth='')[0], 401)

    def test_cards_and_accounts(self):
        for body in (SAMPLES[6][0], SAMPLES[7][0], 'PoS Purchase\nAmount: SAR 10.00\nCard: *1234\nAt: PANDA'):
            self.call('POST', '/sms', json.dumps({'sender': 'AlRajhiBank', 'body': body, 'received_at': '2026-10-05T10:00:00+03:00'}).encode())
        s = json.loads(self.call('GET', '/api/summary')[1])
        self.assertEqual(s['by_account'], {'AlRajhiBank · Card •5678': 1299.0, 'AlRajhiBank · Card •1234': 55.5})
        self.assertEqual(server.account_label('JD-HDFCBK-S', 'Rs 5 debited from A/c XX5678'), 'HDFCBK · Account •5678')

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
