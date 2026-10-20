"""Expense server: receives SMS from the phone, stores transactions, serves analytics.

A plain WSGI app (`app`), so the same code runs on Vercel and locally:
    EXPENSE_TOKEN=some-long-secret python3 server.py                   # local, SQLite file
    DATABASE_URL=postgresql://... EXPENSE_TOKEN=... python3 server.py   # local, Postgres
"""
import hashlib
import hmac
import json
import os
import sqlite3
import ssl
from collections import defaultdict
import time
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

from parse import categorize, parse

HERE = Path(__file__).parent
RULES = HERE / 'rules.json'
MAX_BODY = 16 * 1024
MAX_IMPORT_BODY, MAX_IMPORT_BATCH = 2 * 1024 * 1024, 500  # one upload batch from the import page
SESSION_DAYS = 30
PAGES = {'/': 'dashboard.html', '/import': 'import.html'}
MIN_PASSWORD = 6
MAX_FAILURES, LOCKOUT_SECONDS = 10, 15 * 60  # wrong guesses per IP before it is locked out, and for how long

SCHEMA = '''CREATE TABLE IF NOT EXISTS transactions (
    id {id} PRIMARY KEY,
    received_at TEXT NOT NULL,
    sender TEXT NOT NULL,
    body TEXT NOT NULL UNIQUE,  -- shortcut: dedupes phone retries; two byte-identical SMS count once. Add a phone-side message id if that bites.
    amount_minor INTEGER NOT NULL,
    currency TEXT NOT NULL,
    direction TEXT NOT NULL,
    merchant TEXT NOT NULL
)'''
FAILURES_SCHEMA = 'CREATE TABLE IF NOT EXISTS login_failures (ip TEXT NOT NULL, at INTEGER NOT NULL)'
# Your corrections: a category for every transaction from a merchant, or for one transaction.
LEARNED_SCHEMAS = ['CREATE TABLE IF NOT EXISTS merchant_categories (merchant TEXT PRIMARY KEY, category TEXT NOT NULL)',
                   'CREATE TABLE IF NOT EXISTS txn_categories (txn_id BIGINT PRIMARY KEY, category TEXT NOT NULL)']
MAX_CATEGORY = 40


