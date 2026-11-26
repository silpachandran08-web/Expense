"""Run: python3 -m unittest -v
Set TEST_DATABASE_URL=postgresql://... to also run the storage tests against Postgres (the table is dropped first)."""
import io
from decimal import Decimal
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

import server
import statement
from parse import categorize, compile_rules, merchant_of, parse

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
    ('Bill payment\nAmount: SAR 120.00\nBiller: STC', 12000, 'SAR', 'debit', 'STC', 'Bills & Utilities'),
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

    def test_dollar_purchase_not_the_available_limit(self):
        sms = ('Transaction Successful! USD 23.60 spent on your IDFC FIRST Bank Credit Card ending XX3662 at ANTHROPIC* CLAUDE SUB '
               'on 10 NOV 2026 at 06:51 PM Avbl Limit: INR 314721.04 If not done by you, call 180010888 for dispute or to block your card SMS CCBLOCK 3662 to 5676732')
        t = parse(sms)
        self.assertEqual((t['amount_minor'], t['currency'], t['direction'], t['merchant']), (2360, 'USD', 'debit', 'ANTHROPIC* CLAUDE SUB'))
        # A purchase in rupees with only the available limit after it, in either order of words.
        self.assertEqual(parse('Rs 499.00 spent on Card XX1234 at SWIGGY. Avl Lmt: Rs 2,50,000')['amount_minor'], 49900)
        self.assertEqual(parse('Available balance: INR 50,000. INR 1,200.00 debited from A/c XX9876 at AMAZON')['amount_minor'], 120000)
        self.assertEqual(parse('$15.99 charged to your card ending 4455 at NETFLIX.COM, spent')['currency'], 'USD')

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
                 'BARNS COFFEES': 'Food', 'MCDONALDS': 'Food',
                 'Tree Food Meals Foundatio': 'Food', "McDonald's Olaya": 'Food', "HARDEE'S": 'Food', 'AL TAZAJ': 'Food',
                 'Krispy Kreme': 'Food', 'Al Romansiah': 'Food', 'Karak House': 'Food', 'Fried Chicken Express': 'Food',
                 'مطعم الكبسة': 'Food', 'وجبات سريعة': 'Food', 'Food Market': 'Groceries', 'Steakhouse 21': 'Food'}
        self.assertEqual({m: categorize(m, '', RULES) for m in cases}, cases)

    def test_everyday_bank_wording_never_picks_a_category(self):
        # Words every bank SMS uses must not decide a category on their own (the whole SMS is checked when the merchant isn't known).
        for sms in ('Fund Transfer\nAmount: SAR 500.00\nTo: ZYXW TRADING EST\nVia Internet Banking',
                    'PoS Purchase\nAmount: SAR 50.00\nCard: *1234 Gold card - mada (Apple Pay)\nAt: QWERTY EST\nTotal incl. VAT\nBalance: SAR 9,000.00',
                    'Auto debit of SAR 20.00 from A/c XX1234 via Mobile Banking. Fees SAR 0.00. Capital Holdings Co',
                    'Online Purchase\nBy:1596 ;Visa-Apple Pay\nAmount:57.5 SAR\nAt:ABC Co\nBalance:181573.84 SAR\n22/2/26 16:08',
                    'شراء عبر نقاط البيع\nبطاقة: ٥٦٧٨\nمبلغ: ٤٥ ريال\nلدى: مؤسسة النور\nالسلام عليكم'):
            with self.subTest(sms=sms):
                self.assertEqual(categorize(merchant_of(sms), sms, RULES), 'Uncategorized')

    def test_more_merchants_find_their_category(self):
        cases = {'BIN DAWOOD': 'Groceries', 'AL SADHAN MARKETS': 'Groceries', 'ملحمة الخير': 'Groceries',
                 'RED SEA MALL': 'Shopping', 'H&M': 'Shopping', 'ARABIAN OUD': 'Shopping', 'TABBY': 'Shopping',
                 'MAWQIF PARKING': 'Transport', 'YELO RENT A CAR': 'Transport', 'ARAMCO STATION 12': 'Transport',
                 'MARRIOTT RIYADH': 'Travel', 'FLYDUBAI': 'Travel', 'فندق الريتز': 'Travel',
                 'SALAM MOBILE': 'Bills & Utilities', 'فاتورة الكهرباء': 'Bills & Utilities',
                 'APPLE MUSIC': 'Subscriptions', 'OPENAI CHATGPT': 'Subscriptions',
                 'SULAIMAN AL HABIB': 'Health', 'FITNESS TIME': 'Health', 'عيادة الأسنان': 'Health',
                 'VOX CINEMAS': 'Entertainment', 'RIYADH SEASON': 'Entertainment',
                 'BARBERSHOP 1': 'Personal care', 'مغسلة النظافة': 'Personal care',
                 'KING SAUD UNIVERSITY': 'Education', 'ABSHER': 'Government & fees', 'EHSAN': 'Charity',
                 'TAWUNIYA': 'Insurance', 'TAKAFUL': 'Insurance', 'DERAYAH': 'Investments'}
        self.assertEqual({m: categorize(m, '', RULES) for m in cases}, cases)

    def test_maestro_card_is_not_food(self):
        sms = 'Purchase on Maestro card XX1234\nAmount: SAR 50.00\nAt: JARIR BOOKSTORE'
        self.assertEqual(categorize(parse(sms)['merchant'], sms, RULES), 'Shopping')

    def test_whole_word_categories(self):
        self.assertEqual(categorize('MOTOROLA STORE', '', RULES), 'Uncategorized')  # not "ola" → Transport


