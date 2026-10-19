"""Expense server: receives SMS from the phone, stores transactions, serves analytics.

Run:  EXPENSE_TOKEN=some-long-secret python3 server.py
"""
import base64
import binascii
import hmac
import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from parse import categorize, parse

HERE = Path(__file__).parent
TOKEN = os.environ.get('EXPENSE_TOKEN', '')
DB = os.environ.get('EXPENSE_DB', str(HERE / 'expense.db'))
RULES = HERE / 'rules.json'
MAX_BODY = 16 * 1024

SCHEMA = '''CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    received_at TEXT NOT NULL,
    sender TEXT NOT NULL,
    body TEXT NOT NULL UNIQUE,  -- shortcut: dedupes phone retries; two byte-identical SMS count once. Add a phone-side message id if that bites.
    amount_paise INTEGER NOT NULL,
    direction TEXT NOT NULL,
    merchant TEXT NOT NULL
)'''


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def store_sms(conn, sender, body, received_at):
    """Parse and insert one SMS. Returns the stored row as a dict, or None if it was skipped/duplicate."""
    txn = parse(body)
    if txn is None:
        return None
    cur = conn.execute(
        'INSERT OR IGNORE INTO transactions (received_at, sender, body, amount_paise, direction, merchant) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (received_at, sender, body, txn['amount_paise'], txn['direction'], txn['merchant']))
    conn.commit()
    return {**txn, 'received_at': received_at} if cur.rowcount else None


def transactions(conn, month=None):
    # Categories are computed on read, so editing rules.json re-categorizes history instantly.
    rules = json.loads(RULES.read_text())
    sql, args = 'SELECT * FROM transactions', ()
    if month:
        sql, args = sql + ' WHERE substr(received_at, 1, 7) = ?', (month,)
    rows = conn.execute(sql + ' ORDER BY received_at DESC', args).fetchall()
    return [{
        'id': r['id'], 'received_at': r['received_at'], 'sender': r['sender'], 'body': r['body'],
        'amount': r['amount_paise'] / 100, 'direction': r['direction'], 'merchant': r['merchant'],
        'category': categorize(r['merchant'], r['body'], rules),
    } for r in rows]


def summary(conn, month=None):
    rows = transactions(conn)
    months = defaultdict(lambda: {'spent': 0, 'received': 0})
    for t in rows:
        months[t['received_at'][:7]]['spent' if t['direction'] == 'debit' else 'received'] += round(t['amount'] * 100)
    month = month or max(months, default=datetime.now().strftime('%Y-%m'))

    by_category, by_merchant = defaultdict(int), defaultdict(int)
    for t in rows:
        if t['received_at'].startswith(month) and t['direction'] == 'debit':
            paise = round(t['amount'] * 100)
            by_category[t['category']] += paise
            by_merchant[t['merchant'] or '(unknown)'] += paise

    rupees = lambda d: {k: v / 100 for k, v in sorted(d.items(), key=lambda kv: -kv[1])}
    return {
        'month': month,
        'months': [{'month': m, 'spent': v['spent'] / 100, 'received': v['received'] / 100} for m, v in sorted(months.items())],
        'by_category': rupees(by_category),
        'top_merchants': dict(list(rupees(by_merchant).items())[:10]),
    }


class Handler(BaseHTTPRequestHandler):
    def authed(self):
        header = self.headers.get('Authorization', '')
        if header.startswith('Bearer '):
            given = header[7:]
        elif header.startswith('Basic '):  # browsers: any username, token as password
            try:
                given = base64.b64decode(header[6:]).decode().partition(':')[2]
            except (binascii.Error, UnicodeDecodeError):
                return False
        else:
            return False
        return hmac.compare_digest(given.encode(), TOKEN.encode())

    def send(self, status, payload, content_type='application/json'):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        if status == 401:
            self.send_header('WWW-Authenticate', 'Basic realm="expense"')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.authed():
            return self.send(401, {'error': 'unauthorized'})
        url = urlparse(self.path)
        month = parse_qs(url.query).get('month', [None])[0]
        with db() as conn:
            if url.path == '/':
                return self.send(200, (HERE / 'dashboard.html').read_bytes(), 'text/html; charset=utf-8')
            if url.path == '/api/summary':
                return self.send(200, summary(conn, month))
            if url.path == '/api/transactions':
                return self.send(200, transactions(conn, month))
        self.send(404, {'error': 'not found'})

    def do_POST(self):
        if not self.authed():
            return self.send(401, {'error': 'unauthorized'})
        if urlparse(self.path).path != '/sms':
            return self.send(404, {'error': 'not found'})
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= MAX_BODY:
                raise ValueError('body size')
            raw = self.rfile.read(length)
            # Form encoding is accepted because phone automation apps escape it reliably; SMS text with quotes breaks hand-built JSON.
            if self.headers.get('Content-Type', '').startswith('application/x-www-form-urlencoded'):
                msg = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
            else:
                msg = json.loads(raw)
            sender, body = str(msg['sender']).strip(), str(msg['body']).strip()
            received_at = msg.get('received_at') or datetime.now().astimezone().isoformat(timespec='seconds')
            datetime.fromisoformat(received_at)  # reject junk dates; they would break month grouping
        except (ValueError, KeyError, TypeError) as e:
            return self.send(400, {'error': f'bad request: {e}'})
        with db() as conn:
            stored = store_sms(conn, sender, body, received_at)
        self.send(201 if stored else 200, {'stored': bool(stored), 'transaction': stored})


def main():
    if len(TOKEN) < 16:
        raise SystemExit('Set EXPENSE_TOKEN to a secret of at least 16 characters.')
    with db() as conn:
        conn.execute(SCHEMA)
    host, port = os.environ.get('HOST', '0.0.0.0'), int(os.environ.get('PORT', '8000'))
    print(f'Listening on http://{host}:{port}')
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == '__main__':
    main()