class DB:
    """Postgres when DATABASE_URL is set (Neon on Vercel), else a local SQLite file. Both take :name parameters."""
    _schema_ready = set()

    def __init__(self, url=None):
        url = url if url is not None else os.environ.get('DATABASE_URL', '')
        if url.startswith(('postgres://', 'postgresql://')):
            import pg8000.native
            u = urlparse(url)
            tls = parse_qs(u.query).get('sslmode', ['require'])[0] != 'disable'
            self.conn = pg8000.native.Connection(
                unquote(u.username or ''), host=u.hostname, port=u.port or 5432, database=u.path.lstrip('/'),
                password=unquote(u.password or ''), ssl_context=ssl.create_default_context() if tls else None)
            self.pg, schema = True, SCHEMA.format(id='BIGSERIAL')
        else:
            url = url or os.environ.get('EXPENSE_DB', str(HERE / 'expense.db'))
            self.conn = sqlite3.connect(url, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            self.pg, schema = False, SCHEMA.format(id='INTEGER')
        if url not in DB._schema_ready:  # once per process, not per request
            self.run(schema)
            for extra in [FAILURES_SCHEMA, *LEARNED_SCHEMAS]:
                self.run(extra)
            DB._schema_ready.add(url)

    def run(self, sql, **params):
        if self.pg:
            rows = self.conn.run(sql, **params) or []
            names = [c['name'] for c in self.conn.columns or []]
            return [dict(zip(names, r)) for r in rows]
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def close(self):
        self.conn.close()


def store_many(db, messages):
    """Parse (sender, body, received_at) messages and insert the new transactions in one statement
    (one round trip, so a big import stays fast). Returns the stored transactions."""
    rows = {}
    for sender, body, received_at in messages:
        txn = parse(body)
        if txn:
            rows.setdefault(body, {**txn, 'sender': sender, 'body': body, 'received_at': received_at})
    if not rows:
        return []
    columns = ('received_at', 'sender', 'body', 'amount_minor', 'currency', 'direction', 'merchant')
    values = ', '.join('(' + ', '.join(f':{c}{i}' for c in columns) + ')' for i in range(len(rows)))
    params = {f'{c}{i}': row[c] for i, row in enumerate(rows.values()) for c in columns}
    inserted = {r['body'] for r in db.run(
        f'INSERT INTO transactions ({", ".join(columns)}) VALUES {values} ON CONFLICT (body) DO NOTHING RETURNING body', **params)}
    return [{k: row[k] for k in ('amount_minor', 'currency', 'direction', 'merchant', 'received_at')}
            for row in rows.values() if row['body'] in inserted]


def store_sms(db, sender, body, received_at):
    """Parse and insert one SMS. Returns the stored transaction, or None if it was skipped or a duplicate."""
    stored = store_many(db, [(sender, body, received_at)])
    return stored[0] if stored else None


def transactions(db, month=None, currency=None):
    # Categories are computed on read, so a correction or a rules.json edit re-categorizes history too.
    # Order: your correction for this transaction > your rule for its merchant > rules.json keywords.
    rules = json.loads(RULES.read_text(encoding='utf-8'))
    learned = defaultdict(list)
    for r in db.run('SELECT merchant, category FROM merchant_categories'):
        learned[r['category']].append(r['merchant'])
    overrides = {r['txn_id']: r['category'] for r in db.run('SELECT txn_id, category FROM txn_categories')}
    rows = db.run('SELECT * FROM transactions ORDER BY received_at DESC')
    return [{
        'id': r['id'], 'received_at': r['received_at'], 'sender': r['sender'], 'body': r['body'],
        'amount': r['amount_minor'] / 100, 'currency': r['currency'], 'direction': r['direction'],
        'merchant': r['merchant'],
        'category': overrides.get(r['id']) or (r['merchant'] and categorize(r['merchant'], '', learned, None))
                    or categorize(r['merchant'], r['body'], rules, 'Income' if r['direction'] == 'credit' else 'Uncategorized'),
    } for r in rows
        if (not month or r['received_at'].startswith(month)) and (not currency or r['currency'] == currency)]


def set_category(db, txn_id, category, whole_merchant):
    """Save a correction. With whole_merchant, every past and future transaction from that merchant follows it."""
    found = db.run('SELECT merchant FROM transactions WHERE id = :id', id=txn_id)
    if not found:
        raise KeyError('no such transaction')
    merchant = found[0]['merchant'].strip().lower()
    if whole_merchant and merchant:
        db.run('INSERT INTO merchant_categories (merchant, category) VALUES (:m, :c) '
               'ON CONFLICT (merchant) DO UPDATE SET category = excluded.category', m=merchant, c=category)
        db.run('DELETE FROM txn_categories WHERE txn_id = :id', id=txn_id)  # so this one follows the merchant rule too
    else:
        db.run('INSERT INTO txn_categories (txn_id, category) VALUES (:id, :c) '
               'ON CONFLICT (txn_id) DO UPDATE SET category = excluded.category', id=txn_id, c=category)


def summary(db, month=None, currency=None):
    every = transactions(db)
    currencies = sorted({t['currency'] for t in every})
    # INR and SAR are never added together; SAR is the main account, so it is shown first.
    currency = currency or ('SAR' if 'SAR' in currencies or not every else every[0]['currency'])
    rows = [t for t in every if t['currency'] == currency]

    months = defaultdict(lambda: {'spent': 0, 'received': 0})
    for t in rows:
        months[t['received_at'][:7]]['spent' if t['direction'] == 'debit' else 'received'] += round(t['amount'] * 100)
    month = month or max(months, default=datetime.now(timezone.utc).strftime('%Y-%m'))

    by_category, by_merchant = defaultdict(int), defaultdict(int)
    for t in rows:
        if t['received_at'].startswith(month) and t['direction'] == 'debit':
            minor = round(t['amount'] * 100)
            by_category[t['category']] += minor
            by_merchant[t['merchant'] or '(unknown)'] += minor

    major = lambda d: {k: v / 100 for k, v in sorted(d.items(), key=lambda kv: -kv[1])}
    return {
        'currency': currency,
        'currencies': currencies,
        'month': month,
        'months': [{'month': m, 'spent': v['spent'] / 100, 'received': v['received'] / 100} for m, v in sorted(months.items())],
        'by_category': major(by_category),
        'top_merchants': dict(list(major(by_merchant).items())[:10]),
        # Choices for the category editor: the built-in ones plus any you invented.
        'categories': sorted({*json.loads(RULES.read_text(encoding='utf-8')), 'Income', *(t['category'] for t in every)} - {'Uncategorized'}),
    }


def _token():
    token = os.environ.get('EXPENSE_TOKEN', '').strip()  # a space pasted into Vercel's value box shouldn't lock you out
    return token if len(token) >= MIN_PASSWORD else None


def _sign(expires):
    return hmac.new(_token().encode(), f'session:{expires}'.encode(), hashlib.sha256).hexdigest()


def new_session():
    """Cookie value '<expiry>.<signature>': stateless, so it works across Vercel instances; changing EXPENSE_TOKEN logs everyone out."""
    expires = int(time.time()) + SESSION_DAYS * 86400
    return f'{expires}.{_sign(expires)}'


def client_ip(environ):
    # Vercel overwrites X-Forwarded-For with the real client address, so it can't be spoofed there.
    return (environ.get('HTTP_X_FORWARDED_FOR') or environ.get('REMOTE_ADDR') or '?').split(',')[0].strip()


def check_password(environ, given):
    """'ok', 'wrong' or 'locked'. A short password is guessable, so every wrong guess is recorded per IP
    (in the database, because Vercel instances share no memory) and MAX_FAILURES of them lock that IP out."""
    db = DB()
    try:
        ip, now = client_ip(environ), int(time.time())
        recent = db.run('SELECT COUNT(*) AS n FROM login_failures WHERE ip = :ip AND at > :since',
                        ip=ip, since=now - LOCKOUT_SECONDS)[0]['n']
        if recent >= MAX_FAILURES:
            return 'locked'
        if hmac.compare_digest(given.strip().encode(), _token().encode()):  # strip: phone keyboards add trailing spaces
            return 'ok'
        db.run('DELETE FROM login_failures WHERE at <= :since', since=now - LOCKOUT_SECONDS)
        db.run('INSERT INTO login_failures (ip, at) VALUES (:ip, :at)', ip=ip, at=now)
        return 'wrong'
    finally:
        db.close()


def bearer_ok(environ):
    """The phone authenticates with 'Authorization: Bearer <EXPENSE_TOKEN>'; guesses here count toward the lockout too."""
    header = environ.get('HTTP_AUTHORIZATION', '')
    return bool(_token()) and header.startswith('Bearer ') and check_password(environ, header[7:]) == 'ok'


def session_ok(environ):
    """The browser authenticates with the cookie set by the login page."""
    cookie = SimpleCookie(environ.get('HTTP_COOKIE', '')).get('session')
    expires, _, sig = (cookie.value if cookie else '').partition('.')
    return bool(_token()) and expires.isdigit() and int(expires) > time.time() and hmac.compare_digest(sig, _sign(expires))


def read_form(environ):
    length = int(environ.get('CONTENT_LENGTH') or 0)
    if not 0 < length <= MAX_BODY:
        return {}
    return {k: v[0] for k, v in parse_qs(environ['wsgi.input'].read(length).decode(errors='replace')).items()}


def read_sms(environ):
    """Validate the phone's POST. Accepts JSON or form fields: sender, body, received_at (optional ISO 8601 or epoch ms)."""
    length = int(environ.get('CONTENT_LENGTH') or 0)
    if not 0 < length <= MAX_BODY:
        raise ValueError('body size')
    raw = environ['wsgi.input'].read(length)
    if environ.get('CONTENT_TYPE', '').startswith('application/x-www-form-urlencoded'):
        msg = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
    else:
        msg = json.loads(raw)
    return clean_message(msg)


def clean_message(msg):
    """(sender, body, received_at) from one message dict; raises ValueError/KeyError/TypeError when it's malformed."""
    sender, body = str(msg['sender']).strip(), str(msg['body']).strip()
    received_at = str(msg.get('received_at') or '').strip()
    if received_at.isdigit():  # epoch milliseconds from the phone; shown in EXPENSE_TZ so months split at local midnight
        when = datetime.fromtimestamp(int(received_at) / 1000, ZoneInfo(os.environ.get('EXPENSE_TZ') or 'Asia/Riyadh'))
        received_at = when.isoformat(timespec='seconds')
    received_at = received_at or datetime.now(timezone.utc).isoformat(timespec='seconds')
    datetime.fromisoformat(received_at)  # reject junk dates; they would break month grouping
    return sender, body, received_at


def read_json(environ, limit):
    """JSON request body from the dashboard. JSON only, so a cross-site form can't submit it with the browser's cookie."""
    length = int(environ.get('CONTENT_LENGTH') or 0)
    if not 0 < length <= limit or not environ.get('CONTENT_TYPE', '').startswith('application/json'):
        raise ValueError(f'expected a JSON body up to {limit} bytes')
    return json.loads(environ['wsgi.input'].read(length))


def read_category(environ):
    """{"id": 12, "category": "Food", "whole_merchant": true} from the category editor."""
    msg = read_json(environ, MAX_BODY)
    category = ' '.join(str(msg['category']).split())
    if not 0 < len(category) <= MAX_CATEGORY or category == 'Uncategorized':
        raise ValueError(f'category must be 1-{MAX_CATEGORY} characters')
    return int(msg['id']), category, bool(msg.get('whole_merchant', True))


def read_import(environ):
    """A batch from the import page: JSON {"messages": [{sender, body, received_at}, ...]}."""
    messages = read_json(environ, MAX_IMPORT_BODY)['messages']
    if not isinstance(messages, list) or len(messages) > MAX_IMPORT_BATCH:
        raise ValueError(f'messages must be a list of at most {MAX_IMPORT_BATCH}')
    return [clean_message(m) for m in messages]


def app(environ, start_response):
    def respond(status, payload, content_type='application/json', headers=()):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        start_response(status, [('Content-Type', content_type), ('Content-Length', str(len(data))),
                                ('Cache-Control', 'no-store'), *headers])
        return [data]

    def redirect(location, cookie=None):
        headers = [('Location', location)]
        if cookie is not None:
            max_age = SESSION_DAYS * 86400 if cookie else 0
            headers.append(('Set-Cookie', f'session={cookie}; Max-Age={max_age}; Path=/; HttpOnly; Secure; SameSite=Lax'))
        return respond('303 See Other', b'', 'text/plain', headers)

    method, path = environ['REQUEST_METHOD'], environ.get('PATH_INFO') or '/'
    query = {k: v[0] for k, v in parse_qs(environ.get('QUERY_STRING', '')).items()}

    if path == '/login':
        if method == 'POST':
            if not _token():  # says only that setup is incomplete, nothing about the password itself
                return redirect('/login?error=setup')
            result = check_password(environ, read_form(environ).get('password', ''))
            if result == 'ok':
                return redirect('/', new_session())
            return redirect('/login?error=locked' if result == 'locked' else '/login?error=1')
        return respond('200 OK', (HERE / 'login.html').read_bytes(), 'text/html; charset=utf-8')
    if (method, path) == ('POST', '/logout'):
        return redirect('/login', '')

    if (method, path) == ('POST', '/sms'):
        if not bearer_ok(environ):  # phone only: the cookie isn't accepted here, so no cross-site form can add SMS
            return respond('401 Unauthorized', {'error': 'unauthorized'})
        try:
            sms = read_sms(environ)
        except (ValueError, KeyError, TypeError) as e:
            return respond('400 Bad Request', {'error': f'bad request: {e}'})
    elif not (session_ok(environ) or bearer_ok(environ)):
        return redirect('/login') if method == 'GET' and path in PAGES else respond('401 Unauthorized', {'error': 'unauthorized'})
    elif method == 'GET' and path in PAGES:
        return respond('200 OK', (HERE / PAGES[path]).read_bytes(), 'text/html; charset=utf-8')
    elif (method, path) == ('POST', '/api/import'):
        try:
            batch = read_import(environ)
        except (ValueError, KeyError, TypeError) as e:
            return respond('400 Bad Request', {'error': f'bad request: {e}'})
    elif (method, path) == ('POST', '/api/category'):
        try:
            correction = read_category(environ)
        except (ValueError, KeyError, TypeError) as e:
            return respond('400 Bad Request', {'error': f'bad request: {e}'})
    elif (method, path) not in {('GET', '/api/summary'), ('GET', '/api/transactions')}:
        return respond('404 Not Found', {'error': 'not found'})

    db = DB()
    try:
        if path == '/api/category':
            try:
                set_category(db, *correction)
            except KeyError:
                return respond('404 Not Found', {'error': 'no such transaction'})
            return respond('200 OK', {'saved': True})
        if path == '/api/import':
            stored = store_many(db, batch)
            return respond('200 OK', {'received': len(batch), 'stored': len(stored)})
        if method == 'POST':
            stored = store_sms(db, *sms)
            return respond('201 Created' if stored else '200 OK', {'stored': bool(stored), 'transaction': stored})
        if path == '/api/summary':
            return respond('200 OK', summary(db, query.get('month'), query.get('currency')))
        return respond('200 OK', transactions(db, query.get('month'), query.get('currency')))
    finally:
        db.close()


if __name__ == '__main__':
    from wsgiref.simple_server import make_server
    if not _token():
        raise SystemExit(f'Set EXPENSE_TOKEN to a password of at least {MIN_PASSWORD} characters.')
    host, port = os.environ.get('HOST', '127.0.0.1'), int(os.environ.get('PORT', '8000'))
    print(f'Listening on http://{host}:{port}')
    make_server(host, port, app).serve_forever()