class StoreMixin:
    url = None

    def setUp(self):
        self.db = server.DB(self.url)

    def tearDown(self):
        self.db.close()

    def test_statements_trace_salary_to_the_home_loan(self):
        db = self.db
        db.run("INSERT INTO holdings (kind, name, currency, balance_minor, pattern, as_of) VALUES ('loan', 'SBI home loan', 'INR', 3000000000, '3876', '2025-01-01')")
        # The phone already sent the HDFC transfer's SMS: the statement row is skipped and the SMS used instead.
        server.store_sms(db, 'VM-HDFCBK', 'Rs.1,00,000.00 debited from A/c XX5678 on 01-02-25 by IMPS to SBI SELF', '2025-02-01T09:00:00+05:30')
        first = server.import_statement(db, 'Al Rajhi', 'bank', 'SAR', '1111', 'rajhi.xlsx', RAJHI)
        hdfc = server.import_statement(db, 'HDFC', 'bank', 'INR', '5678', 'hdfc.csv', HDFC)
        server.import_statement(db, 'SBI', 'bank', 'INR', '', 'sbi.pdf', pdf(SBI))
        self.assertEqual((first['read'], first['new']), (3, 3))
        self.assertEqual((hdfc['read'], hdfc['new'], hdfc['from_sms']), (3, 2, 1))
        self.assertEqual(server.import_statement(db, 'HDFC', 'bank', 'INR', '5678', 'hdfc.csv', HDFC)['new'], 0)  # again: nothing new
        every = server.transactions(db)
        find = lambda start: next(t for t in every if t['body'].startswith(start))
        sms = next(t for t in every if not t['statement'])
        self.assertEqual(sms['account'], 'HDFC')  # its A/c XX5678 is the HDFC account
        self.assertEqual(find('International transfer')['category'], server.OWN_TRANSFER)  # sent home: not spending
        self.assertEqual(find('INWARD REMITTANCE ALRAJHI')['category'], server.OWN_TRANSFER)
        self.assertEqual(find('POS PANDA')['category'], 'Groceries')
        t = server.trail(db)
        flows = {(f['from'], f['to']): f for f in t['flows']}
        self.assertEqual(set(flows), {('Salary', 'Al Rajhi'), ('Al Rajhi', 'HDFC'), ('HDFC', 'SBI'), ('SBI', 'SBI home loan')})
        self.assertEqual((flows['Al Rajhi', 'HDFC']['sent'], flows['Al Rajhi', 'HDFC']['got']), ({'SAR': 5000.0}, {'INR': 112500.0}))
        self.assertEqual(len(t['journeys']), 1)
        path = lambda n: [(n['way'], n['account'], n['likely'])] + (path(n['then'][0]) if n['then'] else [])
        self.assertEqual(path(t['journeys'][0]), [('salary', 'Al Rajhi', False), ('sent', 'Al Rajhi', False), ('arrived', 'HDFC', False), ('sent', 'HDFC', True),
                                                  ('arrived', 'SBI', False), ('sent', 'SBI', True), ('loan', 'SBI home loan', False)])
        # Undo the SBI import: its rows go, and the journey stops at SBI's door.
        sid = next(r['id'] for r in server.statements(db)['statements'] if r['account'] == 'SBI')
        server.delete_statement(db, sid)
        self.assertEqual([(w, a) for w, a, _ in path(server.trail(db)['journeys'][0])], [('salary', 'Al Rajhi'), ('sent', 'Al Rajhi'), ('arrived', 'HDFC'), ('paid', 'HDFC')])
        self.assertEqual(server.import_statement(db, 'SBI', 'bank', 'INR', '', 'sbi.pdf', pdf(SBI))['new'], 2)  # can come back

    def test_one_transfer_home_split_to_each_loan_account(self):
        db = self.db
        for name, pattern in (('SBI home loan', '3876'), ('Canara car loan', '9123')):
            db.run("INSERT INTO holdings (kind, name, currency, balance_minor, pattern, as_of) VALUES ('loan', :n, 'INR', 100000000, :p, '2025-01-01')", n=name, p=pattern)
        for sender, body, at in (
                ('D360', 'Outgoing international transfer\nAmount: SAR 10,000.00\nTo: IDFC FIRST BANK India', '2025-02-01T20:00:00+03:00'),
                ('VM-IDFCFB', 'INR 2,25,000.00 credited to A/c XX4444 by inward remittance on 03-02-25', '2025-02-03T10:00:00+05:30'),
                ('VM-IDFCFB', 'INR 1,00,000.00 debited from A/c XX4444 by IMPS to SBI A/c XX2222 on 04-02-25', '2025-02-04T10:00:00+05:30'),
                ('VM-IDFCFB', 'INR 60,000.00 debited from A/c XX4444 by NEFT to CANARA A/c XX3333 on 04-02-25', '2025-02-04T10:05:00+05:30'),
                ('VM-SBIINB', 'Your A/c XX2222 credited INR 1,00,000.00 by IMPS from IDFC on 04-02-25', '2025-02-04T10:01:00+05:30'),
                ('VM-CNRBNK', 'A/c XX3333 credited with INR 60,000.00 by NEFT from IDFC on 04-02-25', '2025-02-04T10:30:00+05:30'),
                ('VM-SBIINB', 'INR 52,000.00 debited from A/c XX2222 towards EMI of loan A/c XX3876 on 05-02-25', '2025-02-05T09:00:00+05:30'),
                ('VM-CNRBNK', 'INR 41,000.00 debited from A/c XX3333 for EMI loan A/c 9123 on 05-02-25', '2025-02-05T09:00:00+05:30')):
            self.assertTrue(server.store_sms(db, sender, body, at), body)
        [j] = server.trail(db)['journeys']
        tree = lambda n: (n['account'].split(' · ')[0], [tree(k) for k in n['then']])
        self.assertEqual(tree(j), ('D360', [('VM-IDFCFB'.replace('VM-', ''), [
            ('IDFCFB', [('SBIINB', [('SBIINB', [('SBI home loan', [])])])]),
            ('IDFCFB', [('CNRBNK', [('CNRBNK', [('Canara car loan', [])])])])])]))

    def test_real_d360_to_idfc_and_on(self):
        db = self.db
        db.run("INSERT INTO holdings (kind, name, currency, balance_minor, emi_minor, as_of) VALUES ('loan', 'Canara home loan', 'INR', 100000000, 1481334, '2026-01-01')")
        for sender, body, at in (
                ('D360', 'International transfer\nAmount: INR 175,000.00 (SAR 6,945.28)\nTo: ravi kumar\n10001234567\nCountry:India\nThrough:THUNES\nOn: 2026-10-26 16:26', '2026-10-26T16:26:00+03:00'),
                ('JD-IDFCFB-S', 'Your a/c no. XXXXXXXX234 is credited by Rs. 175000.00 on 26-Oct-26 by a/c linked to mobile XXXXXXXXX123 (IMPS Ref no 600000000001 ). Team IDFC FIRST Bank', '2026-10-26T19:05:00+05:30'),
                ('JD-IDFCFB-S', 'Your A/c XX1234 debited by Rs. 15,000.00 on 26/10/26; ANIL  KUMAR credited. RRN 300000000002. Available balance Rs. 82,978.82. Team IDFC FIRST Bank', '2026-10-26T19:12:00+05:30'),
                ('JD-IDFCFB-S', 'Your A/C XXXXX951234 is debited by INR 14,813.34 on 26/10/26 19:18. New Bal :INR 65,243.48. Call us for dispute. Team IDFC FIRST Bank', '2026-10-26T19:18:00+05:30')):
            self.assertTrue(server.store_sms(db, sender, body, at), body)
        every = server.transactions(db)
        self.assertEqual({t['account'] for t in every}, {'D360', 'IDFCFB · Account •1234'})  # •234, •1234, •951234: one account
        sent = next(t for t in every if t['account'] == 'D360')
        self.assertEqual((sent['amount'], sent['currency'], sent['category']), (6945.28, 'SAR', server.OWN_TRANSFER))
        [j] = server.trail(db)['journeys']
        tree = lambda n: (n['way'], n['account'], n['amount'], [tree(k) for k in n['then']])
        self.assertEqual(tree(j), ('sent', 'D360', 6945.28, [('arrived', 'IDFCFB · Account •1234', 175000.0, [
            ('paid', 'IDFCFB · Account •1234', 15000.0, []),
            ('sent', 'IDFCFB · Account •1234', 14813.34, [('loan', 'Canara home loan', 14813.34, [])])])]))
        flows = {f['to']: f for f in server.trail(db)['flows']}
        self.assertEqual([p['why'] for p in flows['Canara home loan']['proof']], ["exactly the loan's EMI"])
        self.assertEqual(flows['IDFCFB · Account •1234']['proof'][0]['why'], 'arrived the same day at 25.20 INR per SAR')
        self.assertEqual(len(flows['IDFCFB · Account •1234']['proof'][0]['items']), 2)  # both messages, as proof
        # You say the 14,813.34 wasn't the EMI: it stops being matched to the loan.
        emi = flows['Canara home loan']['proof'][0]['items'][0]['id']
        server.set_category(db, emi, 'Shopping', False)
        self.assertNotIn('Canara home loan', {f['to'] for f in server.trail(db)['flows']})
        # And a transfer you correct stops being a transfer.
        server.set_category(db, sent['id'], 'Shopping', False)
        self.assertEqual([f for f in server.trail(db)['flows'] if f['kind'] != 'salary'], [])

    def test_unmatched_transfer_is_listed_and_can_be_linked_by_hand(self):
        db = self.db
        server.store_sms(db, 'VM-CANBNK', 'Dear Customer, Acct XXX864 credited with INR 13,000.00 on 04/11/26 from V M Kumar; UPI:900000000001; Bal INR 31,769.14-CanaraBank', '2026-11-04T10:00:00+05:30')
        server.store_sms(db, 'JD-IDFCFB-S', 'Your A/c XX1234 debited by Rs. 13,000.00 on 02/11/26; V M KUMAR credited. RRN 300000000003.', '2026-11-02T09:00:00+05:30')  # 2 days earlier
        server.store_sms(db, 'JD-IDFCFB-S', 'Your A/c XX1234 debited by Rs. 900.00 on 03/11/26; SHOP credited. RRN 300000000004.', '2026-11-03T07:30:00+05:30')
        t = server.trail(db)
        canara = next(u for u in t['unmatched'] if 'CANBNK' in u['account'])
        self.assertEqual(t['flows'], [])
        self.assertEqual([(c['amount'], c['fits']) for c in canara['candidates']], [(13000.0, True), (900.0, False)])  # the likely one first
        self.assertEqual(canara['why_not'], 'the two messages are 2 days apart; two SMS are matched automatically up to a day apart')
        server.link_by_hand(db, canara['candidates'][0]['id'], canara['id'])
        t = server.trail(db)
        self.assertEqual([(f['from'], f['to']) for f in t['flows']], [('IDFCFB · Account •1234', 'CANBNK · Account •864')])
        self.assertNotIn(canara['id'], [u['id'] for u in t['unmatched']])
        self.assertEqual({x['category'] for x in server.transactions(db) if x['amount'] == 13000}, {server.OWN_TRANSFER})
        server.set_category(db, canara['id'], 'Salary', False)  # changed your mind: the link goes
        self.assertEqual([(f['from'], f['kind']) for f in server.trail(db)['flows']], [('Salary', 'salary')])
        with self.assertRaises(KeyError):
            server.link_by_hand(db, canara['id'], canara['candidates'][0]['id'])  # backwards

    def test_money_to_parents_on_the_trail(self):
        db = self.db
        server.DB.run(db, "INSERT INTO settings (key, value) VALUES ('allocations', :v)",
                      v=json.dumps({'others': [{'name': 'Parents', 'amount': 15000, 'cur': 'INR'}, {'name': 'Maid', 'amount': 1500, 'cur': 'SAR'}]}))
        for sender, body, at in (
                ('D360', 'International transfer\nAmount: INR 175,000.00 (SAR 6,945.28)\nTo: ravi kumar\nCountry:India', '2026-10-26T16:26:00+03:00'),
                ('JD-IDFCFB-S', 'Your a/c no. XXXXXXXX234 is credited by Rs. 175000.00 on 26-Oct-26 (IMPS Ref no 600000000001 ).', '2026-10-26T19:05:00+05:30'),
                ('JD-IDFCFB-S', 'Your A/c XX1234 debited by Rs. 15,000.00 on 26/10/26; ANIL  KUMAR credited. RRN 300000000002.', '2026-10-26T19:12:00+05:30'),
                ('JD-IDFCFB-S', 'Your A/c XX1234 debited by Rs. 9,000.00 on 27/10/26; SITA DEVI credited. RRN 300000000005.', '2026-10-27T09:00:00+05:30')):
            server.store_sms(db, sender, body, at)
        t = server.trail(db)
        flows = {(f['to'], f['kind']): f for f in t['flows']}
        self.assertEqual(flows['Parents', 'payee']['proof'][0]['why'], 'exactly your monthly Parents payment')
        self.assertEqual(t['journeys'][0]['then'][0]['then'][0]['then'][0]['account'], 'Parents')
        self.assertEqual([u['amount'] for u in t['unmatched']], [9000.0])  # not the 15,000: that's Parents
        # The 9,000 also went to your parents: say so, and it joins them.
        server.set_category(db, t['unmatched'][0]['id'], 'Parents', False)
        t = server.trail(db)
        self.assertEqual(next(f for f in t['flows'] if f['to'] == 'Parents')['count'], 2)
        self.assertEqual(t['unmatched'], [])

    def test_salary_to_the_loan_through_four_banks(self):
        db = self.db
        db.run("INSERT INTO holdings (kind, name, currency, balance_minor, pattern, as_of) VALUES ('loan', 'SBI home loan', 'INR', 3000000000, '3876', '2026-01-01')")
        for sender, body, at in (
                ('SAB', 'Salary Deposit\nAmount: SAR 25,000.00\nAccount: **5555\nFrom: ACME CO', '2026-10-25T09:00:00+03:00'),
                ('SAB', 'Outgoing Transfer\nAmount: SAR 7,000.00\nFrom: **5555\nTo: D360 Bank', '2026-10-26T12:30:00+03:00'),
                ('D360', 'Incoming Transfer: Saudi Awwal Bank\nAmount: SAR 7,000.00\nFrom: R KUMAR\nIBAN: ****0001', '2026-10-26T12:36:00+03:00'),
                ('D360', 'International transfer\nAmount: INR 175,000.00 (SAR 6,945.28)\nTo: ravi kumar\nCountry:India', '2026-10-26T16:26:00+03:00'),
                ('JD-IDFCFB-S', 'Your a/c no. XXXXXXXX234 is credited by Rs. 175000.00 on 26-Oct-26 (IMPS Ref no 600000000001 ).', '2026-10-26T19:05:00+05:30'),
                ('JD-IDFCFB-S', 'Your A/c XX1234 debited by Rs. 1,10,000.00 on 27/10/26; R KUMAR credited. RRN 300000000006.', '2026-10-27T10:00:00+05:30'),
                ('CBSSBI', 'Your A/C XXXXX7777 Credited INR 1,10,000.00 on 27/10/26 -Deposit by transfer from R KUMAR. Avl Bal INR 1,20,000.00', '2026-10-27T10:01:00+05:30'),
                ('CBSSBI', 'Your A/C XXXXX7777 Debited INR 1,05,000.00 on 05/11/26 towards loan A/c XX3876 EMI. Avl Bal INR 15,000.00', '2026-11-05T09:00:00+05:30')):
            self.assertTrue(server.store_sms(db, sender, body, at), body)
        t = server.trail(db)
        path = lambda n: [(n['way'], n['account'].split(' · ')[0])] + (path(n['then'][0]) if n['then'] else [])
        self.assertEqual(path(t['journeys'][0]), [('salary', 'SAB'), ('sent', 'SAB'), ('arrived', 'D360'), ('sent', 'D360'), ('arrived', 'IDFCFB'),
                                                  ('sent', 'IDFCFB'), ('arrived', 'CBSSBI'), ('sent', 'CBSSBI'), ('loan', 'SBI home loan')])
        self.assertIn(('Salary', 'SAB · Account •5555'), {(f['from'], f['to']) for f in t['flows']})

    def test_slow_sms_pairs_only_when_nothing_else_could_be_it(self):
        db = self.db
        server.store_sms(db, 'JD-IDFCFB-S', 'Your A/c XX1234 debited by Rs. 13,000.00 on 04/11/26; V M KUMAR credited. RRN 300000000003.', '2026-11-04T07:30:00+05:30')
        server.store_sms(db, 'VM-CANBNK', 'Acct XXX864 credited with INR 13,000.00 on 04/11/26 from V M Kumar; UPI:900000000001', '2026-11-04T10:00:00+05:30')
        self.assertEqual(len([t for t in server.transactions(db) if t['linked']]), 2)  # 2.5 h apart, and alone: one transfer
        server.store_sms(db, 'VM-SBIINB', 'A/c XX7777 credited INR 13,000.00 by IMPS from R KUMAR', '2026-11-04T12:00:00+05:30')
        self.assertEqual([t['linked'] for t in server.transactions(db)], [None] * 3)  # two places it could have gone: you link it

    def test_salary_transfer_sms_is_money_in(self):
        db = self.db
        stored = server.store_sms(db, 'SAB', 'Salary Transfer\nTo: **001\nAmount: SAR 25,450.00\nOn: 2026-10-25 11:31:07', '2026-10-25T11:31:00+03:00')
        self.assertEqual((stored['amount_minor'], stored['currency'], stored['direction']), (2545000, 'SAR', 'credit'))
        self.assertEqual(server.transactions(db)[0]['category'], 'Salary')

    def test_split_with_someone_on_the_app(self):
        db = self.db
        hari_id = server.new_user(db, 'hari@example.com', 'hari-pass-1')
        wife_id = server.new_user(db, 'wife@example.com', 'wife-pass-1')
        space = lambda uid: db.run('SELECT space FROM app_users WHERE id = :id', id=uid)[0]['space']
        hari, wife = server.DB(self.url, space(hari_id)), server.DB(self.url, space(wife_id))
        try:
            dinner = server.store_sms(hari, 'B', 'Rs.900.00 paid to SWIGGY via UPI', '2026-11-20T21:00:00+05:30')
            cab = server.store_sms(hari, 'B', 'Rs.300.00 paid to UBER via UPI', '2026-11-21T09:00:00+05:30')
            ids = {t['merchant']: t['id'] for t in server.transactions(hari)}
            server.set_friend(hari, ids['SWIGGY'], 'Wife', 45000)          # split 2 ways
            server.set_contact(hari, 'wife', 'Wife@Example.com')            # her email, once: the earlier tag is shared too
            server.set_friend(hari, ids['UBER'], 'WIFE', 30000)              # next time: just the name
            mine = server.shared(hari)
            self.assertEqual([(c['name'], c['email'], c['app']) for c in mine['contacts']], [('Wife', 'wife@example.com', True)])
            hers = server.shared(wife)
            self.assertEqual(sorted((r['what'], r['amount_minor'], r['from_email']) for r in hers['to_me']),
                             [('SWIGGY', 45000, 'hari@example.com'), ('UBER', 30000, 'hari@example.com')])
            # She bought something for him too: the two directions net out on each side.
            server.store_sms(wife, 'B', 'Rs.400.00 paid to ZOMATO via UPI', '2026-11-22T20:00:00+05:30')
            server.set_contact(wife, 'Hari', 'hari@example.com')
            server.set_friend(wife, server.transactions(wife)[0]['id'], 'Hari', 20000)
            self.assertEqual([r['amount_minor'] for r in server.shared(hari)['to_me']], [20000])
            # She pays him back: settled on his side too, and gone from her list.
            self.assertEqual(server.paid_back(wife, 'hari@example.com', 'INR'), 2)
            self.assertEqual(server.shared(wife)['to_me'], [])
            self.assertEqual({t['merchant']: t['settled'] for t in server.transactions(hari) if t['friend']}, {'SWIGGY': True, 'UBER': True})
            # Removing a tag takes it off her list; a friend without an app email shares nothing.
            server.set_friend(wife, server.transactions(wife)[0]['id'], '', 0)
            self.assertEqual(server.shared(hari)['to_me'], [])
            server.set_friend(hari, ids['UBER'], 'Ali', 30000)
            self.assertEqual(server.shared(wife)['to_me'], [])
            # A friend whose email isn't on the app yet: saved, not linked; linked as soon as the admin adds them.
            self.assertEqual((server.on_app(hari, 'wife@example.com'), server.on_app(hari, 'ali@example.com'), server.on_app(hari, 'hari@example.com')), (True, False, False))
            server.set_contact(hari, 'Ali', 'ali@example.com')
            self.assertFalse(next(c for c in server.shared(hari)['contacts'] if c['name'] == 'Ali')['app'])
            ali_id = server.new_user(db, 'ali@example.com', 'ali-pass-12')
            server.relink(db, 'ali@example.com')
            ali = server.DB(self.url, space(ali_id))
            try:
                self.assertEqual([(r['what'], r['amount_minor']) for r in server.shared(ali)['to_me']], [('UBER', 30000)])
                db.run('DELETE FROM app_users WHERE id = :id', id=ali_id)
                db.run('DELETE FROM shared_splits WHERE to_email = :e', e='ali@example.com')
            finally:
                server.drop_space(db, ali.space)
                ali.close()
        finally:
            for d in (hari, wife):
                server.drop_space(db, d.space)
                d.close()

    def test_money_between_two_people_links_both_sides(self):
        db = self.db
        hid, sid = server.new_user(db, 'hari@example.com', 'hari-pass-1'), server.new_user(db, 'silpa@example.com', 'silpa-pass-1')
        space = lambda uid: db.run('SELECT space FROM app_users WHERE id = :id', id=uid)[0]['space']
        hari, silpa = server.DB(self.url, space(hid)), server.DB(self.url, space(sid))
        try:
            server.store_sms(hari, 'SAB', 'Outgoing Transfer\nAmount: SAR 1,500.00\nTo: SILPA\nOn: 2026-10-05 08:06', '2026-10-05T08:06:00+03:00')
            server.store_sms(silpa, 'D360', 'Incoming Internal transfer: D360 Bank\nAmount: SAR 1,500.00\nFrom: HARISH MOHAN\nIBAN: ****0440\nOn: 2026-10-05 08:07', '2026-10-05T08:07:00+03:00')
            server.store_sms(hari, 'D360', 'International transfer\nAmount: INR 25,000.00 (SAR 992.06)\nTo: silpa\nCountry:India', '2026-10-10T10:00:00+03:00')
            server.store_sms(silpa, 'JD-IDFCFB-S', 'Your a/c no. XXXXXXXX777 is credited by Rs. 25000.00 on 10-Oct-26 (IMPS Ref no 600000000009 ).', '2026-10-10T15:00:00+05:30')
            server.set_contact(hari, 'Silpa', 'silpa@example.com')
            server.link_peers(hari)
            self.assertEqual([t['peer'] for t in server.transactions(hari)], [None, None])  # she hasn't saved him yet: nothing linked
            server.set_contact(silpa, 'Harish', 'hari@example.com')  # now both have: all history links
            his, hers = server.transactions(hari), server.transactions(silpa)
            self.assertEqual(sorted((t['amount'], t['currency'], t['peer']['who'], t['category']) for t in his),
                             [(992.06, 'SAR', 'Silpa', 'Family'), (1500.0, 'SAR', 'Silpa', 'Family')])
            self.assertEqual(sorted((t['amount'], t['peer']['who'], t['peer']['account'], t['category']) for t in hers),
                             [(1500.0, 'Harish', 'SAB', 'Family'), (25000.0, 'Harish', 'D360', 'Family')])
            self.assertIn(('SAB', '👤 Silpa'), {(f['from'], f['to']) for f in server.trail(hari)['flows']})
            self.assertIn(('👤 Harish', 'D360'), {(f['from'], f['to']) for f in server.trail(silpa)['flows']})
            # Unlink on her side: gone on both, and a later check doesn't link it again.
            server.unlink_peer(silpa, next(t['id'] for t in hers if t['amount'] == 1500))
            server.link_peers(hari)
            self.assertEqual(sorted(t['amount'] for t in server.transactions(hari) if t['peer']), [992.06])
            self.assertEqual(sorted(t['amount'] for t in server.transactions(silpa) if t['peer']), [25000.0])
            # Two arrivals it could be: it leaves both alone.
            server.store_sms(hari, 'SAB', 'Outgoing Transfer\nAmount: SAR 700.00\nTo: SILPA', '2026-10-20T08:00:00+03:00')
            server.store_sms(silpa, 'D360', 'Incoming Internal transfer: D360 Bank\nAmount: SAR 700.00\nFrom: HARISH MOHAN', '2026-10-20T08:01:00+03:00')
            server.store_sms(silpa, 'D360', 'Incoming Internal transfer: D360 Bank\nAmount: SAR 700.00\nFrom: SISTER', '2026-10-20T09:00:00+03:00')
            server.link_peers(hari)
            self.assertFalse(any(t['peer'] for t in server.transactions(silpa) if t['amount'] == 700))
        finally:
            for d in (hari, silpa):
                server.drop_space(db, d.space)
                d.close()

    def test_data_spaces_are_separate(self):
        db = self.db
        theirs = server.DB(self.url, 'u_901')
        try:
            server.store_sms(theirs, 'B', 'SAR 12.00 paid to HER CAFE', '2026-10-05T10:00:00+03:00')
            server.store_sms(db, 'B', 'SAR 30.00 paid to HIS SHOP', '2026-10-05T10:00:00+03:00')
            self.assertEqual([t['merchant'] for t in server.transactions(theirs)], ['HER CAFE'])
        finally:
            theirs.close()
        again = server.DB(self.url)  # a pooled connection that was just in u_901 must come back to the owner's data
        try:
            self.assertEqual([t['merchant'] for t in server.transactions(again)], ['HIS SHOP'])
            server.drop_space(again, 'u_901')
            with self.assertRaises(ValueError):
                server.drop_space(again, 'public')
        finally:
            again.close()
        fresh = server.DB(self.url, 'u_901')  # dropped: starts empty
        try:
            self.assertEqual(server.transactions(fresh), [])
            server.drop_space(fresh, 'u_901')
        finally:
            fresh.close()

    def test_move_a_persons_data_to_another_account(self):
        db = self.db  # her old site's data (exported), going into her new, empty account
        server.store_sms(db, 'B', 'SAR 45.00 paid to PANDA', '2026-10-05T10:00:00+03:00')
        server.store_sms(db, 'B', 'SAR 12.00 paid to CAFE', '2026-10-06T10:00:00+03:00')
        cafe = next(t for t in server.transactions(db) if t['merchant'] == 'CAFE')
        server.set_category(db, cafe['id'], 'Food', False)
        server.set_friend(db, cafe['id'], 'Ali', 600)
        db.run("INSERT INTO holdings (kind, name, currency, balance_minor, as_of) VALUES ('loan', 'Car loan', 'SAR', 5000000, '2026-10-01')")
        data = json.loads(json.dumps(server.export_data(db), default=str))  # as the downloaded file
        new = server.DB(self.url, 'u_902')
        try:
            for i, (table, rows) in enumerate(data['tables'].items()):
                server.import_rows(new, table, rows, first=i == 0)
            moved = {t['merchant']: (t['category'], t['friend']) for t in server.transactions(new)}
            self.assertEqual(moved, {'PANDA': ('Groceries', ''), 'CAFE': ('Food', 'Ali')})  # categories and splits still point at the right rows
            self.assertEqual([h['name'] for h in new.run('SELECT name FROM holdings')], ['Car loan'])
            server.store_sms(new, 'B', 'SAR 1.00 paid to NEXT', '2026-10-07T10:00:00+03:00')  # new rows after the imported ids
            self.assertEqual(len(server.transactions(new)), 3)
            with self.assertRaisesRegex(ValueError, 'empty account'):
                server.import_rows(new, 'transactions', data['tables']['transactions'], first=True)
            with self.assertRaisesRegex(ValueError, 'columns'):
                server.import_rows(new, 'holdings', [{'name': 'x', 'evil; DROP TABLE holdings': 1}])
        finally:
            server.drop_space(db, 'u_902')
            new.close()

    def test_card_limit_is_kept(self):
        db = self.db
        server.save_holding(db, {'id': None, 'kind': 'card', 'name': 'Visa card', 'currency': 'SAR', 'balance_minor': 820000, 'rate_bp': 0, 'emi_minor': 0,
                                 'due_day': 25, 'pattern': '2080', 'as_of': '2026-11-01', 'auto': 0, 'limit_minor': 1000000})
        server.card_statement(db, 'Payment on your VISA Card ending 2080 is due on 25/11/2026 for SAR 900.00 total amount due SAR 8,300.00', '2026-11-20T10:00:00+03:00')
        card = next(h for h in server.money(db, 'SAR', rate=24.9)['holdings'] if h['kind'] == 'card')
        self.assertEqual((card['balance_now'], card['limit_minor']), (830000, 1000000))  # the reminder updates what's owed, not the limit
        server.save_holding(db, {**{k: card[k] for k in ('id', 'kind', 'name', 'currency', 'balance_minor', 'rate_bp', 'emi_minor', 'due_day', 'pattern', 'as_of', 'auto')}})
        self.assertEqual(db.run('SELECT limit_minor FROM holdings')[0]['limit_minor'], 0)  # a save without a limit clears it (the form always sends it)

    def test_latest_balance_per_account(self):
        db = self.db
        for sender, body, at in (
                ('JD-IDFCFB-S', 'Your A/c XX1234 debited by Rs. 15,000.00 on 26/10/26; ANIL credited. Available balance Rs. 82,978.82.', '2026-10-26T19:12:00+05:30'),
                ('JD-IDFCFB-S', 'Your A/C XXXXX951234 is debited by INR 14,813.34 on 26/10/26 19:18. New Bal :INR 65,243.48.', '2026-10-26T19:18:00+05:30'),
                ('VM-CANBNK', 'Acct XXX864 credited with INR 13,000.00 on 04/11/26 from V M Kumar; UPI:900000000001; Bal INR 31,769.14-CanaraBank', '2026-11-04T10:00:00+05:30'),
                ('ICICI', 'INR 3,200.50 spent on ICICI Bank Card XX9012 on 15-Oct-26 at AMAZON. Avl Limit: INR 1,20,000.00', '2026-10-15T10:00:00+05:30'),
                ('SAB', 'PoS Purchase\nAmount: SAR 45.50\nAt: PANDA\nBalance as on 20/10 is shown in the app', '2026-10-20T10:00:00+03:00')):
            server.store_sms(db, sender, body, at)
        got = {b['account']: (b['kind'], b['amount']) for b in server.balances(server.transactions(db))}
        self.assertEqual(got, {'IDFCFB · Account •1234': ('balance', 65243.48), 'CANBNK · Account •864': ('balance', 31769.14),
                               'ICICI · Card •9012': ('limit', 120000.0)})  # newest per account; no date read as a balance

    def test_balance_carried_month_to_month(self):
        db = self.db
        for body, at in (('Rs.1000.00 credited to A/c XX1234 by NEFT. Avl Bal Rs.1,500.00', '2026-10-10T10:00:00+05:30'),
                         ('Rs.200.00 debited from A/c XX1234 to SHOP. Avl Bal Rs.1,300.00', '2026-11-03T10:00:00+05:30'),
                         ('Rs.100.00 debited from A/c XX1234 to CAFE. Avl Bal Rs.1,200.00', '2026-11-20T10:00:00+05:30')):
            server.store_sms(db, 'VM-HDFCBK', body, at)
        every = server.transactions(db)
        row = lambda m: {k: v for k, v in server.account_months(every, m)[0].items() if k in ('opening', 'in', 'out', 'closing', 'current')}
        self.assertEqual(row('2026-10'), {'opening': 500.0, 'in': 1000.0, 'out': 0.0, 'closing': 1500.0, 'current': 1200.0})
        self.assertEqual(row('2026-11'), {'opening': 1500.0, 'in': 0.0, 'out': 300.0, 'closing': 1200.0, 'current': 1200.0})  # Oct's closing carried in
        self.assertEqual(row('2026-12')['opening'], 1200.0)
        # A provident-fund passbook SMS is no transaction, and its long PF ID is never read as a balance.
        epf = 'Dear XXXXXXXX0370, your passbook balance against MHBAN00123450000012345 is Rs. 2,44,760/-. Contribution of Rs. 7,476/- for due month 032026 has been received. -EPFOHO'
        self.assertIsNone(parse(epf))
        db.run("INSERT INTO transactions (received_at, sender, body, amount_minor, currency, direction, merchant) VALUES ('2026-11-21T10:00:00+05:30', 'VM-EPFOHO', :b, 24476000, 'INR', 'credit', '')", b=epf)
        db.reread_amounts()  # stored before this fix: now Ignored, and out of the balances
        self.assertEqual([b['account'] for b in server.balances(server.transactions(db))], ['HDFCBK · Account •1234'])

    def test_iphone_transfer_in_joins_the_phones_card(self):
        db = self.db
        server.save_account(db, '', 'D360 card 0785', 'SAR', balance=Decimal('861.00'))  # typed now; the SMS below come after
        server.store_sms(db, 'iPhone', 'Purchase\nAmount: SAR 50.00\nCard: **0785\nAt: CAFE', '2030-01-24T10:00:00+03:00')
        server.store_sms(db, 'iPhone', 'Incoming Internal transfer: D360 Bank\nAmount: SAR 2,000.00\nFrom: A PERSON\nIBAN: ****0440', '2030-01-26T07:13:00+03:00')
        every = server.transactions(db)
        self.assertEqual({t['account'] for t in every}, {'D360 card 0785'})
        row = server.account_months(every, '2030-01', db.run('SELECT * FROM accounts'))[0]
        self.assertEqual((row['in'], row['since']), (2000.0, 1950.0))

    def test_rename_remove_and_add_accounts(self):
        db = self.db
        server.store_sms(db, 'SAB', 'PoS Purchase\nAmount: SAR 45.50\nCard: *2080\nAt: PANDA', '2026-11-20T10:00:00+03:00')
        server.store_sms(db, 'VM-EPFOHO', 'Rs.500.00 credited to A/c XX0370 by NEFT', '2026-11-20T10:00:00+05:30')
        accts = lambda: {t['merchant'] or t['body'][:8]: (t['account'], t['category']) for t in server.transactions(db)}
        card = accts()['PANDA'][0]
        server.save_account(db, card, 'SAB Visa')                                   # rename what the SMS called it
        epf = next(a for _, (a, _) in accts().items() if 'EPFOHO' in a)
        server.save_account(db, epf, epf, hidden=True)                             # remove: its messages stop counting
        self.assertEqual(sorted(accts().values()), sorted([('SAB Visa', 'Groceries'), (epf, 'Ignored')]))
        server.save_account(db, epf, epf, hidden=False)
        self.assertNotEqual(dict(accts().values())[epf], 'Ignored')
        with self.assertRaisesRegex(ValueError, 'already has that name'):
            server.save_account(db, epf, 'SAB Visa')
        # Your own account with a balance you typed: its SMS join it by the last 4 digits; the month starts from it.
        server.save_account(db, '', 'SNB salary 7788', 'SAR', balance=Decimal('1000.00'))
        server.store_sms(db, 'SNB', 'Purchase\nAmount: SAR 100.00\nAccount: 7788\nAt: JARIR', '2026-12-31T10:00:00+03:00')
        typed = db.run('SELECT * FROM accounts')
        row = next(r for r in server.account_months(server.transactions(db), '2026-12', typed) if r['account'] == 'SNB salary 7788')
        # Balance now: the 1,000 you typed less the purchase after it
        self.assertEqual((row['current'], row['stated'], row['since'], row['out'], row['closing'], row['typed']), (900.0, 1000.0, -100.0, 100.0, 900.0, True))
        # Credit or debit: read from the card's own SMS, until you set it.
        server.store_sms(db, 'ICICI', 'INR 3,200.50 spent on ICICI Bank Credit Card XX9012 at AMAZON. Avl Limit: INR 1,20,000.00', '2026-11-20T10:00:00+05:30')
        server.store_sms(db, 'AlRajhi', 'PoS Purchase\nBy: mada *5678\nAmount: SAR 20.00\nAt: CAFE', '2026-11-20T10:00:00+03:00')
        kinds = server.account_kinds(server.transactions(db), db.run('SELECT * FROM accounts'))
        self.assertEqual((kinds['ICICI · Card •9012'], kinds['AlRajhi · Card •5678']), (('card', 'sms'), ('debit', 'sms')))
        server.save_account(db, 'AlRajhi · Card •5678', 'Al Rajhi mada', kind='card')  # you know better
        kinds = server.account_kinds(server.transactions(db), db.run('SELECT * FROM accounts'))
        self.assertEqual(kinds['Al Rajhi mada'], ('card', 'set'))
        # A credit card's "available balance" is credit left: never listed as money you hold.
        server.store_sms(db, 'AlRajhiBank', 'Credit Card Purchase\nCard: *1596\nAmount: SAR 10.00\nAt: CAFE\nAvailable balance: SAR 175,746.84', '2026-12-30T10:00:00+03:00')
        self.assertNotIn('AlRajhiBank · Card •1596', [r['account'] for r in server.account_months(server.transactions(db), '2026-12', db.run('SELECT * FROM accounts'))])
        # The balance you typed is the one 'Can I buy it?' sees too.
        self.assertIn(('SNB salary 7788', 1000.0, True), [(b['account'], b['amount'], b.get('typed', False)) for b in server.known_balances(server.transactions(db), db.run('SELECT * FROM accounts'))])
        server.delete_account(db, 'SNB salary 7788')
        self.assertFalse(db.run("SELECT 1 FROM accounts WHERE name = 'SNB salary 7788'"))
        # An account from a statement keeps its rows when renamed.
        server.import_statement(db, 'HDFC', 'bank', 'INR', '', 'h.csv', HDFC)
        server.save_account(db, 'HDFC', 'HDFC savings')
        self.assertEqual({t['account'] for t in server.transactions(db) if t['statement']}, {'HDFC savings'})

    def test_salary_from_the_plan_amount(self):
        db = self.db
        db.run("INSERT INTO settings (key, value) VALUES ('salary', :v)", v=json.dumps({'amount': 25000, 'day': 25, 'currency': 'SAR'}))
        server.store_sms(db, 'SAB', 'Deposit\nAmount: SAR 24,850.00\nAccount: **5555\nFrom: ACME CO', '2026-10-25T09:00:00+03:00')
        server.store_sms(db, 'SAB', 'Deposit\nAmount: SAR 900.00\nAccount: **5555\nFrom: SAMI', '2026-10-26T09:00:00+03:00')
        self.assertEqual(sorted(t['category'] for t in server.transactions(db)), ['Income', 'Salary'])

    def test_card_purchase_never_meets_an_advert_credit(self):
        db = self.db
        server.store_sms(db, 'SAB', 'Online Purchase\nAmount: SAR 48.00\nCard: *2080\nAt: Amazon sa', '2026-10-22T10:00:00+03:00')
        server.store_sms(db, 'JD-TALUXE-S', 'Rs. 1,200 has been credited to your savings with Luxe. T&C apply', '2026-10-22T12:00:00+05:30')
        server.store_sms(db, 'D360', 'Outgoing Transfer\nAmount: SAR 50.00\nTo: SA03*1234', '2026-10-23T10:00:00+03:00')
        server.store_sms(db, 'JD-TALUXE-S', 'Rs. 1,250 has been credited to your savings with Luxe. T&C apply', '2026-10-23T12:00:00+05:30')
        self.assertEqual([t['linked'] for t in server.transactions(db)], [None] * 4)  # a purchase, and a credit to no account

    def test_sms_sar_transfer_meets_the_inr_credit(self):
        db = self.db
        for sender, body, at in (
                ('AlRajhiBank', 'Outgoing International Transfer\nAmount: SAR 5,000.00\nFrom: 1111\nTo: HARI KUMAR\nCountry: India', '2025-01-28T20:00:00+03:00'),
                ('AlRajhiBank', 'حوالة دولية صادرة\nمبلغ: 2,000.00 ريال\nمن: 1111\nإلى: HARI KUMAR', '2025-03-02T09:00:00+03:00'),
                ('VM-HDFCBK', 'Your A/c XX5678 is credited with INR 1,12,450.00 on 30-01-25 by NEFT inward remittance from AL RAJHI BANK', '2025-01-30T11:00:00+05:30'),
                ('VM-HDFCBK', 'INR 45,100.00 credited to A/c XX5678 on 03-03-25. Info: INWARD REMIT ALRAJHI', '2025-03-03T10:00:00+05:30'),
                ('VM-HDFCBK', 'INR 1,12,450.00 credited to A/c XX5678 on 15-03-25 by UPI from RAVI', '2025-03-15T10:00:00+05:30')):  # unrelated
            self.assertTrue(server.store_sms(db, sender, body, at), body)
        every = server.transactions(db)
        amount = {t['id']: t['amount'] for t in every}
        pairs = sorted((t['amount'], amount[t['linked']]) for t in every if t['currency'] == 'SAR' and t['linked'])
        self.assertEqual(pairs, [(2000.0, 45100.0), (5000.0, 112450.0)])
        upi = next(t for t in every if 'RAVI' in t['body'])
        self.assertIsNone(upi['linked'])  # same amount, but weeks later and a UPI payment
        self.assertEqual(sorted(t['category'] == server.OWN_TRANSFER for t in every), [False, True, True, True, True])
        flows = server.trail(db)['flows']
        self.assertEqual([(f['count'], f['sent'], f['got']) for f in flows], [(2, {'SAR': 7000.0}, {'INR': 157550.0})])

    def test_pay_later_goes_on_the_debt_tab(self):
        db = self.db
        sms = 'Split in 6 payment\nconfirmation:\nStore: Petromin Nissan\nOrder: 6,991.92 SAR\nDate: 01/12/2026\nVisit the app for more details.'
        self.assertIsNone(parse(sms))  # not spending: the six card charges are
        self.assertEqual(server.card_statement(db, sms, '2026-11-22T10:00:00+03:00'), 'Pay later · Petromin Nissan')
        server.card_statement(db, sms, '2026-11-22T10:05:00+03:00')  # the phone sent it twice
        plan = [h for h in db.run('SELECT * FROM holdings') if h['name'].startswith('Pay later')]
        self.assertEqual([(h['kind'], h['currency'], h['balance_minor'], h['emi_minor'], h['due_day'], h['auto']) for h in plan],
                         [('loan', 'SAR', 582660, 116532, 22, 1)])
        later = server.money(db, 'SAR', rate=24.9, today='2027-01-25')['holdings']
        self.assertEqual(next(h for h in later if h['name'].startswith('Pay later'))['balance_now'], 582660 - 2 * 116532)  # Dec and Jan paid

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
        store('SNB', 'Incoming Transfer\nAmount: SAR 500.00\nFrom: SA03*1234\nRef 3', '06T15:00')  # 5 hours later: paired on the trail (nothing else is 500)
        store('AlRajhi', 'PoS Purchase\nAmount: SAR 300.00\nAt: PANDA', '07T10:00')
        store('SNB', 'Incoming Transfer\nAmount: SAR 300.00\nFrom: AHMED', '07T10:10')  # same amount, but the purchase isn't a transfer
        own = lambda: sorted(t['amount'] for t in server.transactions(db) if t['category'] == server.OWN_TRANSFER)
        self.assertEqual(own(), [500.0, 500.0, 2000.0, 2000.0])
        m = server.summary(db)['months'][0]
        self.assertEqual((m['spent'], m['received']), (300.0, 300.0))  # only the purchase and AHMED's 300 count
        self.assertNotIn(server.OWN_TRANSFER, server.summary(db)['by_category'])

        # Marking the unpaired 500 by hand counts it out too; correcting one side of a pair breaks the pair.
        sent500 = next(t for t in server.transactions(db) if 'Ref 2' in t['body'])
        server.set_category(db, sent500['id'], server.OWN_TRANSFER, False)
        sent2000 = next(t for t in server.transactions(db) if 'Ref 1' in t['body'])
        server.set_category(db, sent2000['id'], 'Rent & EMI', False)
        self.assertEqual(own(), [500.0])
        self.assertEqual(server.summary(db)['months'][0]['spent'], 2300.0)

    def test_purchases_for_friends_count_only_your_share(self):
        db = self.db
        def store(body, at):
            server.store_sms(db, 'AlRajhi', body, f'2026-10-{at}:00+03:00')
            return next(t['id'] for t in server.transactions(db) if t['body'] == body)
        dinner = store('PoS Purchase\nAmount: SAR 100.00\nAt: HUNGERSTATION', '05T20:00')
        gift = store('PoS Purchase\nAmount: SAR 60.00\nAt: JARIR', '06T12:00')
        store('PoS Purchase\nAmount: SAR 40.00\nAt: PANDA', '06T13:00')
        back = store('Incoming Transfer\nAmount: SAR 50.00\nFrom: ALI', '07T09:00')
        server.set_friend(db, dinner, 'Ali', 5000)   # shared, half each
        server.set_friend(db, gift, 'ali', 6000)     # all his; the same friend whatever the capitals
        server.set_friend(db, back, 'Ali', 0)        # a credit from a friend is him paying back, all of it
        s = server.summary(db)
        self.assertEqual((s['months'][0]['spent'], s['months'][0]['received']), (90.0, 0.0))  # 50 + 0 + 40; the repayment isn't income
        self.assertEqual(s['by_category'], {'Food': 50.0, 'Groceries': 40.0})
        owed = lambda: sum((t['theirs'] if t['direction'] == 'debit' else -t['theirs'])
                           for t in server.transactions(db) if t['friend'] == 'Ali' and not t['settled'])
        self.assertEqual(owed(), 60.0)  # 50 + 60 - 50 paid back
        with self.assertRaises(ValueError):
            server.set_friend(db, dinner, 'Ali', 10001)  # more than the purchase
        server.settle_friend(db, 'Ali', 'SAR')
        self.assertEqual(owed(), 0)
        self.assertEqual(server.summary(db)['months'][0]['spent'], 90.0)  # settling doesn't change your spending
        server.set_friend(db, dinner, '', 0)
        self.assertEqual(server.summary(db)['months'][0]['spent'], 140.0)

    def test_loans_come_down_with_emi_sms_and_plans_pay_them_off(self):
        db = self.db
        h = lambda **kw: {'id': None, 'kind': 'loan', 'currency': 'INR', 'rate_bp': 0, 'emi_minor': 0, 'due_day': 5, 'pattern': '', 'as_of': '2026-09-30', 'auto': 0, **kw}
        server.save_holding(db, h(name='Home 1', balance_minor=12_00000_00, rate_bp=900, emi_minor=1_50000_00, pattern='XX4321'))
        server.save_holding(db, h(name='Car', balance_minor=3_00000_00, rate_bp=1100, emi_minor=20000_00))
        server.save_holding(db, h(kind='saving', name='FD', currency='SAR', balance_minor=50000_00))
        server.store_sms(db, 'HDFCBK', 'Rs 1,50,000.00 debited from A/c XX7788 towards loan A/c XX4321 on 05-10-26', '2026-10-05T09:00:00+05:30')
        server.store_sms(db, 'HDFCBK', 'Rs 1,50,000.00 debited from A/c XX7788 towards loan A/c XX4321 on 05-09-26', '2026-09-05T09:00:00+05:30')  # before as_of
        m = server.money(db, 'INR', 0)
        home = next(x for x in m['holdings'] if x['name'] == 'Home 1')
        self.assertEqual(len(home['payments']), 1)  # only EMIs after the balance was entered
        self.assertEqual(home['balance_now'], 12_00000_00 + 9_00000 - 1_50000_00)  # + one month at 9 %/12, - the EMI
        p = m['plans']
        self.assertTrue(p['as_is']['months'] and p['avalanche']['months'] <= p['as_is']['months'])
        self.assertLessEqual(p['avalanche']['interest'], p['snowball']['interest'])  # highest rate first costs least
        faster = server.money(db, 'INR', 50000_00)['plans']['avalanche']
        self.assertLess(faster['months'], p['avalanche']['months'])
        self.assertLess(faster['interest'], p['avalanche']['interest'])
        car = next(x for x in m['holdings'] if x['name'] == 'Car')
        self.assertGreater(m['prepay'][car['id']]['interest'], 0)  # a lakh prepaid saves interest
        self.assertGreater(m['prepay'][car['id']]['months'], 0)    # and closes it sooner
        # A loan whose EMI doesn't cover its interest is never paid off.
        self.assertIsNone(server.plan([{'id': 1, 'balance_now': 100000, 'rate_bp': 2400, 'emi_minor': 1000}], 0)['months'])
        # 1,000 at 0 % with an EMI of 300 takes 4 months.
        self.assertEqual(server.plan([{'id': 1, 'balance_now': 1000, 'rate_bp': 0, 'emi_minor': 300}], 0)['months'], 4)

    def test_auto_debited_emis_count_themselves(self):
        db = self.db
        server.save_holding(db, {'id': None, 'kind': 'loan', 'name': 'SBI home loan', 'currency': 'INR', 'balance_minor': 40_00000_00,
                                 'rate_bp': 1200, 'emi_minor': 50000_00, 'due_day': 5, 'pattern': '6935', 'as_of': '2026-09-20', 'auto': 1})
        # October's EMI arrived as an SMS; November's didn't: it's assumed taken on the 5th. December's isn't due yet.
        server.store_sms(db, 'SBIINB', 'Rs 50,000.00 debited towards loan a/c XX6935', '2026-10-05T09:00:00+05:30')
        h = server.money(db, 'INR', today='2026-11-21')['holdings'][0]
        self.assertEqual([(p['date'], p['source']) for p in h['payments']], [('2026-10-05', 'sms'), ('2026-11-05', 'auto')])
        after_oct = 40_00000_00 + 40000_00 - 50000_00  # 1 % a month
        self.assertEqual(h['balance_now'], after_oct + round(after_oct / 100) - 50000_00)
        # Turned off, only what was seen counts.
        db.run('UPDATE holdings SET auto = 0')
        self.assertEqual(len(server.money(db, 'INR', today='2026-11-21')['holdings'][0]['payments']), 1)

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


