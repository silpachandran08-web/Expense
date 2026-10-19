"""Run: python3 -m unittest -v"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('EXPENSE_TOKEN', 'test-token-1234567890')
import server
from parse import categorize, parse

RULES = json.loads((Path(__file__).parent / 'rules.json').read_text())

SAMPLES = [
    # (sms, amount_paise, direction, merchant, category)
    ('Sent Rs.250.00 From HDFC Bank A/C *1234 To SWIGGY On 18/10/26 Ref 628712345678 Not You? Call 18002586161',
     25000, 'debit', 'SWIGGY', 'Food'),
    ('Rs 1,499.00 debited from A/c XX5678 on 12-Oct-26 to VPA netflix.upi@hdfcbank (UPI Ref No 6281234567). -ICICI Bank',
     149900, 'debit', 'netflix.upi@hdfcbank', 'Subscriptions'),
    ('INR 3,200.50 spent on ICICI Bank Card XX9012 on 15-Oct-26 at AMAZON PAY INDIA. Avl Limit: INR 1,20,000.00',
     320050, 'debit', 'AMAZON PAY INDIA', 'Shopping'),
    ('Dear Customer, your A/c XX3456 is credited with Rs.75,000.00 on 01-10-26 by NEFT SALARY ACME LTD. Avl Bal Rs.98,000.00 -SBI',
     7500000, 'credit', '', 'Salary'),
    ('Rs.2000 withdrawn at ATM S1AW000123 from A/c XX3456 on 10Oct26. Avl bal Rs 5000',
     200000, 'debit', 'ATM S1AW000123', 'Cash'),
    ('₹180 paid to Uber India via Axis Bank UPI on 19-10-2026. Ref 123',
     18000, 'debit', 'Uber India', 'Transport'),
]

NOT_TRANSACTIONS = [
    '123456 is your OTP for txn of Rs 500 at AMAZON. Do not share.',
    'Your credit card bill of Rs 12,000 is due on 25-Oct-26.',
    'You are pre-approved for a loan of Rs 5,00,000 to be credited instantly!',
    'Rs 300 will be debited from your account on 20-Oct for your SIP mandate.',
    'Swiggy has requested money from you on Google Pay. Amount: Rs 120',
    'Hi, your package is out for delivery.',
]


class ParseTest(unittest.TestCase):
    def test_transactions(self):
        for sms, paise, direction, merchant, category in SAMPLES:
            with self.subTest(sms=sms):
                t = parse(sms)
                self.assertIsNotNone(t)
                self.assertEqual((t['amount_paise'], t['direction'], t['merchant']), (paise, direction, merchant))
                self.assertEqual(categorize(t['merchant'], sms, RULES), category)

    def test_non_transactions_skipped(self):
        for sms in NOT_TRANSACTIONS:
            with self.subTest(sms=sms):
                self.assertIsNone(parse(sms))

    def test_whole_word_categories(self):
        self.assertEqual(categorize('MOTOROLA STORE', '', RULES), 'Uncategorized')  # not "ola" → Transport


class StoreTest(unittest.TestCase):
    def test_store_dedupes_and_summarizes(self):
        with tempfile.TemporaryDirectory() as d:
            conn = sqlite3.connect(os.path.join(d, 't.db'))
            conn.row_factory = sqlite3.Row
            conn.execute(server.SCHEMA)
            sms = SAMPLES[0][0]
            self.assertTrue(server.store_sms(conn, 'HDFCBK', sms, '2026-10-18T10:00:00+05:30'))
            self.assertIsNone(server.store_sms(conn, 'HDFCBK', sms, '2026-10-18T10:00:05+05:30'))  # retry
            self.assertIsNone(server.store_sms(conn, 'HDFCBK', NOT_TRANSACTIONS[0], '2026-10-18T10:00:00+05:30'))
            server.store_sms(conn, 'SBIINB', SAMPLES[3][0], '2026-10-01T09:00:00+05:30')
            s = server.summary(conn)
            self.assertEqual(s['month'], '2026-10')
            self.assertEqual(s['months'], [{'month': '2026-10', 'spent': 250.0, 'received': 75000.0}])
            self.assertEqual(s['by_category'], {'Food': 250.0})
            conn.close()


if __name__ == '__main__':
    unittest.main()