def xlsx(rows):
    from openpyxl import Workbook
    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def pdf(lines, password=None):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=A4, encrypt=password)
    c.setFont('Courier', 8)
    for i, line in enumerate(lines):
        c.drawString(20, 800 - 12 * i, line)
    c.save()
    return out.getvalue()


# Salary in Saudi, sent home, moved between Indian banks, and on to the home loan.
RAJHI = xlsx([['كشف حساب'], ['التاريخ', 'البيان', 'مدين', 'دائن', 'الرصيد'],
              ['2025-01-27', 'راتب Salary ACME', None, 20000, 25000],
              ['2025-01-28', 'International transfer حوالة دولية HDFC', 5000, None, 20000],
              ['2025-01-29', 'POS PANDA', 123.45, None, 19876.55]])
HDFC = b"""Statement of account,,,,,,
Date,Narration,Chq./Ref.No.,Value Dt,Withdrawal Amt.,Deposit Amt.,Closing Balance
30/01/25,INWARD REMITTANCE ALRAJHI,N1,30/01/25,,"1,12,500.00","1,15,000.00"
01/02/25,IMPS-TO SBI SELF,I2,01/02/25,"1,00,000.00",,"15,000.00"
02/02/25,UPI-SWIGGY,U3,02/02/25,450.00,,"14,550.00"
"""
SBI = ['State Bank of India   Account Statement   INR',
       'Txn Date     Description                          Debit          Credit         Balance',
       '05 Feb 2025  TO TRANSFER-INB HOME LOAN 3876      50,000.00                     65,000.00',
       '             A/C XX3876 EMI',
       '01 Feb 2025  BY TRANSFER-IMPS HDFC SELF                         1,00,000.00     1,15,000.00',
       '31 Jan 2025  Opening Balance                                                   15,000.00']


class StatementReaderTest(unittest.TestCase):
    def test_excel_with_arabic_headers(self):
        got = statement.read_statement(RAJHI)
        self.assertEqual([(r['day'], r['amount_minor'], r['direction']) for r in got['rows']],
                         [('2025-01-27', 2000000, 'credit'), ('2025-01-28', 500000, 'debit'), ('2025-01-29', 12345, 'debit')])
        self.assertEqual(got['checked'], 2)

    def test_csv_with_indian_numbers(self):
        rows = statement.read_statement(HDFC)['rows']
        self.assertEqual([(r['amount_minor'], r['direction'], r['balance_minor']) for r in rows],
                         [(11250000, 'credit', 11500000), (10000000, 'debit', 1500000), (45000, 'debit', 1455000)])

    def test_pdf_columns_wrapped_narration_and_newest_first(self):
        got = statement.read_statement(pdf(SBI))
        self.assertEqual([(r['day'], r['amount_minor'], r['direction']) for r in got['rows']],
                         [('2025-02-05', 5000000, 'debit'), ('2025-02-01', 10000000, 'credit')])
        self.assertIn('XX3876 EMI', got['rows'][0]['desc'])  # the next line continues the narration
        self.assertEqual(got['currency'], 'INR')

    def test_pdf_without_headers_reads_direction_from_balance(self):
        rows = statement.read_statement(pdf(['10/03/2025 PURCHASE NOON 1,250.00 8,750.00', '11/03/2025 SALARY 20,000.00 28,750.00',
                                             '12/03/2025 STC PAY TOPUP 500.00 28,250.00']))['rows']
        self.assertEqual([r['direction'] for r in rows], ['debit', 'credit', 'debit'])

    def test_locked_pdf_needs_its_password(self):
        with self.assertRaisesRegex(statement.StatementError, 'locked'):
            statement.read_statement(pdf(SBI, 'secret'))
        self.assertEqual(len(statement.read_statement(pdf(SBI, 'secret'), 'secret')['rows']), 2)

    def test_html_xls_and_dates(self):
        html = (b'<table><tr><td>Txn Date</td><td>Description</td><td>Debit</td><td>Credit</td><td>Balance</td></tr>'
                b'<tr><td>7 Mar 2025</td><td>ATM WDL</td><td>2,000.00</td><td></td><td>8,000.00</td></tr></table>')
        self.assertEqual(statement.read_statement(html)['rows'][0]['amount_minor'], 200000)
        self.assertEqual([statement.day_of(v) for v in ('05/02/2025', '2025-02-05', '05-Feb-25', 'Feb 5, 2025', '13/02/25', '02/13/25', 'x')],
                         ['2025-02-05', '2025-02-05', '2025-02-05', '2025-02-05', '2025-02-13', '2025-02-13', None])
        with self.assertRaisesRegex(statement.StatementError, 'no table'):
            statement.read_statement(b'just,some\ntext,here\n')


class DirectConnectionTest(unittest.TestCase):
    def test_neon_pooler_address_becomes_direct(self):
        pooled = 'postgresql://neondb_owner:pw@ep-cool-cat-a1b2c3-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require'
        self.assertEqual(server.direct(pooled), 'postgresql://neondb_owner:pw@ep-cool-cat-a1b2c3.ap-southeast-1.aws.neon.tech/neondb?sslmode=require')
        self.assertEqual(server.direct('postgresql://postgres@localhost:5433/x'), 'postgresql://postgres@localhost:5433/x')
        self.assertEqual(server.direct(''), '')


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
        cleanup.run('DROP TABLE IF EXISTS transactions, merchant_categories, txn_categories, txn_friends, deleted_sms, holdings, loan_payments, plan_actions, settings, snapshots, accounts, statements, txn_links, app_users, shared_splits, contacts, peer_links')
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

    def test_statement_upload(self):
        import base64
        send = lambda **kw: self.call('POST', '/api/statement', json.dumps({'account': 'HDFC', 'kind': 'bank', 'currency': 'INR', 'file_name': 'h.csv',
                                                                          'data': base64.b64encode(HDFC).decode(), **kw}).encode())
        status, out = send()
        self.assertEqual((status, json.loads(out)['new']), (200, 3))
        self.assertEqual(send(kind='spaceship')[0], 400)
        status, out = send(data=base64.b64encode(b'hello').decode())
        self.assertEqual(status, 422)
        self.assertIn('no table', json.loads(out)['error'])
        listed = json.loads(self.call('GET', '/api/statements')[1])
        self.assertEqual([a['name'] for a in listed['accounts']], ['HDFC'])
        self.assertEqual(self.call('POST', '/api/statement/delete', json.dumps({'id': listed['statements'][0]['id']}).encode())[0], 200)
        self.assertEqual(json.loads(self.call('GET', '/api/trail')[1])['accounts'], [])

    def test_people_get_their_own_data_and_the_admin_manages_them(self):
        browser = dict(auth='')
        os.environ.pop('ADMIN_PASSWORD', None)
        self.assertIn('off', json.loads(self.call('GET', '/api/admin/users', **browser)[1])['error'])
        os.environ['ADMIN_PASSWORD'] = 'admin-pass-123'
        try:
            self.assertEqual(self.call('GET', '/api/admin/users', **browser)[0], 401)
            self.call('POST', '/admin/login', b'password=wrong-guess', content_type='application/x-www-form-urlencoded', **browser)
            self.assertEqual(self.headers['Location'], '/admin?error=1')
            self.call('POST', '/admin/login', b'password=admin-pass-123', content_type='application/x-www-form-urlencoded', **browser)
            admin = dict(auth='', cookie=self.headers['Set-Cookie'].split(';')[0])
            add = lambda **u: self.call('POST', '/api/admin/users', json.dumps(u).encode(), **admin)
            self.assertEqual(add(email='Wife@Example.com', password='wife-pass-1')[0], 200)
            self.assertEqual(add(email='wife@example.com', password='other-pass-2')[0], 400)  # emails are unique
            self.assertEqual(add(email='me@example.com', password='owner-pass-1', owner=True)[0], 200)  # links the existing data
            people = json.loads(self.call('GET', '/api/admin/users', **admin)[1])
            wife = next(u for u in people['users'] if u['email'] == 'wife@example.com')
            self.assertEqual((people['owner_linked'], wife['space'].startswith('u_')), (True, True))

            def login(email, password):
                self.call('POST', '/login', urlencode({'email': email, 'password': password}).encode(), content_type='application/x-www-form-urlencoded', **browser)
                return dict(auth='', cookie=self.headers.get('Set-Cookie', '').split(';')[0])
            her = login('WIFE@example.com ', 'wife-pass-1')
            me = json.loads(self.call('GET', '/api/me', **her)[1])
            self.assertEqual(me['email'], 'wife@example.com')
            # Her phone sends with her sync key; the owner's with the site password. Each sees only their own.
            sms = lambda key, body: self.call('POST', '/sms', json.dumps({'sender': 'B', 'body': body, 'received_at': '2026-10-05T10:00:00+03:00'}).encode(), auth=f'Bearer {key}')
            self.assertEqual(sms(me['sync_key'], 'SAR 12.00 paid to HER CAFE')[0], 201)
            self.assertEqual(sms(TOKEN, 'SAR 30.00 paid to HIS SHOP')[0], 201)
            self.assertEqual(sms(TOKEN, 'SAR 31.00 paid to HIS SHOP 2')[0], 201)
            count = lambda who: json.loads(self.call('GET', '/api/latest', **who)[1])['count']
            self.assertEqual((count(her), count(login('me@example.com', 'owner-pass-1')), count(dict(auth=f'Bearer {TOKEN}'))), (1, 2, 2))
            self.assertEqual(count(login('', TOKEN)), 2)  # the site password alone still opens the owner's data
            # The admin can browse and delete rows in her data, and open her app.
            tables = {t['table']: t['rows'] for t in json.loads(self.call('GET', '/api/admin/tables', query=f'space={wife["space"]}', **admin)[1])}
            self.assertEqual(tables['transactions'], 1)
            row = json.loads(self.call('GET', '/api/admin/rows', query=f'space={wife["space"]}&table=transactions', **admin)[1])['rows'][0]
            self.assertEqual(self.call('POST', '/api/admin/rows/delete', json.dumps({'space': wife['space'], 'table': 'transactions', 'key': {'id': row['id']}}).encode(), **admin)[0], 200)
            self.assertEqual(count(her), 0)
            self.assertEqual(self.call('GET', '/api/admin/tables', query='space=u_999', **admin)[0], 404)  # nobody: no empty space made
            # A new password signs her out; deleting her with her data removes it.
            self.call('POST', '/api/admin/users/password', json.dumps({'id': wife['id'], 'password': 'new-pass-123'}).encode(), **admin)
            self.assertEqual(self.call('GET', '/api/latest', **her)[0], 401)
            self.call('POST', '/api/admin/users/delete', json.dumps({'id': wife['id'], 'data': True}).encode(), **admin)
            self.assertEqual(login('wife@example.com', 'new-pass-123')['cookie'], '')
            self.assertFalse(any('-u_' in f for f in os.listdir(self.tmp.name)))
            self.assertEqual(self.call('GET', '/api/admin/users', auth='', cookie='admin=1.bad')[0], 401)
        finally:
            os.environ.pop('ADMIN_PASSWORD', None)

    def test_username_password_change_and_a_year_signed_in(self):
        db = server.DB()
        try:
            server.new_user(db, 'hari@example.com', 'hari-pass-1')
            server.new_user(db, 'wife@example.com', 'wife-pass-1')
        finally:
            db.close()
        def login(who, pw):
            self.call('POST', '/login', urlencode({'email': who, 'password': pw}).encode(), auth='', content_type='application/x-www-form-urlencoded')
            return self.headers.get('Set-Cookie', '')
        first = login('hari@example.com', 'hari-pass-1')
        self.assertIn(f'Max-Age={365 * 86400}', first)
        hari = dict(auth='', cookie=first.split(';')[0])
        post = lambda path, body, who: self.call('POST', path, json.dumps(body).encode(), **who)
        self.assertIsNone(json.loads(self.call('GET', '/api/me', **hari)[1])['username'])  # the app asks for one
        self.assertEqual(json.loads(post('/api/me/username', {'username': ' Hari.M '}, hari)[1]), {'username': 'hari.m'})
        self.assertEqual(json.loads(self.call('GET', '/api/me', **hari)[1])['username'], 'hari.m')
        wife = dict(auth='', cookie=login('wife@example.com', 'wife-pass-1').split(';')[0])
        self.assertEqual(post('/api/me/username', {'username': 'HARI.M'}, wife)[0], 400)   # taken, whatever the capitals
        self.assertEqual(post('/api/me/username', {'username': 'x@y.com'}, wife)[0], 400)  # never looks like an email
        self.assertIn('session=', login('Hari.M', 'hari-pass-1'))                        # username or email both sign in
        # A new password: the old one and other devices stop working; this device stays signed in.
        other = dict(auth='', cookie=login('hari.m', 'hari-pass-1').split(';')[0])
        self.assertEqual(post('/api/me/password', {'current': 'wrong-one', 'new': 'new-pass-123'}, hari)[0], 400)
        self.assertEqual(post('/api/me/password', {'current': 'hari-pass-1', 'new': 'short'}, hari)[0], 400)
        status, _ = post('/api/me/password', {'current': 'hari-pass-1', 'new': 'new-pass-123'}, hari)
        self.assertEqual(status, 200)
        hari = dict(auth='', cookie=self.headers['Set-Cookie'].split(';')[0])
        self.assertEqual(self.call('GET', '/api/latest', **hari)[0], 200)
        self.assertEqual(self.call('GET', '/api/latest', **other)[0], 401)
        self.assertEqual(login('hari.m', 'hari-pass-1'), '')
        self.assertIn('session=', login('hari.m', 'new-pass-123'))

    def test_phone_sms_link_two_people_as_they_arrive(self):
        db = server.DB()
        try:
            server.new_user(db, 'hari@example.com', 'hari-pass-1')
            server.new_user(db, 'silpa@example.com', 'silpa-pass-1')
            keys = [u['sync_key'] for u in db.run('SELECT sync_key FROM app_users ORDER BY id')]
        finally:
            db.close()
        def login(who, pw):
            self.call('POST', '/login', urlencode({'email': who, 'password': pw}).encode(), auth='', content_type='application/x-www-form-urlencoded')
            return dict(auth='', cookie=self.headers['Set-Cookie'].split(';')[0])
        hari, silpa = login('hari@example.com', 'hari-pass-1'), login('silpa@example.com', 'silpa-pass-1')
        self.call('POST', '/api/contact', json.dumps({'name': 'Silpa', 'email': 'silpa@example.com'}).encode(), **hari)
        self.call('POST', '/api/contact', json.dumps({'name': 'Harish', 'email': 'hari@example.com'}).encode(), **silpa)
        sms = lambda key, body, at: self.call('POST', '/sms', json.dumps({'sender': 'B', 'body': body, 'received_at': at}).encode(), auth=f'Bearer {key}')
        sms(keys[0], 'Outgoing Transfer\nAmount: SAR 1,500.00\nTo: SILPA', '2025-03-05T08:06:00+03:00')  # an old date, as from a backup
        sms(keys[1], 'Incoming Internal transfer: D360 Bank\nAmount: SAR 1,500.00\nFrom: HARISH MOHAN', '2025-03-05T08:07:00+03:00')
        hers = json.loads(self.call('GET', '/api/search', query='q=harish', **silpa)[1])['results'][0]
        self.assertEqual((hers['peer']['who'], hers['category']), ('Harish', 'Family'))

    def test_phone_forwarding_every_message_keeps_only_money(self):
        post = lambda sender, body: json.loads(self.call('POST', '/sms', json.dumps({'sender': sender, 'body': body}).encode())[1])
        self.assertEqual(post('Mum', 'Call me when you are free')['ignored'], 'not about money')
        self.assertEqual(post('+966 50 123 4567', 'I sent you SAR 200 for dinner')['ignored'], 'not about money')  # a person, not a bank
        self.assertTrue(post('D360', 'Purchase\nAmount: SAR 25.00\nAt: STARBUCKS')['stored'])
        db = server.DB()
        try:
            self.assertEqual([d['sender'] for d in db.run('SELECT sender FROM phone_deliveries')], ['D360'])  # chats leave no trace
            self.assertEqual(db.run('SELECT COUNT(*) AS n FROM skipped_sms')[0]['n'], 0)
        finally:
            db.close()

    def test_no_one_sees_anyone_elses_data(self):
        db = server.DB()
        try:
            a_id = server.new_user(db, 'a@example.com', 'a-pass-123')
            b_id = server.new_user(db, 'b@example.com', 'b-pass-123')
            key = {u['id']: u['sync_key'] for u in db.run('SELECT id, sync_key FROM app_users')}
        finally:
            db.close()
        login = lambda email, pw: (self.call('POST', '/login', urlencode({'email': email, 'password': pw}).encode(), auth='',
                                             content_type='application/x-www-form-urlencoded'), self.headers['Set-Cookie'].split(';')[0])[1]
        a, b = dict(auth='', cookie=login('a@example.com', 'a-pass-123')), dict(auth='', cookie=login('b@example.com', 'b-pass-123'))
        # A's money: an SMS, a loan, a salary, a friend split; B adds one ordinary purchase.
        self.call('POST', '/sms', json.dumps({'sender': 'B', 'body': 'SAR 777.77 paid to SECRETSHOP', 'received_at': '2026-11-05T10:00:00+03:00'}).encode(), auth=f'Bearer {key[a_id]}')
        self.call('POST', '/sms', json.dumps({'sender': 'B', 'body': 'SAR 5.00 paid to BAKERY', 'received_at': '2026-11-05T10:00:00+03:00'}).encode(), auth=f'Bearer {key[b_id]}')
        self.call('POST', '/api/money', json.dumps({'kind': 'loan', 'name': 'SECRETLOAN', 'currency': 'INR', 'balance': '4242424', 'rate': '9', 'emi': '31313',
                                                   'due_day': 5, 'match': '', 'as_of': '2026-11-01'}).encode(), **a)
        self.call('POST', '/api/settings', json.dumps({'key': 'salary', 'value': {'amount': 98765, 'day': 25, 'currency': 'SAR'}}).encode(), **a)
        tid = json.loads(self.call('GET', '/api/search', query='q=secretshop', **a)[1])['results'][0]['id']
        self.call('POST', '/api/friend', json.dumps({'id': tid, 'friend': 'SECRETFRIEND', 'theirs': '100'}).encode(), **a)
        secrets_of_a = ('SECRETSHOP', 'SECRETLOAN', 'SECRETFRIEND', '777.77', '98765', '4242424', 'a@example.com')
        pages = ['/api/summary', '/api/transactions', '/api/search?q=secret', '/api/latest', '/api/money', '/api/plan', '/api/settings',
                 '/api/statements', '/api/trail', '/api/balances', '/api/me', '/api/shared']
        seen_by_a = b''.join(self.call('GET', p.split('?')[0], query=(p.split('?') + [''])[1], **a)[1] for p in pages)
        self.assertTrue(all(x.encode() in seen_by_a for x in secrets_of_a[:4]))  # the check can see them where they belong
        for p in pages:
            status, out = self.call('GET', p.split('?')[0], query=(p.split('?') + [''])[1], **b)
            self.assertEqual(status, 200, p)
            for x in secrets_of_a:
                self.assertNotIn(x.encode(), out, f'{x} leaked to B through {p}')
        # And through every MCP tool, with B's connector link.
        for name in server.MCP_TOOLS:
            if not server.MCP_TOOLS[name][3]:
                continue  # read-only tools; the write ones act on B's own data
            args = {'query': 'secret'} if name == 'search_transactions' else {}
            out = self.call('POST', f'/mcp/{key[b_id]}', json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}}).encode(), auth='')[1]
            for x in secrets_of_a:
                self.assertNotIn(x.encode(), out, f'{x} leaked to B through MCP {name}')
        # B changing a category by A's transaction id only ever touches B's own transaction with that id (or none).
        self.call('POST', f'/mcp/{key[b_id]}', json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                                         'params': {'name': 'set_category', 'arguments': {'transaction_id': tid, 'category': 'Hacked'}}}).encode(), auth='')
        self.assertNotIn(b'Hacked', self.call('GET', '/api/transactions', **a)[1])

    def test_mcp_for_claude_and_chatgpt(self):
        rpc = lambda path, msg, **kw: self.call('POST', path, json.dumps(msg).encode(), **{'auth': '', **kw})
        call = lambda path, name, args=None, **kw: json.loads(rpc(path, {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': name, 'arguments': args or {}}}, **kw)[1])['result']
        # The owner's data, with the site password as a bearer header (Claude Desktop / Claude Code).
        status, out = rpc('/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 't'}}},
                          auth=f'Bearer {TOKEN}')
        self.assertEqual((status, json.loads(out)['result']['protocolVersion']), (200, '2025-03-26'))
        self.assertEqual(rpc('/mcp', {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, auth=f'Bearer {TOKEN}')[0], 202)
        tools = {t['name']: t for t in json.loads(rpc('/mcp', {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}, auth=f'Bearer {TOKEN}')[1])['result']['tools']}
        self.assertIn('get_overview', tools)
        self.assertFalse(tools['add_transaction']['annotations']['readOnlyHint'])
        added = call('/mcp', 'add_transaction', {'amount': 45, 'currency': 'SAR', 'merchant': 'Starbucks', 'date': '2026-11-20'}, auth=f'Bearer {TOKEN}')
        self.assertFalse(added['isError'])
        t = added['structuredContent']['added']
        self.assertEqual((t['amount'], t['currency'], t['direction'], t['merchant'], t['date']), (45.0, 'SAR', 'spent', 'Starbucks', '2026-11-20'))
        fixed = call('/mcp', 'set_category', {'transaction_id': t['id'], 'category': 'Food'}, auth=f'Bearer {TOKEN}')['structuredContent']
        self.assertEqual(fixed['transaction']['category'], 'Food')
        listed = call('/mcp', 'list_transactions', {'month': '2026-11'}, auth=f'Bearer {TOKEN}')['structuredContent']
        self.assertEqual([x['merchant'] for x in listed['transactions']], ['Starbucks'])
        self.assertTrue(call('/mcp', 'list_transactions', {'month': 'Nov'}, auth=f'Bearer {TOKEN}')['isError'])  # a clear message, not a crash
        self.assertIn('debts', call('/mcp', 'get_debts', auth=f'Bearer {TOKEN}')['structuredContent'])
        # A person's own link (for connectors that take only a URL) opens only their data.
        db = server.DB()
        try:
            uid = server.new_user(db, 'friend@example.com', 'friend-pass-1')
            key = db.run('SELECT sync_key FROM app_users WHERE id = :id', id=uid)[0]['sync_key']
        finally:
            db.close()
        self.assertEqual(call(f'/mcp/{key}', 'list_transactions')['structuredContent']['count'], 0)
        self.assertEqual(rpc('/mcp/not-a-real-key-123456789', {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})[0], 401)
        self.assertEqual(rpc('/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})[0], 401)  # no key at all
        self.assertEqual(self.call('GET', f'/mcp/{key}', auth='')[0], 405)

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

    def test_friend_endpoints(self):
        self.call('POST', '/sms', json.dumps({'sender': 'B', 'body': 'SAR 80.00 paid to ABU SAAD BAKERY'}).encode())
        txn = json.loads(self.call('GET', '/api/transactions')[1])[0]
        tag = lambda **kw: self.call('POST', '/api/friend', json.dumps({'id': txn['id'], **kw}).encode(), query='currency=SAR')
        status, out = tag(friend='  Sara  ', theirs='40.00')
        reply = json.loads(out)
        self.assertEqual((status, reply['all'][0]['friend'], reply['all'][0]['theirs']), (200, 'Sara', 40.0))
        self.assertEqual(reply['summary']['months'][0]['spent'], 40.0)
        self.assertEqual(tag(friend='Sara', theirs='80.01')[0], 400)
        self.assertEqual(tag(friend='x' * 41, theirs='1')[0], 400)
        settled = json.loads(self.call('POST', '/api/friend/settle', b'{"friend": "Sara"}', query='currency=SAR')[1])
        self.assertTrue(settled['all'][0]['settled'])
        self.assertEqual(json.loads(tag(friend='')[1])['all'][0]['friend'], '')
        self.assertEqual(self.call('POST', '/api/friend', b'{"id": 424242, "friend": "Sara", "theirs": 1}')[0], 404)
        self.assertEqual(self.call('POST', '/api/friend', b'{}', auth='')[0], 401)

    def test_deleted_transactions_stay_deleted(self):
        sms = json.dumps({'sender': 'B', 'body': 'SAR 30.00 paid to ABU SAAD BAKERY'}).encode()
        self.call('POST', '/sms', sms)
        txn = json.loads(self.call('GET', '/api/transactions')[1])[0]
        self.call('POST', '/api/friend', json.dumps({'id': txn['id'], 'friend': 'Sara', 'theirs': 10}).encode())
        status, out = self.call('POST', '/api/delete', json.dumps({'id': txn['id']}).encode(), query='currency=SAR')
        self.assertEqual((status, json.loads(out)['all']), (200, []))
        self.assertEqual(self.call('POST', '/sms', sms)[0], 200)  # the phone sending it again doesn't bring it back
        batch = json.dumps({'messages': [{'sender': 'B', 'body': 'SAR 30.00 paid to ABU SAAD BAKERY'}]}).encode()
        self.assertEqual(json.loads(self.call('POST', '/api/import', batch)[1])['stored'], 0)
        self.assertEqual(json.loads(self.call('GET', '/api/transactions')[1]), [])
        self.assertEqual(self.call('POST', '/api/delete', json.dumps({'id': txn['id']}).encode())[0], 404)
        self.assertEqual(self.call('POST', '/api/delete', b'{}', auth='')[0], 401)

    def test_money_endpoints(self):
        loan = {'kind': 'loan', 'name': 'Home loan', 'currency': 'INR', 'balance': '45,00,000', 'rate': '8.5', 'emi': '42000', 'due_day': 5, 'match': '4321'}
        status, out = self.call('POST', '/api/money', json.dumps(loan).encode(), query='currency=INR&extra=10000')
        m = json.loads(out)
        self.assertEqual((status, m['holdings'][0]['balance_minor'], m['holdings'][0]['rate_bp'], m['extra']), (200, 4500000_00, 850, 10000.0))
        self.assertIsNotNone(m['plans']['avalanche']['months'])
        self.assertEqual(self.call('POST', '/api/money', json.dumps({**loan, 'rate': '75'}).encode())[0], 400)
        self.assertEqual(self.call('POST', '/api/money', json.dumps({**loan, 'kind': 'gold'}).encode())[0], 400)
        self.assertEqual(self.call('POST', '/api/money', json.dumps({**loan, 'id': 999}).encode())[0], 404)
        hid = m['holdings'][0]['id']
        self.assertEqual(json.loads(self.call('POST', '/api/money', json.dumps({**loan, 'id': hid, 'balance': '44,00,000'}).encode())[1])['holdings'][0]['balance_minor'], 4400000_00)
        self.assertEqual(json.loads(self.call('POST', '/api/money/delete', json.dumps({'id': hid}).encode())[1])['holdings'], [])
        self.assertEqual(self.call('GET', '/api/money', auth='')[0], 401)

    def test_exchange_rate_falls_back_and_rejects_nonsense(self):
        server._rate.clear()
        answers = iter([OSError('down'), {'rates': {'INR': 93.375}}, {'price': 3539.0}])  # first service down, the dollar rate answers; then gold
        class Reply(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): pass
        def fake(url, timeout):
            a = next(answers)
            if isinstance(a, Exception):
                raise a
            return Reply(json.dumps(a).encode())
        real, server.urlopen = server.urlopen, fake
        try:
            status, out = self.call('GET', '/api/rate')
            self.assertEqual((status, json.loads(out)['rate'], json.loads(out)['usd'], json.loads(out)['source']), (200, 24.9, 93.375, 'frankfurter.dev'))
            self.assertEqual(json.loads(out)['gold_usd_oz'], 3539.0)
            self.assertEqual(self.call('GET', '/api/rate')[0], 200)  # kept: no second lookup (the fake has no answers left)
            server._rate.clear()
            server.urlopen = lambda url, timeout: Reply(b'{"rates": {"INR": 0.04, "USD": 0.27}}')  # upside down: refused
            self.assertEqual(self.call('GET', '/api/rate')[0], 503)
        finally:
            server.urlopen = real
            server._rate.clear()

    def test_plan_checkin_and_recorded_payments(self):
        loan = {'kind': 'loan', 'name': 'Car loan', 'currency': 'INR', 'balance': '600000', 'rate': '12', 'emi': '20000', 'due_day': 10,
                'match': '9911', 'as_of': '2026-10-31'}
        hid = json.loads(self.call('POST', '/api/money', json.dumps(loan).encode())[1])['holdings'][0]['id']
        pay = lambda **kw: self.call('POST', '/api/money/pay', json.dumps({'holding_id': hid, **kw}).encode())
        # An EMI ticked by hand plus an extra payment in the same month: one month's interest (1 %), both amounts off.
        pay(amount='20000', paid_on='2026-11-10', note='EMI')
        m = json.loads(pay(amount='50000', paid_on='2026-11-20', note='Extra')[1])
        self.assertEqual(m['holdings'][0]['balance_now'], (600000 + 6000 - 20000 - 50000) * 100)
        # The EMI SMS shows up after all: the hand-ticked EMI for that month no longer counts twice.
        self.call('POST', '/sms', json.dumps({'sender': 'JD-ICICIB', 'body': 'Rs 20,000.00 debited towards loan a/c XX9911', 'received_at': '2026-11-10T10:00:00+05:30'}).encode())
        h = json.loads(self.call('GET', '/api/money')[1])['holdings'][0]
        self.assertEqual(h['balance_now'], (600000 + 6000 - 20000 - 50000) * 100)
        self.assertEqual(sorted(p['source'] for p in h['payments']), ['Extra', 'sms'])
        self.assertEqual(pay(amount='0', paid_on='2026-11-20')[0], 400)
        self.assertEqual(self.call('POST', '/api/money/pay', json.dumps({'holding_id': 999, 'amount': '1', 'paid_on': '2026-11-20'}).encode())[0], 404)
        extra_pid = next(p['pid'] for p in h['payments'] if p['source'] == 'Extra')
        h = json.loads(self.call('POST', '/api/money/pay/delete', json.dumps({'id': extra_pid}).encode())[1])['holdings'][0]
        self.assertEqual(h['balance_now'], (600000 + 6000 - 20000) * 100)
        # The check-in: plan an extra payment, then mark it done; last month's actions come back for the review.
        act = lambda **kw: self.call('POST', '/api/plan', json.dumps({'month': '2026-12', 'kind': 'extra', 'ref': str(hid), **kw}).encode())
        act(target='30000', status='planned')
        p = json.loads(act(target='30000', actual='25000', status='done')[1])
        self.assertEqual([(a['status'], a['target_minor'], a['actual_minor']) for a in p['actions']], [('done', 3000000, 2500000)])
        self.call('POST', '/api/plan', json.dumps({'month': '2027-01', 'kind': 'limit', 'ref': 'Food', 'target': '1200', 'status': 'planned'}).encode())
        jan = json.loads(self.call('GET', '/api/plan', query='month=2027-01')[1])
        self.assertEqual((jan['previous_month'], len(jan['previous']), jan['actions'][0]['ref']), ('2026-12', 1, 'Food'))
        self.assertEqual(act(status='maybe')[0], 400)
        self.assertEqual(self.call('GET', '/api/plan', query='month=junk')[0], 400)

    def test_credit_card_debt_follows_purchases_and_payments(self):
        card = {'kind': 'card', 'name': 'SNB credit card', 'currency': 'SAR', 'balance': '5000', 'rate': '30', 'emi': '1000', 'due_day': 28,
                'match': '4455', 'as_of': '2026-10-31'}
        self.call('POST', '/api/money', json.dumps(card).encode())
        sms = lambda body, at: self.call('POST', '/sms', json.dumps({'sender': 'SNB', 'body': body, 'received_at': at}).encode())
        sms('PoS Purchase\nAmount: SAR 300.00\nCard: *4455\nAt: PANDA', '2026-11-03T10:00:00+03:00')     # adds to the debt
        sms('Credit card payment received\nAmount: SAR 1,000.00\nCard: *4455', '2026-11-25T10:00:00+03:00')  # takes it off
        sms('PoS Purchase\nAmount: SAR 80.00\nCard: *9999\nAt: PANDA', '2026-11-04T10:00:00+03:00')      # another card: ignored
        m = json.loads(self.call('GET', '/api/money', query='currency=INR&rate=25')[1])
        c = m['holdings'][0]
        self.assertEqual((c['balance_now'], len(c['charges']), [p['amount'] for p in c['payments']]), (4300_00, 1, [1000.0]))
        self.assertEqual(m['rate'], 25.0)
        self.assertIn(str(c['id']), m['prepay'])  # the card is in the payoff plan, converted to INR
        self.assertIsNotNone(m['plans']['avalanche']['months'])
        # A card payment can be recorded by hand too.
        m = json.loads(self.call('POST', '/api/money/pay', json.dumps({'holding_id': c['id'], 'amount': '300', 'paid_on': '2026-11-26'}).encode(), query='rate=25')[1])
        self.assertEqual(m['holdings'][0]['balance_now'], 4000_00)

    def test_card_due_reminder_adds_and_updates_the_card(self):
        nov = 'Payment on your VISA Card is due on 25/11/2026 for SAR 8,622.00 total amount due SAR 13,845.84 , Your SADAD No 16001880067 . Please ignore if already paid'
        self.call('POST', '/sms', json.dumps({'sender': 'SNB-AlAhli', 'body': nov, 'received_at': '2026-11-18T10:00:00+03:00'}).encode())
        c = json.loads(self.call('GET', '/api/money', query='rate=25')[1])['holdings'][0]
        self.assertEqual((c['kind'], c['name'], c['currency'], c['balance_minor'], c['emi_minor'], c['due_day'], c['as_of']),
                         ('card', 'Visa card', 'SAR', 1384584, 862200, 25, '2026-11-18'))
        self.assertEqual(json.loads(self.call('GET', '/api/transactions')[1]), [])  # a reminder is not spending
        self.assertIn('card due: Visa card', json.loads(self.call('GET', '/api/summary')[1])['deliveries'][0]['outcome'])
        # Next month's reminder (via the phone app's batch import) updates the same card; an older one changes nothing.
        dec = nov.replace('25/11/2026', '25/12/2026').replace('8,622.00', '7,900.00').replace('13,845.84', '11,200.00')
        oct_ = nov.replace('25/11/2026', '25/10/2026').replace('13,845.84', '15,000.00')
        batch = {'messages': [{'sender': 'SNB', 'body': dec, 'received_at': '2026-12-18T10:00:00+03:00'},
                              {'sender': 'SNB', 'body': oct_, 'received_at': '2026-10-18T10:00:00+03:00'}]}
        self.call('POST', '/api/import', json.dumps(batch).encode())
        cards = [h for h in json.loads(self.call('GET', '/api/money', query='rate=25')[1])['holdings'] if h['kind'] == 'card']
        self.assertEqual([(h['balance_minor'], h['emi_minor'], h['as_of']) for h in cards], [(1120000, 790000, '2026-12-18')])

    def test_salary_settings_and_debt_options(self):
        salary = {'amount': 18000, 'day': 27, 'currency': 'SAR'}
        status, out = self.call('POST', '/api/settings', json.dumps({'key': 'salary', 'value': salary}).encode())
        self.assertEqual((status, json.loads(out)['salary']), (200, salary))
        self.call('POST', '/api/settings', json.dumps({'key': 'debt_choice', 'value': {'option': 'fast', 'strategy': 'avalanche'}}).encode())
        got = json.loads(self.call('GET', '/api/settings')[1])
        self.assertEqual(sorted(got), ['debt_choice', 'salary'])
        self.assertEqual(self.call('POST', '/api/settings', json.dumps({'key': 'password', 'value': 1}).encode())[0], 400)
        self.assertEqual(self.call('POST', '/api/settings', b'{"key": "salary", "value": "' + b'x' * 20000 + b'"}')[0], 400)  # too big
        loan = {'kind': 'loan', 'name': 'Car', 'currency': 'INR', 'balance': '500000', 'rate': '10', 'emi': '15000', 'due_day': 5}
        self.call('POST', '/api/money', json.dumps(loan).encode())
        opts = json.loads(self.call('GET', '/api/money', query='options=0,10000,40000')[1])['options']
        self.assertEqual([o['extra'] for o in opts], [0, 10000, 40000])
        months = [o['avalanche']['months'] for o in opts]
        self.assertTrue(months[0] > months[1] > months[2])  # more extra, sooner
        self.assertGreater(opts[0]['avalanche']['interest'], opts[2]['avalanche']['interest'])

    def test_optional_emi_curve_and_history(self):
        gold = {'kind': 'loan', 'name': 'Gold loan', 'currency': 'INR', 'balance': '690899', 'rate': '10.95', 'emi': '', 'due_day': 3}
        status, out = self.call('POST', '/api/money', json.dumps(gold).encode(), query='rate=25')
        m = json.loads(out)
        self.assertEqual((status, m['holdings'][0]['emi_minor']), (200, 0))  # no EMI: allowed
        self.assertIsNone(m['plans']['as_is']['months'])  # nothing paid: it never closes
        self.assertGreater(m['plans']['as_is']['curve'][11], 690899)  # and grows with interest
        p = json.loads(self.call('GET', '/api/money', query='rate=25&extra=30000')[1])['plans']['avalanche']
        self.assertTrue(p['months'] and p['curve'][-1] == 0 and len(p['curve']) == p['months'])
        h = json.loads(self.call('GET', '/api/money', query='rate=25')[1])['history']
        self.assertEqual([(x['debt_minor'], x['savings_minor']) for x in h], [(69089900, 0)])  # one snapshot a month, replaced

    def test_us_stocks_in_dollars(self):
        stocks = {'kind': 'saving', 'name': 'US stocks', 'currency': 'USD', 'balance': '12000'}
        m = json.loads(self.call('POST', '/api/money', json.dumps(stocks).encode(), query='rate=25&usd=93.75')[1])
        self.assertEqual((m['holdings'][0]['currency'], m['usd']), ('USD', 93.75))
        h = json.loads(self.call('GET', '/api/money', query='rate=25&usd=93.75')[1])['history']
        self.assertEqual(h[-1]['savings_minor'], 12000 * 9375)  # counted in ₹ at the dollar rate
        self.assertEqual(self.call('POST', '/api/money', json.dumps({**stocks, 'kind': 'loan'}).encode())[0], 400)  # dollars: savings only

    def test_gold_by_grams_and_karat(self):
        gold = {'kind': 'saving', 'name': 'Gold jewellery', 'currency': 'GOLD', 'balance': '50', 'rate': '22'}
        m = json.loads(self.call('POST', '/api/money', json.dumps(gold).encode(), query='rate=25&usd=93.75&gold=10000')[1])
        g = m['holdings'][0]
        self.assertEqual((g['currency'], g['balance_minor'], g['rate_bp']), ('GOLD', 5000, 2200))  # 50 g, 22K
        h = json.loads(self.call('GET', '/api/money', query='rate=25&usd=93.75&gold=10000')[1])['history']
        self.assertEqual(h[-1]['savings_minor'], round(5000 * 2200 / 2400 * 10000))  # 50 g × 22/24 × ₹10,000 a gram
        self.assertEqual(self.call('POST', '/api/money', json.dumps({**gold, 'rate': '23'}).encode())[0], 400)  # no 23K
        self.assertEqual(self.call('POST', '/api/money', json.dumps({**gold, 'kind': 'loan'}).encode())[0], 400)

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

    def test_reverse_transaction_from_iphone_shortcut(self):
        # An iPhone Shortcut that sends the whole message as its sender too; "Reverse Transaction" is money back.
        body = 'Reverse Transaction\nBy:0785;Visa\nAt:Amazon\nAmount: SAR 49.00\nOn: 2026-11-19 21:40'
        self.assertEqual(self.call('POST', '/sms', json.dumps({'sender': body, 'body': body}).encode())[0], 201)
        t = json.loads(self.call('GET', '/api/transactions')[1])[0]
        self.assertEqual((t['direction'], t['amount'], t['category']), ('credit', 49.0, 'Shopping'))  # a refund, not income
        self.assertTrue(t['account'].startswith('iPhone'))

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

    def test_crash_reports_its_reason_and_startup_cleanup_cannot_break_requests(self):
        original = server.summary
        server.summary = lambda *a, **k: 1 / 0
        try:
            status, out = self.call('GET', '/api/summary')
        finally:
            server.summary = original
        self.assertEqual((status, json.loads(out)['error']), (500, 'ZeroDivisionError: division by zero'))
        original = server.DB.reread_amounts
        server.DB.reread_amounts = lambda self: 1 / 0  # the start-up clean-up fails...
        server.DB._schema_ready.clear()
        try:
            self.assertEqual(self.call('GET', '/api/summary')[0], 200)  # ...and the request still works
        finally:
            server.DB.reread_amounts = original

    def test_phone_sync_app_batch_leaves_a_receipt(self):
        batch = {'messages': [{'sender': 'AlRajhiBank', 'body': s, 'received_at': '1792002600000'} for s, *_ in SAMPLES[6:9]]}
        status, out = self.call('POST', '/api/import', json.dumps(batch).encode())  # Bearer token, as the app sends
        self.assertEqual((status, json.loads(out)['stored']), (200, 3))
        self.call('POST', '/api/import', json.dumps(batch).encode())             # the next run sends them again: nothing new
        got = [d['outcome'] for d in json.loads(self.call('GET', '/api/summary')[1])['deliveries']]
        self.assertEqual(got[:2], ['0 new of 3 checked', '3 new of 3 checked'])

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
