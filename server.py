"""Expense server: receives SMS from the phone, stores transactions, serves analytics.

A plain WSGI app (`app`), so the same code runs on Vercel and locally:
    EXPENSE_TOKEN=some-long-secret python3 server.py                   # local, SQLite file
    DATABASE_URL=postgresql://... EXPENSE_TOKEN=... python3 server.py   # local, Postgres
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import ssl
import sys
from collections import defaultdict
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import urlopen
from zoneinfo import ZoneInfo

import base64
from bisect import bisect_left
from datetime import date

from parse import AD, ARABIC_DIGITS, CUR_FIRST, NUM_FIRST, account_of, categorize, compile_rules, merchant_of, parse
from statement import StatementError, read_statement, span

HERE = Path(__file__).parent
RULES = HERE / 'rules.json'
MAX_BODY = 16 * 1024
VERSION = os.environ.get('VERCEL_GIT_COMMIT_SHA', 'local')[:7]  # which commit is live; Vercel sets this
MAX_IMPORT_BODY, MAX_IMPORT_BATCH = 2 * 1024 * 1024, 500  # one upload batch from the import page
MAX_STATEMENT_BODY = 4_400_000  # Vercel refuses request bodies over 4.5 MB; the file arrives base64 (a 3.2 MB file)
SESSION_DAYS = 365  # stay signed in for a year on a device; a password change or the admin ends it sooner
PAGES = {'/': 'dashboard.html', '/import': 'import.html'}
# What Android needs to install the site as an app. No personal data, and Chrome fetches the manifest without
# your login cookie, so these are served to anyone.
PUBLIC = {'/manifest.webmanifest': 'application/manifest+json', '/icon-192.png': 'image/png', '/icon-512.png': 'image/png'}
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
# A purchase made for a friend: their part of it (theirs_minor) isn't your spending, and a credit tagged with a
# friend is them paying you back, not income. settled: paid back outside the bank, or squared up by hand.
FRIENDS_SCHEMA = 'CREATE TABLE IF NOT EXISTS txn_friends (txn_id BIGINT PRIMARY KEY, friend TEXT NOT NULL, theirs_minor BIGINT NOT NULL, settled INTEGER NOT NULL DEFAULT 0)'
MAX_FRIEND = 40
# Your loans and savings, entered by hand. A loan's balance is as of `as_of`; EMI SMS after that whose text
# contains `pattern` (e.g. the loan account's last digits) bring it down. rate_bp: yearly rate in hundredths of a %.
HOLDINGS_SCHEMA = '''CREATE TABLE IF NOT EXISTS holdings (id {id} PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
  currency TEXT NOT NULL, balance_minor BIGINT NOT NULL, rate_bp INTEGER NOT NULL DEFAULT 0, emi_minor BIGINT NOT NULL DEFAULT 0,
  due_day INTEGER NOT NULL DEFAULT 0, pattern TEXT NOT NULL DEFAULT '', as_of TEXT NOT NULL, auto INTEGER NOT NULL DEFAULT 0)'''
# Payments you record yourself on the Plan page: an EMI the SMS didn't show, or an extra (pre)payment.
PAYMENTS_SCHEMA = '''CREATE TABLE IF NOT EXISTS loan_payments (id {id} PRIMARY KEY, holding_id BIGINT NOT NULL, paid_on TEXT NOT NULL,
  amount_minor BIGINT NOT NULL, note TEXT NOT NULL DEFAULT '')'''
# The monthly check-in: what you planned (an extra payment, a spending limit) and whether you did it.
ACTIONS_SCHEMA = '''CREATE TABLE IF NOT EXISTS plan_actions (month TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL,
  target_minor BIGINT NOT NULL DEFAULT 0, actual_minor BIGINT NOT NULL DEFAULT 0, status TEXT NOT NULL, at TEXT NOT NULL,
  PRIMARY KEY (month, kind, ref))'''
PLAN_KINDS = {'balances', 'emi', 'extra', 'limit', 'savings', 'alloc'}
# Your answers to the Plan tab's setup: the salary, how you split it, and the debt option you chose.
SETTINGS_SCHEMA = 'CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)'
SETTING_KEYS = {'salary', 'allocations', 'debt_choice'}
# What you owed and had saved, once a month (INR, the latest look at the Debt tab that month), for the graphs.
SNAPSHOTS_SCHEMA = 'CREATE TABLE IF NOT EXISTS snapshots (month TEXT PRIMARY KEY, debt_minor BIGINT NOT NULL, savings_minor BIGINT NOT NULL)'
PLAN_STATUS = {'planned', 'done', 'skipped'}
PLAN_MONTHS = 600  # 50 years: a loan whose EMI doesn't cover its interest never ends
# SMS of transactions you deleted, so the phone sending them again (or an import) doesn't bring them back.
DELETED_SCHEMA = 'CREATE TABLE IF NOT EXISTS deleted_sms (body TEXT PRIMARY KEY)'
# Your bank accounts, cards, wallets (STC Pay, PhonePe…), buy-now-pay-later (Tamara, Tabby) and loan accounts, named when
# you import a statement. digits: the account's or card's last 4, so its SMS ('A/c XX1234') show under the same name.
ACCOUNTS_SCHEMA = "CREATE TABLE IF NOT EXISTS accounts (name TEXT PRIMARY KEY, kind TEXT NOT NULL, currency TEXT NOT NULL, digits TEXT NOT NULL DEFAULT '')"
ACCOUNT_KINDS = {'bank', 'card', 'debit', 'wallet', 'bnpl', 'loan'}  # card: a credit card; debit: a debit (mada/ATM) card
# People using this app. Each has their own data space: a Postgres schema ('u_12') with every table above, so no query
# can see another person's data; 'public' holds the data from before accounts existed (the owner's). Only the admin
# adds people. sync_key: what their phone app sends instead of a password. pw_hash: PBKDF2, never the password.
USERS_SCHEMA = '''CREATE TABLE IF NOT EXISTS app_users (id {id} PRIMARY KEY, email TEXT NOT NULL UNIQUE, pw_hash TEXT NOT NULL,
  space TEXT NOT NULL DEFAULT '', sync_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, last_seen TEXT NOT NULL DEFAULT '')'''
# A split with someone who also uses this app (by their email): what they owe whom, kept in the public space so both
# sides see it. One row per tagged transaction; amount_minor < 0 is them paying back. Rewritten from the tagging
# person's own data each time it changes (share()), so it never drifts from it.
SHARED_SCHEMA = '''CREATE TABLE IF NOT EXISTS shared_splits (from_space TEXT NOT NULL, txn_id BIGINT NOT NULL, from_email TEXT NOT NULL,
  to_email TEXT NOT NULL, amount_minor BIGINT NOT NULL, currency TEXT NOT NULL, what TEXT NOT NULL, day TEXT NOT NULL,
  settled INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (from_space, txn_id))'''
# Your friends' emails, given once: name -> email (empty: not known).
CONTACTS_SCHEMA = "CREATE TABLE IF NOT EXISTS contacts (name TEXT PRIMARY KEY, email TEXT NOT NULL DEFAULT '')"
# Money between two people on the app (e.g. a husband's transfer arriving in his wife's account): each side keeps the
# other side's details. blocked = 1: you unlinked it, so it's never linked again.
PEER_SCHEMA = '''CREATE TABLE IF NOT EXISTS peer_links (txn_id BIGINT PRIMARY KEY, who TEXT NOT NULL, their_space TEXT NOT NULL,
  their_txn BIGINT NOT NULL, their_account TEXT NOT NULL DEFAULT '', their_day TEXT NOT NULL DEFAULT '', blocked INTEGER NOT NULL DEFAULT 0)'''
SPACE = re.compile(r'(?:public|u_\d{1,9})\Z')
EMAIL = re.compile(r'[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}\Z')
MIN_USER_PASSWORD = 8
USERNAME = re.compile(r'[a-z0-9][a-z0-9._-]{2,29}\Z')  # 3-30 letters, digits . _ -; stored in lower case


def set_username(db, space, name):
    """db: the public space. Unique, case-insensitive, and never an email (so sign-in can tell them apart)."""
    name = name.strip().lower()
    if not USERNAME.match(name):
        raise ValueError('3-30 letters, digits, dots, dashes or underscores, starting with a letter or digit')
    if db.run('SELECT 1 FROM app_users WHERE username = :u AND space != :s', u=name, s=space):
        raise ValueError('that username is taken')
    db.run('UPDATE app_users SET username = :u WHERE space = :s', u=name, s=space)
    return name


def change_password(db, space, current, new):
    """db: the public space. Returns the new hash (the session is re-signed with it)."""
    found = db.run('SELECT pw_hash FROM app_users WHERE space = :s', s=space)
    if not found or not password_matches(current, found[0]['pw_hash']):
        raise ValueError('the current password is wrong')
    if len(new) < MIN_USER_PASSWORD:
        raise ValueError(f'the new password needs at least {MIN_USER_PASSWORD} characters')
    h = hash_password(new)
    db.run('UPDATE app_users SET pw_hash = :h WHERE space = :s', h=h, s=space)
    return h
# Transfers you linked by hand on the Money trail: the debit in one of your accounts and the credit it became.
LINKS_SCHEMA = 'CREATE TABLE IF NOT EXISTS txn_links (debit_id BIGINT PRIMARY KEY, credit_id BIGINT NOT NULL UNIQUE)'
# One row per imported statement file; its transactions carry statement_id, so the import can be undone.
STATEMENTS_SCHEMA = '''CREATE TABLE IF NOT EXISTS statements (id {id} PRIMARY KEY, account TEXT NOT NULL, file_name TEXT NOT NULL,
  imported_at TEXT NOT NULL, first_day TEXT, last_day TEXT, read_rows INTEGER NOT NULL, new_rows INTEGER NOT NULL,
  sms_rows INTEGER NOT NULL, checked INTEGER NOT NULL)'''
# A receipt per SMS the phone sends (time, sender, what happened; never the text), so you can check delivery.
DELIVERIES_SCHEMA = 'CREATE TABLE IF NOT EXISTS phone_deliveries (id {id} PRIMARY KEY, at TEXT NOT NULL, sender TEXT NOT NULL, outcome TEXT NOT NULL)'
DELIVERY_DAYS = 7
# The text of SMS that were skipped as 'not a transaction', kept as long as their receipt, so you can save one
# as a transaction from Settings. Codes and passwords are never kept.
SKIPPED_SCHEMA = 'CREATE TABLE IF NOT EXISTS skipped_sms (delivery_id BIGINT PRIMARY KEY, body TEXT NOT NULL, received_at TEXT NOT NULL)'
SECRET = re.compile(r'\b(?:otp|one[- ]time|verification|passcode|password|pin)\b|رمز|كلمة المرور', re.I)
IGNORED = 'Ignored'  # a category like any other, except it never counts toward totals
OWN_TRANSFER = 'Own transfer'  # money moved between your own accounts: not spending, not income
NOT_COUNTED = {IGNORED, OWN_TRANSFER}
# Advert senders: Saudi rules put "AD" on promotional sender names ("Mobily-AD"); Indian promotional ones end in "-P".
# A phone can forward every message it gets (iPhone: "Message Contains" a space). Chats and anything without an amount in
# a currency are dropped unread and unrecorded; a sender that is a full phone number is a person, not a bank (banks use names or short codes).
PERSON_SENDER = re.compile(r'^\+?[\d\s()-]{8,}$')


def not_money(sender, body):
    text = body.translate(ARABIC_DIGITS)
    return bool(PERSON_SENDER.match(sender.strip())) or not (CUR_FIRST.search(text) or NUM_FIRST.search(text))


AD_SENDER = re.compile(r'(?:^ad[-_ ]|[-_ ]ad$|[-_ ]adv$|-p$)', re.I)
# A refund from a merchant with no category is money back on spending, not income: it waits for a category.
REFUND = re.compile(r'\b(?:refund(?:ed)?|reversal|reversed|reverse|cashback)\b|استرداد|مسترد', re.I)
# Words that mark a bank-to-bank transfer (English and Arabic), used to pair the two sides of an own transfer.
TRANSFER = re.compile(r'\b(?:transfer(?:red)?|trf|xfer|neft|imps|rtgs|upi|iban|sarie)\b|حوالة|تحويل', re.I)
# shortcut: pairs only same-currency, same-amount SMS within an hour; a slower or cross-currency transfer
# needs the "Own transfer" option once (with "apply to all", that also covers the future ones).
PAIR_WINDOW_SECONDS = 60 * 60
# A slower bank SMS (the 'credited' one arriving hours later) still pairs within a day, when nothing else of that
# amount and currency is near either side: then there's no other money it could be.
SLOW_PAIR_SECONDS = 24 * 60 * 60


def direct(url):
    """Neon's direct address instead of its connection pooler ('ep-x-pooler.…' → 'ep-x.…'). pg8000 sends a query in two
    steps, and a pooler in transaction mode can hand the second step to another server ('unnamed prepared statement
    does not exist'). Neon on Vercel also sets DATABASE_URL_UNPOOLED, which is preferred when present."""
    u = urlparse(url)
    if u.hostname and '-pooler.' in u.hostname:
        return url.replace(u.hostname, u.hostname.replace('-pooler.', '.'), 1)
    return url


class DB:
    """Postgres when DATABASE_URL is set (Neon on Vercel), else a local SQLite file. Both take :name parameters."""
    _schema_ready = set()
    # Open Postgres connections kept between requests: a new TLS + login handshake costs several round trips,
    # and Vercel reuses warm instances. A pool (not one shared connection) because requests can run concurrently.
    _idle = {}
    POOL_SIZE = 3

    def __init__(self, url=None, space='public'):
        if not SPACE.match(space):
            raise ValueError('bad data space')
        url = direct(url if url is not None else os.environ.get('DATABASE_URL_UNPOOLED') or os.environ.get('DATABASE_URL', ''))
        self.url, self.reused, self.space = url, False, space
        if url.startswith(('postgres://', 'postgresql://')):
            try:
                self.conn, self.reused = DB._idle.setdefault(url, []).pop(), True
            except IndexError:
                self.conn = self._connect(url)
            self.pg, schema = True, SCHEMA.format(id='BIGSERIAL')
            self._enter()
        else:
            url = self.main_file = url or os.environ.get('EXPENSE_DB', str(HERE / 'expense.db'))
            if space != 'public':  # locally, each person's data is a file next to the main one
                url = str(Path(url).with_name(f'{Path(url).stem}-{space}{Path(url).suffix or ".db"}'))
            self.conn = sqlite3.connect(url, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            self.pg, schema = False, SCHEMA.format(id='INTEGER')
        ready = url if space == 'public' else (url, space)
        if ready not in DB._schema_ready:  # once per process, not per request
            if self.pg and space != 'public':
                self.run(f'CREATE SCHEMA IF NOT EXISTS {space}')
            self.run(schema)
            for extra in [USERS_SCHEMA, SHARED_SCHEMA] if space == 'public' else []:
                self.run(extra.format(id='BIGSERIAL' if self.pg else 'INTEGER'))
            for extra in [FAILURES_SCHEMA, *LEARNED_SCHEMAS, DELIVERIES_SCHEMA, SKIPPED_SCHEMA, FRIENDS_SCHEMA, DELETED_SCHEMA, HOLDINGS_SCHEMA, PAYMENTS_SCHEMA, ACTIONS_SCHEMA, SETTINGS_SCHEMA, SNAPSHOTS_SCHEMA, ACCOUNTS_SCHEMA, STATEMENTS_SCHEMA, LINKS_SCHEMA, CONTACTS_SCHEMA, PEER_SCHEMA]:
                self.run(extra.format(id='BIGSERIAL' if self.pg else 'INTEGER'))
            self.migrate()
            DB._schema_ready.add(ready)  # before the re-read: if that fails, requests must still work and not retry it each time
            try:
                self.reread_amounts()
            except Exception as e:  # a clean-up; never worth failing a request over
                print(f'reread_amounts failed: {type(e).__name__}: {e}', file=sys.stderr)

    def _enter(self):
        """Point this connection at this person's data space (pooled connections keep the last one's)."""
        if getattr(self.conn, 'expense_space', None) != self.space:
            self.conn.run(f'SET search_path TO {self.space}')
            self.conn.expense_space = self.space

    def migrate(self):
        """Columns added after a table first existed. holdings.auto: the EMI is taken automatically each month
        (loans made before it existed are, as Indian EMIs usually are; cards aren't)."""
        cols = ({r['column_name'] for r in self.run("SELECT column_name FROM information_schema.columns WHERE table_name = 'holdings' AND table_schema = current_schema()")}
                if self.pg else {r['name'] for r in self.run('PRAGMA table_info(holdings)')})
        if 'auto' not in cols:
            self.run('ALTER TABLE holdings ADD COLUMN auto INTEGER NOT NULL DEFAULT 0')
            self.run("UPDATE holdings SET auto = 1 WHERE kind = 'loan'")
        acols = table_columns(self, 'accounts')
        for col, ddl in (('label', "label TEXT NOT NULL DEFAULT ''"), ('hidden', 'hidden INTEGER NOT NULL DEFAULT 0'),
                         ('balance_minor', 'balance_minor BIGINT NOT NULL DEFAULT 0'), ('balance_at', "balance_at TEXT NOT NULL DEFAULT ''")):
            if col not in acols:  # label: the name the SMS gave it (now shown as `name`); hidden: removed by you
                self.run(f'ALTER TABLE accounts ADD COLUMN {ddl}')
        if 'limit_minor' not in cols:  # a card's credit limit, for the 70%-used warning
            self.run('ALTER TABLE holdings ADD COLUMN limit_minor BIGINT NOT NULL DEFAULT 0')
        if self.space == 'public':  # app_users.username: a unique name to sign in with instead of the email
            ucols = ({r['column_name'] for r in self.run("SELECT column_name FROM information_schema.columns WHERE table_name = 'app_users' AND table_schema = current_schema()")}
                     if self.pg else {r['name'] for r in self.run('PRAGMA table_info(app_users)')})
            if ucols and 'username' not in ucols:
                self.run("ALTER TABLE app_users ADD COLUMN username TEXT")
            self.run('CREATE UNIQUE INDEX IF NOT EXISTS app_users_username ON app_users (username)')
        # transactions.statement_id: the statement a row came from (none for SMS)
        cols = ({r['column_name'] for r in self.run("SELECT column_name FROM information_schema.columns WHERE table_name = 'transactions' AND table_schema = current_schema()")}
                if self.pg else {r['name'] for r in self.run('PRAGMA table_info(transactions)')})
        if 'statement_id' not in cols:
            self.run('ALTER TABLE transactions ADD COLUMN statement_id BIGINT')

    @staticmethod
    def _connect(url):
        import pg8000.native
        u = urlparse(url)
        tls = parse_qs(u.query).get('sslmode', ['require'])[0] != 'disable'
        return pg8000.native.Connection(
            unquote(u.username or ''), host=u.hostname, port=u.port or 5432, database=u.path.lstrip('/'),
            password=unquote(u.password or ''), ssl_context=ssl.create_default_context() if tls else None)

    def reread_amounts(self):
        """Once per server start: read amount, currency and direction again from every stored SMS and fix the ones
        the current parser reads differently, so a parser fix also corrects messages stored before it
        (e.g. a date on the line after 'Balance: … SAR' once read as the amount). Adverts stored before adverts
        were recognized are marked Ignored, unless you set their category yourself. Batched, a few hundred rows per
        statement: one round trip per row could outlast the request on a slow link."""
        has_category = {r['txn_id'] for r in self.run('SELECT txn_id FROM txn_categories')}
        adverts, fixes = [], []
        for r in self.run('SELECT id, sender, body, amount_minor, currency, direction FROM transactions WHERE statement_id IS NULL'):
            if AD_SENDER.search(r['sender'].strip()) or AD.search(r['body']):
                if r['id'] not in has_category:
                    adverts.append(r['id'])
                continue
            again = parse(r['body'])
            if again is None and r['id'] not in has_category and r['sender'] != 'Assistant':
                adverts.append(r['id'])  # no longer read as a transaction (e.g. EPFO passbook SMS): Ignored, unless you chose
                continue
            if again and (again['amount_minor'], again['currency'], again['direction']) != (r['amount_minor'], r['currency'], r['direction']):
                fixes.append((r['id'], again['amount_minor'], again['currency'], again['direction']))
        for i in range(0, len(adverts), 300):
            chunk = adverts[i:i + 300]
            self.run('INSERT INTO txn_categories (txn_id, category) VALUES ' + ', '.join(f'(:id{n}, :c)' for n in range(len(chunk)))
                     + ' ON CONFLICT (txn_id) DO NOTHING', c=IGNORED, **{f'id{n}': v for n, v in enumerate(chunk)})
        for i in range(0, len(fixes), 300):  # only after a parser fix, and only the rows it changed
            chunk = fixes[i:i + 300]
            # VALUES columns are column1..column4 in both Postgres and SQLite; casts give Postgres the types.
            self.run('UPDATE transactions SET amount_minor = v.column2, currency = v.column3, direction = v.column4 FROM (VALUES '
                     + ', '.join(f'(CAST(:i{n} AS BIGINT), CAST(:a{n} AS BIGINT), :c{n}, :d{n})' for n in range(len(chunk)))
                     + ') AS v WHERE transactions.id = v.column1',
                     **{k: v for n, f in enumerate(chunk) for k, v in ((f'i{n}', f[0]), (f'a{n}', f[1]), (f'c{n}', f[2]), (f'd{n}', f[3]))})

    def run(self, sql, **params):
        if self.pg:
            from pg8000.exceptions import DatabaseError, InterfaceError
            try:
                rows = self.conn.run(sql, **params) or []
            except (InterfaceError, OSError, DatabaseError) as e:
                # 26000 'unnamed prepared statement does not exist': a connection pooler sent the two halves of the
                # query to different servers. Reconnect and run it again, like a stale connection.
                pooled = isinstance(e, DatabaseError) and isinstance(e.args[0], dict) and e.args[0].get('C') == '26000'
                if isinstance(e, DatabaseError) and not pooled or not (self.reused or pooled):
                    raise
                # A kept connection went stale (Neon drops them when it sleeps): reconnect once and retry.
                # Every statement here is safe to repeat (inserts are ON CONFLICT DO NOTHING / upserts).
                self.conn, self.reused = self._connect(self.url), False
                self._enter()  # a new connection starts in 'public': back to this person's space first
                rows = self.conn.run(sql, **params) or []
            names = [c['name'] for c in self.conn.columns or []]
            return [dict(zip(names, r)) for r in rows]
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def close(self):
        idle = DB._idle.get(self.url)
        if self.pg and len(idle) < DB.POOL_SIZE:
            idle.append(self.conn)
        else:
            self.conn.close()


def store_many(db, messages):
    """Parse (sender, body, received_at) messages and insert the new transactions in one statement
    (one round trip, so a big import stays fast). Returns the stored transactions."""
    rows = {}
    deleted = {r['body'] for r in db.run('SELECT body FROM deleted_sms')} if messages else set()
    for sender, body, received_at in messages:
        if body in deleted:
            continue
        txn = None if AD_SENDER.search(sender.strip()) else parse(body)
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


def record_delivery(db, sender, outcome, skipped=None):
    """skipped: (body, received_at) of an SMS that wasn't read as a transaction, kept so you can save it yourself."""
    now = datetime.now(timezone.utc)
    receipt = db.run('INSERT INTO phone_deliveries (at, sender, outcome) VALUES (:at, :s, :o) RETURNING id',
                     at=now.isoformat(timespec='seconds'), s=sender[:40], o=outcome)[0]['id']
    if skipped and not SECRET.search(skipped[0]):
        db.run('INSERT INTO skipped_sms (delivery_id, body, received_at) VALUES (:id, :b, :r)', id=receipt, b=skipped[0], r=skipped[1])
    # Keep a week, and at most the newest 100, so a stream of refused requests can't fill the table.
    db.run('DELETE FROM phone_deliveries WHERE at < :old OR id <= (SELECT id FROM phone_deliveries ORDER BY id DESC LIMIT 1 OFFSET 100)',
           old=(now - timedelta(days=DELIVERY_DAYS)).isoformat(timespec='seconds'))
    db.run('DELETE FROM skipped_sms WHERE delivery_id NOT IN (SELECT id FROM phone_deliveries)')


def save_skipped(db, receipt_id, amount_minor, direction, currency):
    """Store a skipped SMS as a transaction with the amount and direction you gave; its receipt then says so."""
    found = db.run('SELECT s.body, s.received_at, d.sender FROM skipped_sms s JOIN phone_deliveries d ON d.id = s.delivery_id '
                   'WHERE s.delivery_id = :id', id=receipt_id)
    if not found:
        raise KeyError('no such message')
    sms = found[0]
    db.run('INSERT INTO transactions (received_at, sender, body, amount_minor, currency, direction, merchant) '
           'VALUES (:r, :s, :b, :a, :c, :d, :m) ON CONFLICT (body) DO NOTHING',
           r=sms['received_at'], s=sms['sender'], b=sms['body'], a=amount_minor, c=currency, d=direction, m=merchant_of(sms['body']))
    db.run('INSERT INTO txn_categories (txn_id, category) SELECT id, :c FROM transactions WHERE body = :b ON CONFLICT (txn_id) DO NOTHING',
           c='Uncategorized', b=sms['body'])  # you chose to keep it: the start-up advert sweep must leave it alone
    db.run("UPDATE phone_deliveries SET outcome = 'saved by you' WHERE id = :id", id=receipt_id)
    db.run('DELETE FROM skipped_sms WHERE delivery_id = :id', id=receipt_id)


def store_sms(db, sender, body, received_at):
    """Parse and insert one SMS. Returns the stored transaction, or None if it was skipped or a duplicate."""
    stored = store_many(db, [(sender, body, received_at)])
    return stored[0] if stored else None


def transactions(db, month=None, currency=None):
    # Categories are computed on read, so a correction or a rules.json edit re-categorizes history too.
    # Order: your correction for this transaction > your rule for its merchant > rules.json keywords.
    rules = compile_rules(json.loads(RULES.read_text(encoding='utf-8')))
    saved = {r['merchant']: r['category'] for r in db.run('SELECT merchant, category FROM merchant_categories')}
    by_wording = {k: c for k, c in saved.items() if k.startswith(WORDING)}
    for_merchant = rule_finder({k: c for k, c in saved.items() if not k.startswith(WORDING)})
    overrides = {r['txn_id']: r['category'] for r in db.run('SELECT txn_id, category FROM txn_categories')}
    friends = {r['txn_id']: r for r in db.run('SELECT txn_id, friend, theirs_minor, settled FROM txn_friends')}
    peers = {r['txn_id']: r for r in db.run('SELECT * FROM peer_links WHERE blocked = 0')}
    # The salary you entered on the Plan tab: a credit within 10% of it, in its currency, is your salary even when the
    # bank's SMS doesn't say 'salary'.
    pay = db.run("SELECT value FROM settings WHERE key = 'salary'")
    pay = json.loads(pay[0]['value']) if pay else {}
    pay_minor, pay_cur = round(float(pay.get('amount') or 0) * 100), pay.get('currency') or 'SAR'
    is_pay = lambda r: pay_minor and r['direction'] == 'credit' and r['currency'] == pay_cur and abs(r['amount_minor'] - pay_minor) <= pay_minor / 10
    # id breaks ties: rows with the same timestamp must come back in the same order every time,
    # or the dashboard sees "changed" data on each load and redraws under your finger.
    rows = db.run('SELECT * FROM transactions ORDER BY received_at DESC, id DESC')
    own = own_transfers(rows, overrides)
    named = {a['digits']: a['name'] for a in db.run("SELECT name, digits FROM accounts WHERE digits != ''")}
    mine = lambda d: next((n for k, n in named.items() if d and k.endswith(d)), None)  # '234' shown, '1234' given
    acct = {r['id']: r['sender'] if r['statement_id'] else mine(account_of(r['body']).partition('•')[2]) or account_label(r['sender'], r['body'])
            for r in rows}
    # One bank shows '•234' in one SMS and '•1234' in the next: the same account, under its longer ending.
    ends = defaultdict(set)
    for a in set(acct.values()):
        head, dot, d = a.rpartition('•')
        if dot:
            ends[head].add(d)
    longer = lambda a: next(iter([f'{a.rpartition("•")[0]}•{x}' for x in ends[a.rpartition('•')[0]]
                                  if len(x) > len(a.rpartition('•')[2]) and x.endswith(a.rpartition('•')[2])][:1]), a)
    acct = {k: longer(v) if '•' in v else v for k, v in acct.items()}
    # An SMS that names no account ('SAB' alone) is from that bank's account when the bank has just one ('SAB ·
    # Account •5555'); its cards are separate.
    only = {head.split(' · ')[0]: [f'{head}•{d}' for d in ds] for head, ds in ends.items() if head.endswith('Account ')}
    acct = {r['id']: only[acct[r['id']]][0] if not r['statement_id'] and len(only.get(acct[r['id']], ())) == 1 else acct[r['id']]
            for r in rows}
    mine = db.run('SELECT name, label, hidden FROM accounts')
    renamed = {a['label']: a['name'] for a in mine if a['label']}
    acct = {k: renamed.get(v, v) for k, v in acct.items()}
    removed = {a['name'] for a in mine if a['hidden']}
    linked = links(rows, acct, overrides, [(r['debit_id'], r['credit_id']) for r in db.run('SELECT * FROM txn_links')])
    own |= set(linked)
    result = []
    for r in rows:
        if (month and not r['received_at'].startswith(month)) or (currency and r['currency'] != currency):
            continue
        merchant = r['merchant'] if r['statement_id'] else merchant_of(r['body'])
        f = friends.get(r['id'])
        # A credit from a friend is all theirs (a repayment); a debit is theirs up to its amount.
        theirs = 0 if not f else r['amount_minor'] if r['direction'] == 'credit' else min(f['theirs_minor'], r['amount_minor'])
        result.append({
            'id': r['id'], 'received_at': r['received_at'], 'sender': r['sender'], 'body': r['body'],
            'amount': r['amount_minor'] / 100, 'currency': r['currency'], 'direction': r['direction'],
            'merchant': merchant,
            'account': acct[r['id']], 'statement': bool(r['statement_id']), 'linked': linked.get(r['id']), 'fixed': r['id'] in overrides,
            'friend': f['friend'] if f else '', 'theirs': theirs / 100, 'settled': bool(f and f['settled']),
            'peer': {'who': peers[r['id']]['who'], 'account': peers[r['id']]['their_account'], 'day': peers[r['id']]['their_day']} if r['id'] in peers else None,
            'category': overrides.get(r['id']) or (IGNORED if acct[r['id']] in removed else None) or (OWN_TRANSFER if r['id'] in own else None)
                        or (merchant and for_merchant(merchant))
                        or (not merchant and by_wording.get(wording(r['body'])))
                        or (r['id'] in peers and 'Family')
                        or (r['id'] not in own and is_pay(r) and not REFUND.search(r['body']) and 'Salary')
                        or categorize(merchant, r['body'], rules, 'Income' if r['direction'] == 'credit' and not REFUND.search(r['body']) else 'Uncategorized'),
        })
    return result


def account_label(sender, body):
    """'AlRajhiBank · Card •1234': which bank and card/account a transaction used. Indian sender IDs carry an
    operator prefix/suffix ('VM-HDFCBK', 'JD-HDFCBK-S') that changes per SMS, so it's dropped."""
    bank = re.sub(r'^[A-Za-z]{2}-|-[A-Za-z]$', '', sender.strip())
    return ' · '.join(part for part in (bank, account_of(body)) if part) or 'Unknown account'


def rule_finder(rules):
    """Look up your rule for a merchant: its exact name, else the longest saved name found inside it, so a rule
    for 'JARIR MEDICAL CO' beats one for 'JARIR' whatever order they were saved in. Memoized per merchant."""
    found = {}

    def find(merchant):
        m = merchant.strip().lower()
        if m not in found:
            inside = [k for k in rules if re.search(r'\b' + re.escape(k) + r'\b', m)]
            found[m] = rules.get(m) or (rules[max(inside, key=len)] if inside else None)
        return found[m]
    return find


def own_transfers(rows, overrides):
    """Ids of transfers between your own accounts: a transfer out and a transfer in of the same amount and
    currency within PAIR_WINDOW_SECONDS (each account's bank sends its own SMS). A transaction you
    categorized yourself is never paired, so correcting one side breaks a wrong pair."""
    credits = defaultdict(list)  # (currency, amount) -> [(time, id)], so each debit only looks at equal amounts
    for r in rows:
        if r['direction'] == 'credit' and r['id'] not in overrides and TRANSFER.search(r['body']):
            credits[r['currency'], r['amount_minor']].append((datetime.fromisoformat(r['received_at']), r['id']))
    paired = set()
    for r in rows:
        if r['direction'] != 'debit' or r['id'] in overrides or not TRANSFER.search(r['body']):
            continue
        sent_at = datetime.fromisoformat(r['received_at'])
        gaps = [(abs((got_at - sent_at).total_seconds()), cid) for got_at, cid in credits[r['currency'], r['amount_minor']]
                if cid not in paired]
        gap, cid = min(gaps, default=(None, None))
        if cid is not None and gap <= PAIR_WINDOW_SECONDS:
            paired |= {r['id'], cid}
    return paired


# Words that make a debit and a credit in two of your accounts days apart (or in two currencies) one movement of money:
# transfers, remittances home, wallet top-ups, card bills and loan repayments.
# Generic words only (no bank or app names), so it works the same whatever banks you use.
LINK_WORDS = re.compile(r'\b(?:remit\w*|inward|outward|international|swift|foreign|nre|nro|'
                        r'top ?-?up|card ?payment|cc ?payment|payment (?:received|thank)|'
                        r'repayment|emi|loan|by transfer|to transfer)\b|حوالة|تحويل|سداد', re.I)
# Credit/debit amount ratio for money changing currency on the way (₹ per SAR, ₹ per $, $ per SAR), with room for fees and
# the rates of the last few years.
# 'A/c XX1234 debited by Rs 15,000; ANIL K credited. RRN …': money sent to another account (a person's, or yours).
# A purchase or a card spend pays a shop: it never moves money to one of your accounts.
PURCHASE = re.compile(r'\b(?:purchase|pos|spent|shopping)\b|شراء', re.I)
P2P = re.compile(r'\bdebited\b[^\n]*\bcredited\b|\brrn\b', re.I)
FX_RATIO = {('SAR', 'INR'): (20.0, 27.0), ('USD', 'INR'): (78.0, 100.0), ('SAR', 'USD'): (0.255, 0.27)}
LINK_DAYS = 4


def transfer_like(r):
    return bool(LINK_WORDS.search(r['body']) or TRANSFER.search(r['body']) or P2P.search(r['body']))


def link_by_hand(db, debit_id, credit_id):
    """You say this debit became that credit (or, credit_id None, that it isn't linked by hand any more). Your own
    category on either is dropped, so both count as an own transfer."""
    db.run('DELETE FROM txn_links WHERE debit_id = :d OR credit_id = :d', d=debit_id)
    if credit_id is None:
        return
    rows = {r['id']: r for r in db.run('SELECT id, direction FROM transactions WHERE id IN (:d, :c)', d=debit_id, c=credit_id)}
    if rows.get(debit_id, {}).get('direction') != 'debit' or rows.get(credit_id, {}).get('direction') != 'credit':
        raise KeyError('pick money going out, and money coming in')
    db.run('DELETE FROM txn_links WHERE debit_id = :c OR credit_id = :c', c=credit_id)
    db.run('INSERT INTO txn_links (debit_id, credit_id) VALUES (:d, :c)', d=debit_id, c=credit_id)
    db.run('DELETE FROM txn_categories WHERE txn_id IN (:d, :c)', d=debit_id, c=credit_id)


def links(rows, acct, overrides, manual=()):
    """{debit id: credit id, credit id: debit id} for money that left one of your accounts and arrived in another:
    the same amount (or the same at an exchange rate) within LINK_DAYS, with transfer words on either side.
    Two SMS in the same currency must be an hour apart at most (as in own_transfers), so a UPI payment and an
    unrelated refund of the same amount days later don't pair. Nearest in time first; each row pairs once."""
    day = {r['id']: date.fromisoformat(r['received_at'][:10]) for r in rows}
    credits = defaultdict(list)  # currency -> [(day, id)] sorted
    for r in rows:
        if r['direction'] == 'credit' and r['id'] not in overrides:
            credits[r['currency']].append((day[r['id']], r['id']))
    for v in credits.values():
        v.sort()
    by_id = {r['id']: r for r in rows}
    at = {r['id']: datetime.fromisoformat(r['received_at']) for r in rows}
    same = defaultdict(list)
    for r in rows:
        same[r['currency'], r['amount_minor']].append(r['id'])
    alone = lambda d, c: not any(i not in (d['id'], c['id']) and min(abs((at[i] - at[d['id']]).total_seconds()), abs((at[i] - at[c['id']]).total_seconds())) <= SLOW_PAIR_SECONDS
                                 for i in same[d['currency'], d['amount_minor']])
    paired = {}
    for d, c in manual:  # yours first
        if d in by_id and c in by_id:
            paired[d], paired[c] = c, d
    words = transfer_like
    for d in sorted((r for r in rows if r['direction'] == 'debit' and r['id'] not in overrides and r['id'] not in paired and not PURCHASE.search(r['body'])),
                    key=lambda r: r['received_at']):
        best = None
        for cur, lo_hi in [(d['currency'], None)] + [(c2, rng) for (c1, c2), rng in FX_RATIO.items() if c1 == d['currency']]:
            lst = credits.get(cur, [])
            for k in range(bisect_left(lst, (day[d['id']], -1)), len(lst)):
                when, cid = lst[k]
                gap = (when - day[d['id']]).days
                if gap > LINK_DAYS:
                    break
                c = by_id[cid]
                if cid in paired or acct[cid] == acct[d['id']]:
                    continue
                if lo_hi is None:
                    secs = abs((at[cid] - at[d['id']]).total_seconds())
                    if c['amount_minor'] != d['amount_minor'] or not (d['statement_id'] or c['statement_id']) and secs > PAIR_WINDOW_SECONDS and not (
                            secs <= SLOW_PAIR_SECONDS and alone(d, c)):
                        continue
                elif not lo_hi[0] <= c['amount_minor'] / d['amount_minor'] <= lo_hi[1]:
                    continue
                sms = not (d['statement_id'] or c['statement_id'])
                if lo_hi is None and sms and not (words(d) and words(c)):
                    continue  # two SMS: both must say transfer, as own_transfers asks
                if lo_hi and not (words(d) and (words(c) or c['statement_id'] or account_of(c['body']))):
                    continue  # changing currency: a transfer out, arriving in a named account (not an advert's 'credited to you')
                if lo_hi is None and not sms and not (words(d) or words(c)):
                    continue
                if best is None or gap < best[0]:
                    best = (gap, cid)
                break  # the nearest in this currency
        if best:
            paired[d['id']], paired[best[1]] = best[1], d['id']
    return paired


def import_statement(db, account, kind, currency, digits, file_name, data, password=None):
    """Store a statement's transactions under `account`. A row that an SMS already stored shows (same amount, way and
    currency within 2 days, and the same last 4 digits when both have them) is skipped: the SMS stays. Importing the
    same file again adds nothing. Returns the statement's summary."""
    got = read_statement(data, password)
    rows = got['rows']
    if not rows:
        raise StatementError('no transactions were found in this file')
    first, last = span(rows)
    db.run('INSERT INTO accounts (name, kind, currency, digits) VALUES (:n, :k, :c, :d) '
           'ON CONFLICT (name) DO UPDATE SET kind = :k, currency = :c, digits = :d', n=account, k=kind, c=currency, d=digits)
    sms = [r for r in db.run('SELECT id, received_at, body, amount_minor, direction FROM transactions '
                             'WHERE statement_id IS NULL AND currency = :c AND received_at >= :a AND received_at < :b',
                             c=currency, a=(date.fromisoformat(first) - timedelta(days=3)).isoformat(),
                             b=(date.fromisoformat(last) + timedelta(days=4)).isoformat())]
    used, fresh, seen = set(), [], defaultdict(int)
    for r in rows:
        twin = next((m for m in sms if m['id'] not in used and m['amount_minor'] == r['amount_minor'] and m['direction'] == r['direction']
                     and abs((date.fromisoformat(m['received_at'][:10]) - date.fromisoformat(r['day'])).days) <= 2
                     and not (digits and account_of(m['body'])[-4:].isdigit() and account_of(m['body'])[-4:] != digits)), None)
        if twin:
            used.add(twin['id'])
            continue
        amount = f"{currency} {r['amount_minor'] / 100:,.2f}"
        body = (f"{r['desc'] or 'No description'} · {'Paid' if r['direction'] == 'debit' else 'Received'} {amount} · {account}, {r['day']}"
                + (f" · balance {r['balance_minor'] / 100:,.2f}" if r['balance_minor'] is not None else ''))
        seen[body] += 1
        if seen[body] > 1:
            body += f' · #{seen[body]}'  # two identical rows in one file are two transactions
        fresh.append({'received_at': f"{r['day']}T12:00:00{'+05:30' if currency == 'INR' else '+03:00'}", 'sender': account,
                      'body': body, 'amount_minor': r['amount_minor'], 'currency': currency, 'direction': r['direction'],
                      'merchant': ' '.join(r['desc'].split())[:80]})
    deleted = {r['body'] for r in db.run('SELECT body FROM deleted_sms')} if fresh else set()
    fresh = [f for f in fresh if f['body'] not in deleted]
    sid = db.run('INSERT INTO statements (account, file_name, imported_at, first_day, last_day, read_rows, new_rows, sms_rows, checked) '
                 'VALUES (:a, :f, :t, :x, :y, :n, 0, :s, :k) RETURNING id', a=account, f=file_name[:120],
                 t=datetime.now(timezone.utc).isoformat(timespec='seconds'), x=first, y=last, n=len(rows), s=len(used), k=got['checked'])[0]['id']
    new = 0
    columns = ('received_at', 'sender', 'body', 'amount_minor', 'currency', 'direction', 'merchant')
    for i in range(0, len(fresh), 300):
        chunk = fresh[i:i + 300]
        values = ', '.join('(' + ', '.join(f':{c}{n}' for c in columns) + f', :sid)' for n in range(len(chunk)))
        new += len(db.run(f'INSERT INTO transactions ({", ".join(columns)}, statement_id) VALUES {values} ON CONFLICT (body) DO NOTHING RETURNING id',
                          sid=sid, **{f'{c}{n}': row[c] for n, row in enumerate(chunk) for c in columns}))
    db.run('UPDATE statements SET new_rows = :n WHERE id = :id', n=new, id=sid)
    return {'id': sid, 'account': account, 'kind': got['kind'], 'first_day': first, 'last_day': last, 'read': len(rows),
            'new': new, 'from_sms': len(used), 'already': len(fresh) - new, 'checked': got['checked'], 'currency_in_file': got['currency']}


def statements(db):
    return {'accounts': db.run('SELECT * FROM accounts ORDER BY name'),
            'statements': db.run('SELECT * FROM statements ORDER BY imported_at DESC, id DESC')}


def delete_statement(db, sid):
    """Undo an import: its transactions go (and can be imported again), the account stays."""
    if not db.run('SELECT id FROM statements WHERE id = :id', id=sid):
        raise KeyError('no such statement')
    for table, key in (('txn_categories', 'txn_id'), ('txn_friends', 'txn_id')):
        db.run(f'DELETE FROM {table} WHERE {key} IN (SELECT id FROM transactions WHERE statement_id = :id)', id=sid)
    db.run('DELETE FROM transactions WHERE statement_id = :id', id=sid)
    db.run('DELETE FROM statements WHERE id = :id', id=sid)


WALLETS = re.compile(r'stc ?pay|urpay|barq|wallet', re.I)  # PhonePe/GPay are UPI: the money moves in a bank account
BNPL = re.compile(r'tamara|tabby|postpay|spotii|simpl|lazypay', re.I)
FOLLOW_DAYS = 10


def trail(db, first=None, last=None):
    """Where money went between your accounts: each account's in and out, the flows between accounts (and on to
    loans), and journeys: a transfer, then the next transfer or loan payment out of the account it reached
    (likely the same money: within FOLLOW_DAYS and no more than what arrived), and so on."""
    every = transactions(db)
    by_id = {t['id']: t for t in every}
    kinds = {a['name']: a['kind'] for a in db.run('SELECT name, kind FROM accounts')}
    loans = [h for h in db.run('SELECT id, name, kind, currency, pattern, emi_minor FROM holdings') if h['kind'] in DEBTS]
    def why_loan(t):
        """(loan name, why) for a payment to one of your loans: its account digits in the SMS, or exactly its EMI. Not
        once you've given the transaction a category of your own (other than Rent & EMI): that's your correction."""
        if t['direction'] != 'debit' or t['fixed'] and t['category'] != 'Rent & EMI':
            return None
        for h in loans:
            if h['currency'] == t['currency'] and h['pattern'].strip() and h['pattern'].strip().lower() in t['body'].lower():
                return h['name'], f"the message mentions {h['pattern'].strip()}, the loan's account digits"
            if h['currency'] == t['currency'] and h['emi_minor'] and round(t['amount'] * 100) == h['emi_minor']:
                return h['name'], "exactly the loan's EMI"
        return None
    loan_of = lambda t: (why_loan(t) or (None,))[0]
    # Your regular payments from the Plan tab (Parents, Maid…): a payment of exactly that amount, or one you gave
    # that name as its category, went to them.
    found = db.run("SELECT value FROM settings WHERE key = 'allocations'")
    regular = [(o['name'].strip(), round(float(o.get('amount') or 0) * 100), o.get('cur') or 'SAR')
               for o in (json.loads(found[0]['value']).get('others') or [] if found else []) if str(o.get('name') or '').strip()]

    def why_payee(t):
        if t['direction'] != 'debit' or t['linked'] in by_id or why_loan(t):
            return None
        for name, minor, cur in regular:
            if t['category'].lower() == name.lower():
                return name, f'you marked it {name}'
            if not t['fixed'] and minor and cur == t['currency'] and round(t['amount'] * 100) == minor:
                return name, f'exactly your monthly {name} payment'
        return None
    inside = lambda t: (not first or t['received_at'][:10] >= first) and (not last or t['received_at'][:10] <= last)
    kind_of = lambda name: kinds.get(name) or ('wallet' if WALLETS.search(name) else 'bnpl' if BNPL.search(name)
                                               else 'card' if 'Card' in name else 'bank')
    accounts = {}
    for t in every:
        if not inside(t) or t['category'] == IGNORED:
            continue
        a = accounts.setdefault(t['account'], {'name': t['account'], 'kind': kind_of(t['account']), 'in': defaultdict(float),
                                               'out': defaultdict(float), 'count': 0, 'last': ''})
        a['in' if t['direction'] == 'credit' else 'out'][t['currency']] += t['amount']
        a['count'] += 1
        a['last'] = max(a['last'], t['received_at'][:10])
    flows = {}

    proof = lambda t: {k: t[k] for k in ('id', 'account', 'amount', 'currency', 'direction', 'body', 'category')} | {'day': t['received_at'][:10]}

    def flow(src, dst, kind, sent, got, why):
        f = flows.setdefault((src, dst), {'from': src, 'to': dst, 'kind': kind, 'count': 0, 'sent': defaultdict(float), 'got': defaultdict(float), 'last': '', 'proof': []})
        f['count'] += 1
        f['proof'].append({'why': why, 'items': [proof(sent)] + ([proof(got)] if got is not sent else [])})
        f['sent'][sent['currency']] += sent['amount']
        f['got'][got['currency']] += got['amount']
        f['last'] = max(f['last'], sent['received_at'][:10])
    salary = lambda t: t['direction'] == 'credit' and t['category'] == 'Salary'
    for t in every:
        if salary(t) and inside(t):  # where the money starts
            flow('Salary', t['account'], 'salary', t, t, 'your salary arriving')
        if t['direction'] == 'debit' and inside(t):
            if t['linked'] in by_id:
                c = by_id[t['linked']]
                gap = (date.fromisoformat(c['received_at'][:10]) - date.fromisoformat(t['received_at'][:10])).days
                when = 'the same day' if not gap else f'{gap} day{"s" * (gap > 1)} later'
                flow(t['account'], c['account'], 'transfer', t, c,
                     f"arrived {when}" + (f" at {c['amount'] / t['amount']:.2f} {c['currency']} per {t['currency']}" if c['currency'] != t['currency'] else ', same amount'))
            elif found := why_loan(t):
                flow(t['account'], found[0], 'loan', t, t, found[1])
            elif found := why_payee(t):
                flow(t['account'], found[0], 'payee', t, t, found[1])
        if t.get('peer') and not t['linked'] and inside(t):  # money with someone on this app
            who = f"👤 {t['peer']['who']}"
            why = f"arrived in {t['peer']['who']}’s {t['peer']['account']} on {t['peer']['day']}" if t['direction'] == 'debit' else f"sent from {t['peer']['who']}’s {t['peer']['account']} on {t['peer']['day']}"
            flow(*((t['account'], who) if t['direction'] == 'debit' else (who, t['account'])), 'family', t, t, why)
    # Journeys: after money arrives in an account, the next transfer/loan payment out of it.
    out_of = defaultdict(list)
    for t in sorted(every, key=lambda t: t['received_at']):
        if t['direction'] == 'debit' and (t['category'] not in NOT_COUNTED or t['linked'] in by_id):
            out_of[t['account']].append(t)
    # A credit's money goes on in every transfer/loan payment out of that account in the next FOLLOW_DAYS, as long as
    # together they're no more than what arrived: one big transfer home, split out to each loan's account.
    nxt, used = defaultdict(list), set()
    for c in sorted((t for t in every if t['direction'] == 'credit' and t['linked'] in by_id), key=lambda t: t['received_at']):
        day, room = date.fromisoformat(c['received_at'][:10]), 1.02 * c['amount']
        for d in out_of[c['account']]:
            gap = (date.fromisoformat(d['received_at'][:10]) - day).days
            if gap < 0 or d['id'] in used or d['currency'] != c['currency']:
                continue
            if gap > FOLLOW_DAYS:
                break
            if d['amount'] <= room and (d['amount'] >= 0.05 * c['amount'] or d['linked'] in by_id or loan_of(d)):  # not each coffee
                nxt[c['id']].append(d)
                used.add(d['id'])
                room -= d['amount']
    hop = lambda t, way, likely: {'id': t['id'], 'account': t['account'], 'kind': kind_of(t['account']), 'day': t['received_at'][:10], 'way': way,
                                  'amount': t['amount'], 'currency': t['currency'], 'desc': t['merchant'] or wording(t['body'])[len(WORDING):][:60],
                                  'likely': likely, 'then': []}

    def follow(t, likely, depth=0):
        """A journey as a tree: sent → arrived → (each onward transfer, or the loan it paid)."""
        node = hop(t, 'sent', likely)
        if t['linked'] in by_id and depth < 6:
            c = by_id[t['linked']]
            node['then'] = [{**hop(c, 'arrived', False), 'then': [follow(d, True, depth + 1) for d in nxt[c['id']]]}]
        elif name := loan_of(t):
            node['then'] = [{**hop(t, 'loan', False), 'account': name, 'kind': 'loan', 'desc': ''}]
        elif found := why_payee(t):
            node['then'] = [{**hop(t, 'payee', False), 'account': found[0], 'kind': 'payee', 'desc': ''}]
        elif likely:  # money that left your accounts: to a person or a shop
            node['way'] = 'paid'
        return node
    flat = lambda n: [n] + [m for k in n['then'] for m in flat(k)]
    journeys = [follow(d, False) for d in sorted((t for t in every if t['direction'] == 'debit' and t['id'] not in used and t['linked'] in by_id),
                                                 key=lambda t: t['received_at'])]
    # A journey starting from an account your salary reached in the month before begins with that salary.
    pay = sorted((t for t in every if salary(t)), key=lambda t: t['received_at'])
    for i, j in enumerate(journeys):
        start = date.fromisoformat(j['day'])
        got = [t for t in pay if t['account'] == j['account'] and 0 <= (start - date.fromisoformat(t['received_at'][:10])).days <= 35]
        if got:
            journeys[i] = {**hop(got[-1], 'salary', False), 'then': [j]}
    journeys = [j for j in journeys if any(inside(by_id[n['id']]) for n in flat(j))]
    plain = lambda d: {k: round(v, 2) for k, v in d.items()}
    return {'accounts': sorted(({**a, 'in': plain(a['in']), 'out': plain(a['out'])} for a in accounts.values()), key=lambda a: -a['count']),
            'flows': sorted(({**f, 'sent': plain(f['sent']), 'got': plain(f['got']), 'proof': f['proof'][::-1][:20]} for f in flows.values()),
                            key=lambda f: (-f['count'], f['from'])),
            'journeys': journeys[::-1][:60], 'unmatched': unmatched([t for t in every if not (why_loan(t) or why_payee(t))], inside)}


def why_not(a, b):
    """Why the automatic matching passed over two transactions whose amounts fit, in words."""
    d, c = (a, b) if a['direction'] == 'debit' else (b, a)
    secs = (datetime.fromisoformat(c['received_at']) - datetime.fromisoformat(d['received_at'])).total_seconds()
    gap = f'{abs(secs) / 86400:.0f} days' if abs(secs) >= 86400 else f'{int(abs(secs) // 3600)} h {int(abs(secs) % 3600 // 60)} min'
    if secs < -PAIR_WINDOW_SECONDS:
        return f'the money arrived {gap} before it was sent, by the message times'
    if d['currency'] != c['currency']:
        if not transfer_like(d):
            return "the sending message doesn't say transfer, remittance or similar"
        return f'arrived {gap} later; automatic matching allows {LINK_DAYS} days'
    if not (d['statement'] or c['statement']):
        if abs(secs) > PAIR_WINDOW_SECONDS:
            return (f'the two messages are {gap} apart; two SMS are matched automatically up to a day apart' if abs(secs) > SLOW_PAIR_SECONDS
                    else f'the two messages are {gap} apart, and another transaction of the same amount is near, so it could be either')
        missing = [x['account'] for x in (d, c) if not transfer_like(x)]
        if missing:
            return f"the message from {missing[0]} doesn't say transfer, IMPS, NEFT or similar"
    return f'arrived {gap} later; automatic matching allows {LINK_DAYS} days' if secs > LINK_DAYS * 86400 else ''


# 'Avl Bal INR 65,243.48', 'Balance: SAR 18,200.00', 'Avl Limit: INR 3,14,721.04', '· balance 12,345.67' (statements), 'الرصيد'.
# The number needs decimals, Indian/Western commas or 3+ digits, and isn't a date, so 'balance as on 26/10' isn't read.
BALANCE = re.compile(r'\b(limit|lmt|bal(?:ance)?)\b|(الرصيد)', re.I)
# Never the middle of a longer number, and a plain number has at most 9 digits: the 17 digits of a PF ID
# ('MHBAN00123450000012345') are not a balance.
BAL_NUM = re.compile(r'(?<![\d,])(\d[\d,]*\.\d{1,2}|\d{1,3}(?:,\d{2,3})+|\d{3,9})(?![\d/]|-\d)')


def balances(every):
    """Each account's latest balance (or a card's available limit) from the newest SMS or statement row that states one."""
    out = {}
    for t in sorted(every, key=lambda t: t['received_at'], reverse=True):
        if t['account'] in out or t['category'] == IGNORED:
            continue
        body = t['body'].translate(ARABIC_DIGITS)
        for m in BALANCE.finditer(body):
            n = BAL_NUM.search(body[m.end():m.end() + 30])
            if n and not re.match(r'\s*(?:as\s+on|on)\b', body[m.end():m.end() + 8], re.I):
                out[t['account']] = {'account': t['account'], 'kind': 'limit' if (m.group(1) or '').lower() in ('limit', 'lmt') else 'balance',
                                     'amount': float(n.group(1).replace(',', '')), 'currency': t['currency'], 'day': t['received_at'][:10], 'at': t['received_at'],
                                     'txn_id': t['id'], 'body': t['body']}
                break
    return sorted(out.values(), key=lambda b: b['day'], reverse=True)


def save_account(db, label, name, currency='', balance=None, hidden=None, kind=''):
    """Rename an account the SMS named (label → name), hide or show it, or type its balance now. With no label: add an
    account of your own (its SMS join it through `digits` in the name's last 4, e.g. 'SNB salary 1234')."""
    name = ' '.join(str(name).split())[:60]
    if not name:
        raise ValueError('a name is needed')
    rows = db.run('SELECT * FROM accounts WHERE label = :l OR name = :l', l=label) if label else []
    if any(r['name'] == name for r in db.run('SELECT name FROM accounts WHERE name = :n', n=name)) and not (rows and rows[0]['name'] == name):
        raise ValueError('another account already has that name')
    digits = (re.findall(r'(\d{3,4})(?!.*\d)', label or name) or [''])[0]
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    if rows:
        r = rows[0]
        if r['name'] != name and not r['label']:  # an account from a statement: its rows carry the name
            db.run('UPDATE transactions SET sender = :n WHERE statement_id IS NOT NULL AND sender = :o', n=name, o=r['name'])
            db.run('UPDATE statements SET account = :n WHERE account = :o', n=name, o=r['name'])
        db.run('UPDATE accounts SET name = :n, hidden = :h, kind = :k WHERE name = :o', n=name, h=r['hidden'] if hidden is None else int(hidden),
               k=kind if kind in ACCOUNT_KINDS else r['kind'], o=r['name'])
    else:
        db.run('INSERT INTO accounts (name, kind, currency, digits, label, hidden) VALUES (:n, :k, :c, :d, :l, :h)',
               n=name, k=kind if kind in ACCOUNT_KINDS else '', c=currency or 'SAR', d='' if label else digits, l=label, h=int(bool(hidden)))  # '': not chosen, detect it
    if balance is not None:
        db.run('UPDATE accounts SET balance_minor = :b, balance_at = :t WHERE name = :n', b=round(Decimal(str(balance)) * 100), t=now, n=name)
    return name


CREDIT_CARD = re.compile(r'credit\s*card|\b(?:avl|avbl|available)\.?\s*(?:credit\s+)?(?:limit|lmt)\b|credit\s+limit|بطاقة\s*(?:ال)?ائتمان', re.I)
DEBIT_CARD = re.compile(r'debit\s*card|\bmada\b|مدى|\batm\s*card\b|بطاقة\s*(?:ال)?صراف', re.I)


def account_kinds(every, typed):
    """{account: (kind, how)}, how: 'set' by you, 'sms' read from its messages, 'name' only guessed. The type you set (Edit, or at statement upload), else what its SMS say (a credit
    card: 'credit card', 'Avl Limit'; a debit card: 'debit card', 'mada', 'ATM card'), else card or bank by its name."""
    votes = defaultdict(lambda: [0, 0])
    for t in every:
        votes[t['account']][0] += bool(CREDIT_CARD.search(t['body']))
        votes[t['account']][1] += bool(DEBIT_CARD.search(t['body']))
    chosen = {a['name']: a['kind'] for a in typed if a['kind']}
    out = {}
    for name in set(votes) | set(chosen):
        c, d = votes[name]
        out[name] = ((chosen[name], 'set') if name in chosen else ('card' if c >= d else 'debit', 'sms') if c or d
                     else ('card' if 'Card' in name else 'bank', 'name'))
    return out


def delete_account(db, name):
    """Remove an account you added (one the SMS named is hidden instead, so its messages don't come back)."""
    db.run('DELETE FROM accounts WHERE name = :n AND label = \'\' AND name NOT IN (SELECT account FROM statements)', n=name)


def known_balances(every, typed=()):
    """Each account's latest balance: what its SMS last stated, or the one you typed when that's newer (or the SMS
    never state one). The one list Accounts & cards, 'Can I buy it?' and the assistants all use."""
    stated = {b['account']: b for b in balances(every)}
    for a in typed:
        b = stated.get(a['name'])
        if a['balance_at'] and not a['hidden'] and (not b or b['kind'] != 'balance' or a['balance_at'] > b['at']):
            stated[a['name']] = {'account': a['name'], 'kind': 'balance', 'amount': a['balance_minor'] / 100, 'currency': a['currency'],
                                 'day': a['balance_at'][:10], 'at': a['balance_at'], 'typed': True}
    return sorted(stated.values(), key=lambda b: b['day'], reverse=True)


def account_months(every, month, typed=()):
    """Each bank account's month: the balance carried in from the month before, money in, money out, and the balance
    at the end (the next month's opening). Worked back and forward from the latest balance the bank stated, so it's
    exact when every transaction of the account is here; a missing SMS shifts the months before it."""
    tz = ZoneInfo(os.environ.get('EXPENSE_TZ') or 'Asia/Riyadh')
    y, m = map(int, month.split('-'))
    start = datetime(y, m, 1, tzinfo=tz)
    end = datetime(y + m // 12, m % 12 + 1, 1, tzinfo=tz)
    when = lambda t: datetime.fromisoformat(t['received_at'])
    out = []
    credit = {n for n, (k, _) in account_kinds(every, typed).items() if k == 'card'}  # a credit card's 'available balance' is credit left, not money
    for b in known_balances(every, typed):
        if b['account'] in credit:
            continue
        if b['kind'] != 'balance':
            continue  # a card's figure is its available limit, not money you hold
        at, now = datetime.fromisoformat(b['at']), round(b['amount'] * 100)
        mine = [(when(t), round(t['amount'] * 100) * (1 if t['direction'] == 'credit' else -1)) for t in every
                if t['account'] == b['account'] and t['currency'] == b['currency'] and t['category'] != IGNORED]
        # The balance just before moment x: back from the stated one (which already includes its own transaction).
        before = lambda x: now - sum(v for w, v in mine if x <= w <= at) if x <= at else now + sum(v for w, v in mine if at < w < x)
        got = sum(v for w, v in mine if start <= w < end and v > 0)
        spent = -sum(v for w, v in mine if start <= w < end and v < 0)
        opening = before(start)
        out.append({'account': b['account'], 'currency': b['currency'], 'opening': opening / 100, 'in': got / 100, 'out': spent / 100,
                    'closing': (opening + got - spent) / 100, 'current': b['amount'], 'as_of': b['day'], 'typed': b.get('typed', False),
                    'txn_id': b.get('txn_id'), 'body': b.get('body', '')})
    return out


def unmatched(every, inside):
    """Transfers in the period with no other side found, newest first, each with the likeliest other sides (opposite
    way, another account, within a week; same amount or an exchange rate first) for you to link by hand."""
    def fits(a, b):
        if a['currency'] == b['currency']:
            return abs(a['amount'] - b['amount']) <= 0.02 * max(a['amount'], b['amount'])
        d, c = (a, b) if a['direction'] == 'debit' else (b, a)
        rng = FX_RATIO.get((d['currency'], c['currency']))
        return bool(rng) and rng[0] <= c['amount'] / d['amount'] <= rng[1]
    open_ = [t for t in every if not t['linked'] and not t.get('peer') and t['category'] != IGNORED and transfer_like(t) and not PURCHASE.search(t['body'])]
    out = []
    for t in sorted((t for t in open_ if inside(t)), key=lambda t: t['received_at'], reverse=True)[:40]:
        day = date.fromisoformat(t['received_at'][:10])
        near = [o for o in every if o['direction'] != t['direction'] and o['account'] != t['account'] and not o['linked']
                and abs((date.fromisoformat(o['received_at'][:10]) - day).days) <= 7 and o['category'] != IGNORED]
        near.sort(key=lambda o: (not fits(t, o), abs((date.fromisoformat(o['received_at'][:10]) - day).days)))
        pick = lambda o: {k: o[k] for k in ('id', 'account', 'amount', 'currency', 'direction', 'merchant', 'body')} | {'day': o['received_at'][:10], 'fits': fits(t, o)}
        out.append({**pick(t), 'candidates': [pick(o) for o in near[:6]], 'why_not': near and fits(t, near[0]) and why_not(t, near[0]) or ''})
    return out


WORDING = 'sms:'  # merchant_categories keys starting with this are message wordings, not merchant names


def wording(body):
    """A message's wording without its numbers (amounts, dates, references, card digits), so
    'Salary credited SAR 9,000 on 01/10' and 'Salary credited SAR 9,500 on 01/11' count as similar."""
    return WORDING + ' '.join(re.sub(r'[\d٠-٩][\d٠-٩,.:/*-]*', '#', body.lower()).split())


def set_category(db, txn_id, category, whole_merchant):
    """Save a correction. With whole_merchant, every past and future transaction from that merchant follows it;
    for a message with no merchant, every message with the same wording does."""
    found = db.run('SELECT body FROM transactions WHERE id = :id', id=txn_id)
    if not found:
        raise KeyError('no such transaction')
    merchant = merchant_of(found[0]['body']).strip().lower() or wording(found[0]['body'])
    if category != OWN_TRANSFER:
        db.run('DELETE FROM txn_links WHERE debit_id = :id OR credit_id = :id', id=txn_id)
    if whole_merchant:
        db.run('INSERT INTO merchant_categories (merchant, category) VALUES (:m, :c) '
               'ON CONFLICT (merchant) DO UPDATE SET category = excluded.category', m=merchant, c=category)
        db.run('DELETE FROM txn_categories WHERE txn_id = :id', id=txn_id)  # so this one follows the merchant rule too
    else:
        db.run('INSERT INTO txn_categories (txn_id, category) VALUES (:id, :c) '
               'ON CONFLICT (txn_id) DO UPDATE SET category = excluded.category', id=txn_id, c=category)


def set_friend(db, txn_id, friend, theirs_minor):
    """Tag a transaction with a friend and their share of it (for a credit: they paid you back). No name removes the tag.
    A name typed again in other capitals is the same friend."""
    found = db.run('SELECT amount_minor, direction FROM transactions WHERE id = :id', id=txn_id)
    if not found:
        raise KeyError('no such transaction')
    if not friend:
        db.run('DELETE FROM txn_friends WHERE txn_id = :id', id=txn_id)
        share(db, [txn_id])
        return
    known = {r['friend'].lower(): r['friend'] for r in db.run('SELECT DISTINCT friend FROM txn_friends')}
    friend = known.get(friend.lower(), friend)
    amount, credit = found[0]['amount_minor'], found[0]['direction'] == 'credit'
    if not credit and not 0 < theirs_minor <= amount:
        raise ValueError('their share must be more than 0 and at most the amount')
    db.run('INSERT INTO txn_friends (txn_id, friend, theirs_minor, settled) VALUES (:id, :f, :t, 0) '
           'ON CONFLICT (txn_id) DO UPDATE SET friend = excluded.friend, theirs_minor = excluded.theirs_minor, settled = 0',
           id=txn_id, f=friend, t=amount if credit else theirs_minor)
    share(db, [txn_id])


def delete_transaction(db, txn_id):
    """Remove a transaction for good: its SMS is remembered so it isn't stored again when the phone resends it."""
    found = db.run('SELECT body FROM transactions WHERE id = :id', id=txn_id)
    if not found:
        raise KeyError('no such transaction')
    db.run('INSERT INTO deleted_sms (body) VALUES (:b) ON CONFLICT (body) DO NOTHING', b=found[0]['body'])
    for table, key in (('txn_categories', 'txn_id'), ('txn_friends', 'txn_id'), ('transactions', 'id')):
        db.run(f'DELETE FROM {table} WHERE {key} = :id', id=txn_id)
    share(db, [txn_id])


def settle_friend(db, friend, currency):
    """Square up with a friend in one currency: everything so far stops counting toward what they owe."""
    db.run('UPDATE txn_friends SET settled = 1 WHERE friend = :f AND txn_id IN (SELECT id FROM transactions WHERE currency = :c)',
           f=friend, c=currency)
    share(db, [r['txn_id'] for r in db.run('SELECT txn_id FROM txn_friends WHERE friend = :f', f=friend)])


def public_of(db):
    """The public space of the same database as `db` (where people and shared splits live)."""
    return DB(db.url if db.pg else db.main_file)


def set_contact(db, name, email):
    """Remember a friend's email (once), and share what's tagged with them so far."""
    email = email.strip().lower()
    if email and not EMAIL.match(email):
        raise ValueError('enter a valid email')
    known = {r['friend'].lower(): r['friend'] for r in db.run('SELECT DISTINCT friend FROM txn_friends')}
    name = known.get(name.lower(), name)
    db.run('DELETE FROM contacts WHERE LOWER(name) = :n', n=name.lower())
    db.run('INSERT INTO contacts (name, email) VALUES (:n, :e)', n=name, e=email)
    share(db, [r['txn_id'] for r in db.run('SELECT txn_id FROM txn_friends WHERE LOWER(friend) = :n', n=name.lower())])
    if email:
        peers_quietly(db)


def share(db, txn_ids):
    """Rewrite the shared rows for these transactions from this person's own data: a tag with a friend whose email is
    another person on this app becomes a row they see; anything else (no tag, no email, not on the app) has none."""
    if not txn_ids:
        return
    emails = {r['name'].lower(): r['email'] for r in db.run("SELECT name, email FROM contacts WHERE email != ''")}
    pub = public_of(db)
    try:
        me = pub.run('SELECT email FROM app_users WHERE space = :s', s=db.space)
        people = {r['email'] for r in pub.run('SELECT email FROM app_users')}
        for txn_id in txn_ids:
            pub.run('DELETE FROM shared_splits WHERE from_space = :s AND txn_id = :id', s=db.space, id=txn_id)
            found = db.run('SELECT t.amount_minor, t.currency, t.direction, t.body, t.received_at, f.friend, f.theirs_minor, f.settled '
                           'FROM transactions t JOIN txn_friends f ON f.txn_id = t.id WHERE t.id = :id', id=txn_id)
            if not found or not me:
                continue  # untagged, deleted, or you have no email to be known by
            t = found[0]
            to = emails.get(t['friend'].lower())
            if not to or to not in people or to == me[0]['email']:
                continue
            amount = -t['amount_minor'] if t['direction'] == 'credit' else min(t['theirs_minor'], t['amount_minor'])
            pub.run('INSERT INTO shared_splits (from_space, txn_id, from_email, to_email, amount_minor, currency, what, day, settled) '
                    'VALUES (:s, :id, :f, :to, :a, :c, :w, :d, :x)', s=db.space, id=txn_id, f=me[0]['email'], to=to, a=amount,
                    c=t['currency'], w=(merchant_of(t['body']) or wording(t['body'])[len(WORDING):])[:60], d=t['received_at'][:10], x=t['settled'])
    finally:
        pub.close()


def on_app(db, email):
    """Whether an email belongs to someone on this app (other than you)."""
    email = email.strip().lower()
    pub = public_of(db)
    try:
        return bool(email) and bool(pub.run('SELECT 1 FROM app_users WHERE email = :e AND space != :s', e=email, s=db.space))
    finally:
        pub.close()


def relink(pub, email):
    """Someone was just added: everyone who had already saved their email gets their splits shared with them now."""
    for u in pub.run("SELECT space FROM app_users WHERE space != '' AND email != :e", e=email):
        theirs = DB(pub.url if pub.pg else pub.main_file, u['space'])
        try:
            names = [r['name'] for r in theirs.run('SELECT name FROM contacts WHERE email = :e', e=email)]
            for n in names:
                share(theirs, [r['txn_id'] for r in theirs.run('SELECT txn_id FROM txn_friends WHERE LOWER(friend) = :n', n=n.lower())])
        finally:
            theirs.close()


PEER_DAYS, PEER_FX_DAYS = 1, LINK_DAYS


def peers_of(db):
    """People on the app who have you saved with your email, as you have them: [(your name for them, their space,
    their name for you)]. Linking needs both, so no one is matched against a stranger's data."""
    mine = {r['email']: r['name'] for r in db.run("SELECT name, email FROM contacts WHERE email != ''")}
    if not mine:
        return []
    pub = public_of(db)
    try:
        me = pub.run('SELECT email FROM app_users WHERE space = :s', s=db.space)
        people = [(u['email'], u['space']) for u in pub.run("SELECT email, space FROM app_users WHERE space != '' AND space != :s", s=db.space) if u['email'] in mine]
    finally:
        pub.close()
    out = []
    for email, space in people if me else []:
        theirs = DB(db.url if db.pg else db.main_file, space)
        try:
            back = theirs.run('SELECT name FROM contacts WHERE email = :e', e=me[0]['email'])
        finally:
            theirs.close()
        if back:
            out.append((mine[email], space, back[0]['name']))
    return out


def link_peers(db):
    """Pair money leaving one person's account with the same money arriving in the other's (same amount, or the same
    at an exchange rate), close in time, and only when nothing else could be it on either side. Both sides keep the
    link. shortcut: compares all unlinked transactions of both people each time; limit to recent ones if it gets slow."""
    for name, space, my_name in peers_of(db):
        other = DB(db.url if db.pg else db.main_file, space)
        try:
            when = lambda t: datetime.fromisoformat(t['received_at'])
            def open_(d):
                taken = {r['txn_id'] for r in d.run('SELECT txn_id FROM peer_links')}
                return [t for t in transactions(d) if t['id'] not in taken and not t['linked'] and t['category'] != IGNORED
                        and not PURCHASE.search(t['body'])]
            A, B = open_(db), open_(other)

            def fits(x, y):  # x and y opposite ways: the same money?
                d, c = (x, y) if x['direction'] == 'debit' else (y, x)
                gap = (when(c) - when(d)).total_seconds()
                if d['currency'] == c['currency']:
                    return d['amount'] == c['amount'] and abs(gap) <= PEER_DAYS * 86400
                rng = FX_RATIO.get((d['currency'], c['currency']))
                return bool(rng) and rng[0] <= c['amount'] / d['amount'] <= rng[1] and 0 <= gap <= PEER_FX_DAYS * 86400
            for a in A:
                cands = [b for b in B if b['direction'] != a['direction'] and fits(a, b) and (transfer_like(a) or transfer_like(b))]
                if len(cands) != 1 or len([x for x in A if x['direction'] == a['direction'] and fits(x, cands[0])]) != 1:
                    continue  # nothing, or more than one thing it could be: leave it
                b = cands[0]
                db.run('INSERT INTO peer_links (txn_id, who, their_space, their_txn, their_account, their_day) VALUES (:i, :w, :s, :t, :a, :d) ON CONFLICT (txn_id) DO NOTHING',
                       i=a['id'], w=name, s=space, t=b['id'], a=b['account'], d=b['received_at'][:10])
                other.run('INSERT INTO peer_links (txn_id, who, their_space, their_txn, their_account, their_day) VALUES (:i, :w, :s, :t, :a, :d) ON CONFLICT (txn_id) DO NOTHING',
                          i=b['id'], w=my_name, s=db.space, t=a['id'], a=a['account'], d=a['received_at'][:10])
                B.remove(b)
        finally:
            other.close()


def unlink_peer(db, txn_id):
    """Not the same money after all: unlinked on both sides, and never linked again."""
    found = db.run('SELECT * FROM peer_links WHERE txn_id = :id', id=txn_id)
    if not found:
        raise KeyError('not linked')
    db.run('UPDATE peer_links SET blocked = 1 WHERE txn_id = :id', id=txn_id)
    other = DB(db.url if db.pg else db.main_file, found[0]['their_space'])
    try:
        other.run('UPDATE peer_links SET blocked = 1 WHERE txn_id = :id', id=found[0]['their_txn'])
    finally:
        other.close()


def peers_quietly(db):
    """Linking with the other person is extra: an error there must never lose the SMS that was just saved."""
    try:
        link_peers(db)
    except Exception as e:
        print(f'link_peers failed: {type(e).__name__}: {e}', file=sys.stderr)


def shared(db):
    """What other people on the app say you owe them (unsettled), what you've told them they owe you, and your saved
    friends (with whether their email is on the app)."""
    pub = public_of(db)
    try:
        me = pub.run('SELECT email FROM app_users WHERE space = :s', s=db.space)
        email = me[0]['email'] if me else None
        people = {r['email'] for r in pub.run('SELECT email FROM app_users')}
        to_me = pub.run('SELECT * FROM shared_splits WHERE to_email = :e AND settled = 0 ORDER BY day DESC', e=email) if email else []
        from_me = pub.run('SELECT * FROM shared_splits WHERE from_space = :s AND settled = 0 ORDER BY day DESC', s=db.space)
    finally:
        pub.close()
    contacts = [{'name': r['name'], 'email': r['email'], 'app': r['email'] in people} for r in db.run('SELECT * FROM contacts ORDER BY name')]
    keep = lambda r: {k: r[k] for k in ('txn_id', 'from_email', 'to_email', 'amount_minor', 'currency', 'what', 'day')}
    return {'me': email, 'to_me': [keep(r) for r in to_me], 'from_me': [keep(r) for r in from_me], 'contacts': contacts}


def paid_back(db, from_email, currency):
    """You paid someone back what they said you owe (one currency): their side shows it settled too."""
    pub = public_of(db)
    try:
        me = pub.run('SELECT email FROM app_users WHERE space = :s', s=db.space)
        if not me:
            raise KeyError('no account')
        rows = pub.run('SELECT from_space, txn_id FROM shared_splits WHERE to_email = :e AND from_email = :f AND currency = :c AND settled = 0',
                       e=me[0]['email'], f=from_email.strip().lower(), c=currency)
        pub.run('UPDATE shared_splits SET settled = 1 WHERE to_email = :e AND from_email = :f AND currency = :c',
                e=me[0]['email'], f=from_email.strip().lower(), c=currency)
    finally:
        pub.close()
    by_space = defaultdict(list)
    for r in rows:
        by_space[r['from_space']].append(r['txn_id'])
    for space, ids in by_space.items():
        theirs = DB(db.url if db.pg else db.main_file, space)
        try:
            for i in ids:
                theirs.run('UPDATE txn_friends SET settled = 1 WHERE txn_id = :id', id=i)
        finally:
            theirs.close()
    return len(rows)


def plan(loans, extra, strategy=None):
    """Pay the loans off month by month: interest is added (yearly rate / 12), then each loan's EMI is paid.
    With a strategy, the extra per month and the EMIs of loans already paid off go to one loan at a time:
    'avalanche' = highest rate first (least interest), 'snowball' = smallest balance first (fastest wins).
    Without one ('as is'), each loan just pays its own EMI. Returns months to debt-free (None if never),
    total interest, the month each loan is paid off, and `curve`: what's still owed in total at the end of each
    month (whole units, the first 30 years) for the graphs. Amounts in minor units."""
    bal = {l['id']: l['balance_now'] for l in loans if l['balance_now'] > 0}
    rate = {l['id']: l['rate_bp'] / 120000 for l in loans}
    emi = {l['id']: l['emi_minor'] for l in loans}
    budget, done, interest, month, curve = sum(emi[i] for i in bal) + extra, {}, 0, 0, []
    while bal and month < PLAN_MONTHS:
        month += 1
        for i in bal:
            charge = round(bal[i] * rate[i])
            interest += charge
            bal[i] += charge
        left = budget
        for i in bal:  # every loan gets its EMI first
            paid = min(emi[i], bal[i])
            bal[i] -= paid
            left -= paid
        if strategy:
            key = (lambda i: (-rate[i], bal[i])) if strategy == 'avalanche' else (lambda i: (bal[i], -rate[i]))
            for i in sorted(bal, key=key):
                paid = min(max(left, 0), bal[i])
                bal[i] -= paid
                left -= paid
        for i in [i for i in bal if bal[i] <= 0]:
            done[i] = month
            del bal[i]
        if month <= 360:
            curve.append(round(sum(bal.values()) / 100))
    return {'months': None if bal else month, 'interest': interest, 'payoff': done, 'curve': curve}


DEBTS = ('loan', 'card')


def auto_emis(h, taken, today):
    """The EMIs an auto-debited debt has paid since its balance was entered: one on each due day up to today,
    except in months where an SMS or a payment you recorded already shows it (`taken`: those months)."""
    out, month = [], h['as_of'][:7]
    while month <= today[:7]:
        y, m = map(int, month.split('-'))
        day = f'{month}-{min(h["due_day"], [31, 29 if y % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]):02d}'
        if h['as_of'] < day <= today and month not in taken:
            out.append({'date': day, 'amount': h['emi_minor'] / 100, 'source': 'auto'})
        month = f'{y + m // 12:04d}-{m % 12 + 1:02d}'
    return out


def money(db, currency='INR', extra=0, every=None, rate=None, today=None, options=()):
    """Loans and credit cards (with today's balance after the SMS since it was entered) and savings, plus payoff
    plans for all of them in `currency` (the others converted at `rate`, INR per SAR): as they are, and with
    `extra` a month by each strategy. A card's balance goes up with each purchase SMS and down with each payment."""
    every = transactions(db) if every is None else every
    items, recorded = [], defaultdict(list)
    for r in db.run('SELECT * FROM loan_payments ORDER BY paid_on, id'):
        recorded[r['holding_id']].append(r)
    for h in db.run('SELECT * FROM holdings ORDER BY kind, id'):
        h = dict(h)
        h['balance_now'], h['payments'] = h['balance_minor'], []
        h['charges'] = []
        if h['kind'] in DEBTS:
            key, card = h['pattern'].strip().lower(), h['kind'] == 'card'
            seen = [t for t in every if key and t['currency'] == h['currency'] and t['received_at'][:10] > h['as_of']
                    and t['category'] not in NOT_COUNTED and key in t['body'].lower()]
            # A loan's matching SMS are EMIs going out. A card's are purchases (debits) and payments in (credits).
            sms = [{'id': t['id'], 'date': t['received_at'][:10], 'amount': t['amount'], 'source': 'sms'} for t in seen
                   if t['direction'] == ('credit' if card else 'debit')]
            if card:
                h['charges'] = [{'id': t['id'], 'date': t['received_at'][:10], 'amount': t['amount']} for t in seen if t['direction'] == 'debit']
                h['balance_now'] += round(sum(c['amount'] for c in h['charges']) * 100)
            sms_months = {p['date'][:7] for p in sms}
            mine = [{'pid': r['id'], 'date': r['paid_on'], 'amount': r['amount_minor'] / 100, 'source': r['note'] or 'manual'}
                    for r in recorded[h['id']] if r['paid_on'] >= h['as_of']  # recorded on the day you typed the balance: after it
                    and not (r['note'] == 'EMI' and r['paid_on'][:7] in sms_months)]  # the SMS turned up after all: count it once
            if h['auto'] and h['due_day'] and h['emi_minor']:
                today = today or datetime.now(ZoneInfo(os.environ.get('EXPENSE_TZ') or 'Asia/Riyadh')).date().isoformat()
                mine += auto_emis(h, sms_months | {p['date'][:7] for p in mine if p['source'] == 'EMI'}, today)
            interest_month = None
            for p in sorted(sms + mine, key=lambda p: p['date']):
                # One month's interest per month with a payment (yearly rate / 12). Not on a card: its interest
                # shows up when you type the statement balance in the check-in.
                if not card and p['date'][:7] != interest_month:
                    interest_month = p['date'][:7]
                    h['balance_now'] += round(h['balance_now'] * h['rate_bp'] / 120000)
                h['balance_now'] = max(0, h['balance_now'] - round(p['amount'] * 100))
                h['payments'].append(p)
        items.append(h)
    other = [h for h in items if h['kind'] in DEBTS and h['currency'] != currency]
    if other and not rate:
        rate = (inr_per_sar() or {}).get('rate') or 24.9
    factor = lambda h: 1 if h['currency'] == currency else rate if currency == 'INR' else 1 / rate
    loans = [{**h, 'balance_now': round(h['balance_now'] * factor(h)), 'emi_minor': round(h['emi_minor'] * factor(h))}
             for h in items if h['kind'] in DEBTS]
    as_is = plan(loans, 0)
    # What prepaying 1 lakh (or 1,000 SAR) on each loan today would save, for the suggestions.
    lump, prepay = 1_00000_00 if currency == 'INR' else 1000_00, {}
    for h in loans:
        if h['balance_now'] > 0:
            after = plan([{**l, 'balance_now': max(0, l['balance_now'] - lump)} if l is h else l for l in loans], 0)  # in `currency`
            prepay[h['id']] = {'interest': as_is['interest'] - after['interest'],
                               'months': (as_is['payoff'].get(h['id']) or PLAN_MONTHS) - (after['payoff'].get(h['id']) or PLAN_MONTHS)}
    # Ways to close the debt faster: each extra a month, by both strategies (for the Plan tab to choose from).
    choices = [{'extra': e / 100, 'avalanche': plan(loans, e, 'avalanche'), 'snowball': plan(loans, e, 'snowball')} for e in options]
    return {'holdings': items, 'currency': currency, 'rate': rate, 'extra': extra / 100, 'lump': lump / 100, 'prepay': prepay, 'options': choices,
            'plans': {'as_is': as_is, 'avalanche': plan(loans, extra, 'avalanche'), 'snowball': plan(loans, extra, 'snowball')}}


# Open exchange-rate services, no key needed. SAR is pegged at 3.75 to the dollar, so the dollar rate works too.
# Each gives (₹ per SAR, ₹ per USD).
RATE_SOURCES = [('open.er-api.com', 'https://open.er-api.com/v6/latest/SAR', lambda d: (d['rates']['INR'], d['rates']['INR'] / d['rates']['USD'])),
                ('frankfurter.dev', 'https://api.frankfurter.dev/v1/latest?base=USD&symbols=INR', lambda d: (d['rates']['INR'] / 3.75, d['rates']['INR']))]
RATE_TTL = 6 * 3600
# Gold, US dollars per troy ounce (no key needed). 31.1035 g to the ounce.
GOLD_SOURCE = ('gold-api.com', 'https://api.gold-api.com/price/XAU', lambda d: d['price'])
GRAMS_PER_OUNCE = 31.1035
KARATS = {24, 22, 21, 18, 14}
_rate = {}


def inr_per_sar(now=None):
    """Today's ₹ per SAR (rate) and ₹ per USD (usd) from the first service that answers, kept for 6 hours.
    None if none answers."""
    now = now or time.time()
    if _rate.get('value') and now - _rate['at'] < RATE_TTL:
        return _rate['value']
    if now - _rate.get('failed_at', 0) < 600:  # every service just failed: don't make each request wait 10 s again
        return None
    for name, url, pick in RATE_SOURCES:
        try:
            with urlopen(url, timeout=5) as r:
                sar, usd = (round(float(v), 4) for v in pick(json.load(r)))
            if 10 < sar < 100 and 30 < usd < 300:  # a wildly wrong number is worse than none
                _rate.update(at=now, value={'rate': sar, 'usd': usd, 'source': name, 'at': datetime.now(timezone.utc).isoformat(timespec='minutes')})
                try:  # gold is extra: the rates stand without it
                    with urlopen(GOLD_SOURCE[1], timeout=5) as g:
                        oz = float(GOLD_SOURCE[2](json.load(g)))
                    if 500 < oz < 20000:
                        _rate['value'].update(gold_usd_oz=round(oz, 2), gold_source=GOLD_SOURCE[0])
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                return _rate['value']
        except (OSError, ValueError, KeyError, TypeError):
            continue
    _rate['failed_at'] = now
    return None


# Credit-card due reminders: '…VISA Card is due on 25/11/2026 for SAR 8,622.00 total amount due SAR 13,845.84'.
_AMT = r'(SAR|SR|INR|Rs\.?|₹)\s*([\d,]+(?:\.\d+)?)'
CARD_TOTAL = re.compile(r'(?:total\s+(?:amount\s+)?due|total\s+outstanding|outstanding\s+balance|statement\s+balance)\s*(?:is|of|:)?\s*' + _AMT, re.I)
CARD_DUE = re.compile(r'(?:\bdue\b[^\n]*?\bfor\s*|minimum\s+(?:amount\s+)?(?:due|payment)\s*(?:is|of|:)?\s*)' + _AMT, re.I)
CARD_DUE_DAY = re.compile(r'\bdue\s+(?:on|by|date)?\s*:?\s*(\d{1,2})[/.-]\d{1,2}[/.-]\d{2,4}', re.I)
CARD_DIGITS = re.compile(r'(?:card|ending|\*)\D{0,12}?(\d{4})\b', re.I)
CARD_BRAND = re.compile(r'\b(visa|master\s?card|mada|amex|american\s+express)\b', re.I)


# Pay-later (Tamara, Tabby): 'Split in 6 payment confirmation: Store: Petromin Nissan Order: 6,991.92 SAR'.
PAY_LATER = re.compile(r'\b(?:split|pay)\s+in\s+(\d{1,2})\b|\b(\d{1,2})\s+(?:interest-free\s+)?(?:payments|instal+ments)\b', re.I)
PAY_LATER_TOTAL = re.compile(r'(?:order|total|amount|purchase)[^\n\d]{0,12}?(?:' + _AMT + r'|([\d,]+(?:\.\d+)?)\s*(SAR|SR|ريال))', re.I)
PAY_LATER_STORE = re.compile(r'(?:store|merchant)\s*:\s*([^\n]{2,40})', re.I)


def pay_later(db, body, received_at):
    """A pay-later confirmation goes on the Debt tab as a 0% plan: the first part is paid at checkout, the rest one a
    month on the purchase day (taken automatically, like an EMI). Not stored as spending: the card charges that pay
    each part already are. Returns the plan's name, or None."""
    parts, total, store = PAY_LATER.search(body), PAY_LATER_TOTAL.search(body), PAY_LATER_STORE.search(body)
    n = parts and int(parts.group(1) or parts.group(2))
    if not (total and n and 2 <= n <= 24):
        return None
    cur, amount = (total.group(1), total.group(2)) if total.group(1) else (total.group(4), total.group(3))
    owed = round(float(amount.replace(',', '')) * 100)
    part, when = round(owed / n), received_at[:10]
    name = f"Pay later · {' '.join(store.group(1).split()) if store else 'purchase'}"
    if not any(h['name'] == name and h['as_of'] == when for h in db.run("SELECT name, as_of FROM holdings WHERE kind = 'loan'")):  # phone resends
        save_holding(db, {'id': None, 'kind': 'loan', 'auto': 1, 'name': name[:60], 'currency': 'INR' if cur.upper().startswith(('INR', 'RS', '₹')) else 'SAR',
                          'balance_minor': owed - part, 'rate_bp': 0, 'emi_minor': part, 'due_day': min(28, int(when[8:])), 'pattern': '', 'as_of': when})
    return name


def card_statement(db, body, received_at):
    """A card's due reminder puts the card on the Debt page, or updates what's owed, the payment due and the due
    day from it (a reminder older than the card's last update is ignored). Returns the card's name, or None."""
    total = CARD_TOTAL.search(body) if re.search(r'\bcard\b', body, re.I) else None
    if not total:
        return pay_later(db, body, received_at)
    minor = lambda m: round(float(m.group(2).replace(',', '')) * 100)
    currency = 'INR' if total.group(1).upper().startswith(('INR', 'RS', '₹')) else 'SAR'
    due, day, digits, brand = CARD_DUE.search(body), CARD_DUE_DAY.search(body), CARD_DIGITS.search(body), CARD_BRAND.search(body)
    digits = digits and digits.group(1)
    name = f"{brand.group(1).title() if brand else 'Credit'} card" + (f' ••{digits}' if digits else '')
    when = received_at[:10]
    cards = [c for c in db.run("SELECT * FROM holdings WHERE kind = 'card'") if c['currency'] == currency]
    card = (next((c for c in cards if digits and c['pattern'] == digits), None) or next((c for c in cards if c['name'] == name), None)
            or (cards[0] if len(cards) == 1 and not digits else None))  # one card in that currency: the reminder is about it
    owed, pay = minor(total), minor(due) if due else minor(total)
    if card:
        if when >= card['as_of']:
            db.run('UPDATE holdings SET balance_minor = :b, emi_minor = :e, due_day = :d, as_of = :a WHERE id = :id',
                   b=owed, e=pay, d=int(day.group(1)) if day else card['due_day'], a=when, id=card['id'])
        return card['name']
    save_holding(db, {'id': None, 'kind': 'card', 'auto': 0, 'name': name, 'currency': currency, 'balance_minor': owed, 'rate_bp': 0,
                      'emi_minor': pay, 'due_day': int(day.group(1)) if day else 0, 'pattern': digits or '', 'as_of': when})
    return name


def snapshot(db, m, month=None):
    """Keep this month's totals (INR) for the history graphs; later looks in the same month replace it."""
    usd = m.get('usd') or (m['rate'] or 24.9) * 3.75
    def inr(h, v):
        if h['currency'] == 'GOLD':  # v = grams × 100; value = grams × purity × ₹ a gram of 24K
            return round(v * h['rate_bp'] / 2400 * (m.get('gold_inr_g') or 0))
        return v if h['currency'] == 'INR' else round(v * usd) if h['currency'] == 'USD' else round(v * (m['rate'] or 24.9))
    debt = sum(inr(h, h['balance_now']) for h in m['holdings'] if h['kind'] in DEBTS)
    saved = sum(inr(h, h['balance_now']) for h in m['holdings'] if h['kind'] == 'saving')
    month = month or datetime.now(ZoneInfo(os.environ.get('EXPENSE_TZ') or 'Asia/Riyadh')).strftime('%Y-%m')
    db.run('INSERT INTO snapshots (month, debt_minor, savings_minor) VALUES (:m, :d, :s) '
           'ON CONFLICT (month) DO UPDATE SET debt_minor = excluded.debt_minor, savings_minor = excluded.savings_minor', m=month, d=debt, s=saved)


def record_payment(db, holding_id, paid_on, amount_minor, note):
    if not db.run("SELECT id FROM holdings WHERE id = :id AND kind IN ('loan', 'card')", id=holding_id):
        raise KeyError('no such loan')
    db.run('INSERT INTO loan_payments (holding_id, paid_on, amount_minor, note) VALUES (:h, :d, :a, :n)',
           h=holding_id, d=paid_on, a=amount_minor, n=note)


def plan_month(db, month):
    """This month's check-in actions and last month's, for the review."""
    y, m = map(int, month.split('-'))
    previous = f'{y - (m == 1):04d}-{(m - 2) % 12 + 1:02d}'
    rows = lambda mo: db.run('SELECT * FROM plan_actions WHERE month = :m ORDER BY at', m=mo)
    return {'month': month, 'actions': rows(month), 'previous_month': previous, 'previous': rows(previous)}


def save_action(db, a):
    db.run('INSERT INTO plan_actions (month, kind, ref, target_minor, actual_minor, status, at) '
           'VALUES (:month, :kind, :ref, :target_minor, :actual_minor, :status, :at) '
           'ON CONFLICT (month, kind, ref) DO UPDATE SET target_minor = excluded.target_minor, '
           'actual_minor = excluded.actual_minor, status = excluded.status, at = excluded.at', **a)


def save_holding(db, h):
    h = {'limit_minor': 0, **h}
    if h.get('id'):
        if not db.run('SELECT id FROM holdings WHERE id = :id', id=h['id']):
            raise KeyError('no such loan or saving')
        db.run('UPDATE holdings SET kind = :kind, name = :name, currency = :currency, balance_minor = :balance_minor, rate_bp = :rate_bp, '
               'emi_minor = :emi_minor, due_day = :due_day, pattern = :pattern, as_of = :as_of, auto = :auto, limit_minor = :limit_minor WHERE id = :id', **h)
    else:
        db.run('INSERT INTO holdings (kind, name, currency, balance_minor, rate_bp, emi_minor, due_day, pattern, as_of, auto, limit_minor) '
               'VALUES (:kind, :name, :currency, :balance_minor, :rate_bp, :emi_minor, :due_day, :pattern, :as_of, :auto, :limit_minor)',
               **{k: v for k, v in h.items() if k != 'id'})


def mine(t):
    """Your part of a transaction in minor units: the amount less a friend's share."""
    return round((t['amount'] - t['theirs']) * 100)


# Neon (AWS) region -> the Vercel region in the same city, where the server should run.
VERCEL_REGION_FOR = {'us-east-1': 'iad1', 'us-east-2': 'cle1', 'us-west-2': 'pdx1', 'eu-central-1': 'fra1',
                     'eu-west-2': 'lhr1', 'ap-southeast-1': 'sin1', 'ap-southeast-2': 'syd1', 'sa-east-1': 'gru1'}


def health(db, database_ms):
    """Where the server and the database run, and how long a database round trip took. No secrets."""
    host = urlparse(db.url).hostname or '' if db.pg else ''
    db_region = next((part for part in host.split('.') if part in VERCEL_REGION_FOR), 'local' if not db.pg else 'unknown')
    server_region = os.environ.get('VERCEL_REGION', 'local')
    best = VERCEL_REGION_FOR.get(db_region)
    return {
        'version': VERSION, 'server_region': server_region, 'database_region': db_region, 'database_ms': database_ms,
        'reused_connection': db.reused,
        'advice': f'Run the server in {best} (vercel.json "regions") to sit next to the database.'
                  if best and best != server_region else 'Server and database are in the same place.',
    }


SEARCH_LIMIT = 200


def search(db, q, currency=None):
    """Transactions from every month whose SMS, merchant, category or card contain all the words in q
    (case-insensitive, Arabic or Western digits), newest first."""
    words = q.lower().translate(ARABIC_DIGITS).split()
    if not words:
        return {'results': [], 'count': 0, 'spent': 0}
    hits = [t for t in transactions(db, currency=currency)
            if all(w in ' '.join((t['body'], t['merchant'], t['category'], t['account'])).lower().translate(ARABIC_DIGITS)
                   for w in words)]
    spent = sum(mine(t) for t in hits if t['direction'] == 'debit' and t['category'] not in NOT_COUNTED) / 100
    return {'results': hits[:SEARCH_LIMIT], 'count': len(hits), 'spent': round(spent, 2)}


def summary(db, month=None, currency=None, every=None):
    every = transactions(db) if every is None else every
    currencies = sorted({t['currency'] for t in every})
    # INR and SAR are never added together; SAR is the main account, so it is shown first.
    currency = currency or ('SAR' if 'SAR' in currencies or not every else every[0]['currency'])
    rows = [t for t in every if t['currency'] == currency]

    months = defaultdict(lambda: {'spent': 0, 'received': 0})
    for t in rows:
        totals = months[t['received_at'][:7]]  # the month is listed even if everything in it is ignored
        if t['category'] not in NOT_COUNTED:
            totals['spent' if t['direction'] == 'debit' else 'received'] += mine(t)
    month = month or max(months, default=datetime.now(timezone.utc).strftime('%Y-%m'))

    by_category, by_merchant, by_account = defaultdict(int), defaultdict(int), defaultdict(int)
    for t in rows:
        if t['received_at'].startswith(month) and t['direction'] == 'debit' and t['category'] not in NOT_COUNTED:
            minor = mine(t)
            if not minor:  # bought entirely for a friend
                continue
            by_category[t['category']] += minor
            by_merchant[t['merchant'] or '(unknown)'] += minor
            by_account[t['account']] += minor

    major = lambda d: {k: v / 100 for k, v in sorted(d.items(), key=lambda kv: -kv[1])}
    return {
        'currency': currency,
        'currencies': currencies,
        'month': month,
        'months': [{'month': m, 'spent': v['spent'] / 100, 'received': v['received'] / 100} for m, v in sorted(months.items())],
        'by_category': major(by_category),
        'top_merchants': dict(list(major(by_merchant).items())[:10]),
        'by_account': major(by_account),
        # Choices for the category editor: the built-in ones plus any you invented.
        'categories': sorted({*json.loads(RULES.read_text(encoding='utf-8')), 'Income', *(t['category'] for t in every)} - {'Uncategorized', *NOT_COUNTED}),
        # The month's transactions too (ignored ones included, so they can be un-ignored), in one request.
        'transactions': [t for t in rows if t['received_at'].startswith(month)],
        # Every month's uncategorized ones, newest first, for the "Needs a category" list.
        'to_review': [t for t in rows if t['category'] == 'Uncategorized'],
        'version': VERSION,
        'deliveries': db.run('SELECT d.id, d.at, d.sender, d.outcome, s.body FROM phone_deliveries d '
                             'LEFT JOIN skipped_sms s ON s.delivery_id = d.id ORDER BY d.id DESC LIMIT 20'),
    }


def refreshed(db, query, every=None):
    """The updated dashboard after a change, plus every transaction in its currency, so the page
    recomputes all its analytics from one reply."""
    every = transactions(db) if every is None else every
    result = summary(db, query.get('month'), query.get('currency'), every)
    return {'summary': result, 'all': [t for t in every if t['currency'] == result['currency']]}


def _token():
    token = os.environ.get('EXPENSE_TOKEN', '').strip()  # a space pasted into Vercel's value box shouldn't lock you out
    return token if len(token) >= MIN_PASSWORD else None


def _sign(expires, space='public', pw_hash=''):
    # The owner's (public) cookie keeps its old form; a person's also signs their password hash, so a reset logs them out.
    what = f'session:{expires}' if space == 'public' and not pw_hash else f'session:{space}:{expires}:{pw_hash[-24:]}'
    return hmac.new(_token().encode(), what.encode(), hashlib.sha256).hexdigest()


def new_session(space='public', pw_hash=''):
    """Cookie value '<expiry>.<signature>' (the owner) or '<space>.<expiry>.<signature>' (a person): stateless, so it
    works across Vercel instances; changing EXPENSE_TOKEN logs everyone out."""
    expires = int(time.time()) + SESSION_DAYS * 86400
    return f'{expires}.{_sign(expires)}' if space == 'public' and not pw_hash else f'{space}.{expires}.{_sign(expires, space, pw_hash)}'


PBKDF2_ROUNDS = 200_000


def hash_password(password, salt=None):
    salt = salt or os.urandom(16)
    return f'pbkdf2${PBKDF2_ROUNDS}${salt.hex()}${hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS).hex()}'


def password_matches(password, stored):
    try:
        _, rounds, salt, digest = stored.split('$')
        return hmac.compare_digest(hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds)).hex(), digest)
    except ValueError:
        return False


def client_ip(environ):
    # Vercel overwrites X-Forwarded-For with the real client address, so it can't be spoofed there.
    return (environ.get('HTTP_X_FORWARDED_FOR') or environ.get('REMOTE_ADDR') or '?').split(',')[0].strip()


def sign_in(environ, given, email='', key=False, admin=False):
    """('ok', space, pw_hash) or ('wrong' | 'locked', None, ''). With an email: that person's password. Without: the site
    password (EXPENSE_TOKEN: the owner's data), or with key=True a person's phone sync key, or with admin=True the
    ADMIN_PASSWORD. Every wrong guess is recorded per IP (in the database, because Vercel instances share no memory)
    and MAX_FAILURES of them lock that IP out."""
    db = DB()
    try:
        ip, now, given = client_ip(environ), int(time.time()), given.strip()  # strip: phone keyboards add trailing spaces
        recent = db.run('SELECT COUNT(*) AS n FROM login_failures WHERE ip = :ip AND at > :since',
                        ip=ip, since=now - LOCKOUT_SECONDS)[0]['n']
        if recent >= MAX_FAILURES:
            return 'locked', None, ''
        if admin:
            if _admin_password() and hmac.compare_digest(given.encode(), _admin_password().encode()):
                return 'ok', None, ''
        elif email:
            who = email.strip().lower()  # an email or a username: usernames never contain '@'
            found = db.run('SELECT * FROM app_users WHERE email = :e OR username = :e', e=who)
            if found and found[0]['space'] and password_matches(given, found[0]['pw_hash']):
                db.run('UPDATE app_users SET last_seen = :t WHERE id = :id', t=datetime.now(timezone.utc).isoformat(timespec='seconds'), id=found[0]['id'])
                return 'ok', found[0]['space'], found[0]['pw_hash']
        elif hmac.compare_digest(given.encode(), _token().encode()):
            return 'ok', 'public', ''
        elif key and len(given) >= 20:
            found = db.run("SELECT space FROM app_users WHERE sync_key = :k AND space != ''", k=given)
            if found:
                return 'ok', found[0]['space'], ''
        db.run('DELETE FROM login_failures WHERE at <= :since', since=now - LOCKOUT_SECONDS)
        db.run('INSERT INTO login_failures (ip, at) VALUES (:ip, :at)', ip=ip, at=now)
        return 'wrong', None, ''
    finally:
        db.close()


def check_password(environ, given):
    """'ok', 'wrong' or 'locked' for the site password."""
    return sign_in(environ, given)[0]


def bearer_space(environ):
    """The phone authenticates with 'Authorization: Bearer <EXPENSE_TOKEN or a person's sync key>': whose data, or None.
    Guesses here count toward the lockout too."""
    header = environ.get('HTTP_AUTHORIZATION', '')
    if not (_token() and header.startswith('Bearer ')):
        return None
    result, space, _ = sign_in(environ, header[7:], key=True)
    return space if result == 'ok' else None


def bearer_ok(environ):
    return bearer_space(environ) is not None


def session_space(environ):
    """Whose data the browser's login cookie opens, or None. A person's cookie is checked against their current
    password hash, so a deleted person, or one whose password was reset, is signed out."""
    cookie = SimpleCookie(environ.get('HTTP_COOKIE', '')).get('session')
    parts = (cookie.value if cookie else '').split('.')
    if not _token() or len(parts) not in (2, 3):
        return None
    space, expires, sig = parts if len(parts) == 3 else ('public', *parts)
    if not (SPACE.match(space) and expires.isdigit() and int(expires) > time.time()):
        return None
    if len(parts) == 2:
        return 'public' if hmac.compare_digest(sig, _sign(expires)) else None
    db = DB()
    try:
        found = db.run('SELECT pw_hash FROM app_users WHERE space = :s', s=space)
    finally:
        db.close()
    return space if found and hmac.compare_digest(sig, _sign(expires, space, found[0]['pw_hash'])) else None


def session_ok(environ):
    return session_space(environ) is not None


# ---------- the admin ----------
ADMIN_HOURS = 12


def _admin_password():
    pw = os.environ.get('ADMIN_PASSWORD', '').strip()
    return pw if len(pw) >= MIN_PASSWORD else None


def _admin_sign(expires):
    # signs the admin password's hash too: changing ADMIN_PASSWORD ends admin sessions
    return hmac.new(_token().encode(), f'admin:{expires}:{hashlib.sha256(_admin_password().encode()).hexdigest()[:16]}'.encode(), hashlib.sha256).hexdigest()


def admin_ok(environ):
    cookie = SimpleCookie(environ.get('HTTP_COOKIE', '')).get('admin')
    expires, _, sig = (cookie.value if cookie else '').partition('.')
    return bool(_token() and _admin_password()) and expires.isdigit() and int(expires) > time.time() and hmac.compare_digest(sig, _admin_sign(expires))


def new_user(db, email, password, owner=False):
    """Add a person (db: the public space). owner=True links the data from before accounts existed to them."""
    email = email.strip().lower()
    if not EMAIL.match(email):
        raise ValueError('enter a valid email')
    if len(password) < MIN_USER_PASSWORD:
        raise ValueError(f'the password needs at least {MIN_USER_PASSWORD} characters')
    if db.run('SELECT id FROM app_users WHERE email = :e', e=email):
        raise ValueError('that email already has an account')
    if owner and db.run("SELECT id FROM app_users WHERE space = 'public'"):
        raise ValueError('the existing data is already linked to an account')
    uid = db.run("INSERT INTO app_users (email, pw_hash, sync_key, created_at) VALUES (:e, :h, :k, :t) RETURNING id", e=email, h=hash_password(password),
                 k=secrets.token_urlsafe(24), t=datetime.now(timezone.utc).isoformat(timespec='seconds'))[0]['id']
    db.run('UPDATE app_users SET space = :s WHERE id = :id', s='public' if owner else f'u_{uid}', id=uid)
    return uid


def drop_space(db, space):
    """Delete a person's data for good (never the owner's)."""
    if space == 'public' or not SPACE.match(space):
        raise ValueError('the owner’s data can’t be dropped')
    if db.pg:
        db.run(f'DROP SCHEMA IF EXISTS {space} CASCADE')
        DB._schema_ready.discard((db.url, space))
    else:
        path = Path(db.main_file).with_name(f'{Path(db.main_file).stem}-{space}{Path(db.main_file).suffix or ".db"}')
        DB._schema_ready.discard((str(path), space))
        path.unlink(missing_ok=True)


def users(db):
    out = []
    for u in db.run('SELECT id, email, username, space, created_at, last_seen FROM app_users ORDER BY id'):
        out.append(dict(u))
    linked = {u['space'] for u in out}
    return {'users': out, 'owner_linked': 'public' in linked, 'spaces': ['public'] + [u['space'] for u in out if u['space'] != 'public']}


# Tables the admin can browse, with the columns that identify a row (to delete it).
ADMIN_TABLES = {'transactions': ['id'], 'holdings': ['id'], 'loan_payments': ['id'], 'statements': ['id'], 'accounts': ['name'],
                'merchant_categories': ['merchant'], 'txn_categories': ['txn_id'], 'txn_friends': ['txn_id'], 'txn_links': ['debit_id'],
                'plan_actions': ['month', 'kind', 'ref'], 'settings': ['key'], 'snapshots': ['month'], 'deleted_sms': ['body'],
                'phone_deliveries': ['id'], 'skipped_sms': ['delivery_id']}
ADMIN_PAGE_ROWS = 100


def admin_tables(db):
    return [{'table': t, 'rows': db.run(f'SELECT COUNT(*) AS n FROM {t}')[0]['n'], 'key': k} for t, k in ADMIN_TABLES.items()]


def admin_rows(db, table, offset=0):
    if table not in ADMIN_TABLES:
        raise KeyError('no such table')
    order = ', '.join(f'{c} DESC' for c in ADMIN_TABLES[table])
    rows = db.run(f'SELECT * FROM {table} ORDER BY {order} LIMIT {ADMIN_PAGE_ROWS} OFFSET :o', o=max(0, int(offset)))
    return {'table': table, 'key': ADMIN_TABLES[table], 'rows': rows, 'offset': offset}


def table_columns(db, table):
    return ({r['column_name'] for r in db.run("SELECT column_name FROM information_schema.columns WHERE table_name = :t AND table_schema = current_schema()", t=table)}
            if db.pg else {r['name'] for r in db.run(f'PRAGMA table_info({table})')})


def export_data(db):
    """All of this person's data, table by table: to move it to another site (or keep a copy)."""
    return {'expense_export': 1, 'tables': {t: db.run(f'SELECT * FROM {t}') for t in ADMIN_TABLES}}


def import_rows(db, table, rows, first=False):
    """Add exported rows into this space (ids kept, so categories, splits and links still point at the right rows).
    Only into an empty space: the first chunk refuses one that already has transactions."""
    if table not in ADMIN_TABLES or not isinstance(rows, list):
        raise ValueError('unknown table')
    if first and db.run('SELECT COUNT(*) AS n FROM transactions')[0]['n']:
        raise ValueError('this person already has transactions: import only into an empty account')
    cols = table_columns(db, table)
    added = 0
    for r in rows:
        if not isinstance(r, dict) or not r or not set(r) <= cols:
            raise ValueError(f'a {table} row has columns this site doesn’t know')
        names = sorted(r)
        added += len(db.run(f'INSERT INTO {table} ({", ".join(names)}) VALUES ({", ".join(":" + n for n in names)}) ON CONFLICT DO NOTHING RETURNING 1', **r))
    if db.pg and 'id' in cols and rows:  # explicit ids: move the counter past them, or the next new row would clash
        db.run(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), (SELECT MAX(id) FROM {table}))")
    return added


def admin_delete_row(db, table, key):
    if table not in ADMIN_TABLES or set(key) != set(ADMIN_TABLES[table]):
        raise KeyError('no such table or key')
    where = ' AND '.join(f'{c} = :{c}' for c in ADMIN_TABLES[table])
    if table == 'transactions':  # with what hangs off it, as deleting from the app does (but without remembering it as deleted)
        for t, c in (('txn_categories', 'txn_id'), ('txn_friends', 'txn_id')):
            db.run(f'DELETE FROM {t} WHERE {c} = :id', id=key['id'])
        db.run('DELETE FROM txn_links WHERE debit_id = :id OR credit_id = :id', id=key['id'])
    db.run(f'DELETE FROM {table} WHERE {where}', **key)


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
    if len(sender) > 40 or '\n' in sender or sender == body:  # an iPhone Shortcut sending the message as its sender
        sender = 'iPhone'
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


def read_friend(environ):
    """{"id": 12, "friend": "Ali", "theirs": "50.00"} from a transaction; an empty friend removes the tag."""
    msg = read_json(environ, MAX_BODY)
    friend = ' '.join(str(msg.get('friend') or '').split())
    if len(friend) > MAX_FRIEND:
        raise ValueError(f'a name is at most {MAX_FRIEND} characters')
    theirs = int(Decimal(str(msg.get('theirs') or 0).replace(',', '').strip()) * 100)
    email = str(msg.get('email') or '').strip().lower()
    if email and not EMAIL.match(email):
        raise ValueError('enter a valid email')
    return int(msg['id']), friend, theirs, email


def read_payment(environ):
    """{"holding_id": 3, "amount": "57500", "paid_on": "2026-12-05", "note": "EMI" | "Extra"}"""
    msg = read_json(environ, MAX_BODY)
    amount = int(Decimal(str(msg['amount']).replace(',', '').strip()) * 100)
    paid_on = str(msg['paid_on'])
    datetime.strptime(paid_on, '%Y-%m-%d')
    note = str(msg.get('note') or '')
    if not 0 < amount < 10 ** 13 or note not in ('EMI', 'Extra', ''):
        raise ValueError('amount must be more than 0; note EMI or Extra')
    return int(msg['holding_id']), paid_on, amount, note


def read_action(environ):
    """{"month": "2026-12", "kind": "extra", "ref": "3", "target": "20000", "actual": "0", "status": "planned"}"""
    msg = read_json(environ, MAX_BODY)
    minor = lambda v: int(Decimal(str(v or 0).replace(',', '').strip()) * 100)
    a = {'month': str(msg['month']), 'kind': msg['kind'], 'ref': str(msg.get('ref') or '')[:60], 'target_minor': minor(msg.get('target')),
         'actual_minor': minor(msg.get('actual')), 'status': msg['status'], 'at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    datetime.strptime(a['month'], '%Y-%m')
    if a['kind'] not in PLAN_KINDS or a['status'] not in PLAN_STATUS or not 0 <= a['target_minor'] < 10 ** 13 or not 0 <= a['actual_minor'] < 10 ** 13:
        raise ValueError('unknown step or status, or a bad amount')
    return a


def read_holding(environ):
    """A loan or saving from the Money page: {kind, name, currency, balance, rate, emi, due_day, match, as_of, id?}."""
    msg = read_json(environ, MAX_BODY)
    minor = lambda v: int(Decimal(str(v or 0).replace(',', '').strip()) * 100)
    h = {'id': int(msg['id']) if msg.get('id') else None, 'kind': msg['kind'], 'name': ' '.join(str(msg['name']).split()),
         'currency': msg['currency'], 'balance_minor': minor(msg.get('balance')), 'rate_bp': minor(msg.get('rate')),
         'emi_minor': minor(msg.get('emi')), 'due_day': int(msg.get('due_day') or 0), 'pattern': ' '.join(str(msg.get('match') or '').split()),
         'as_of': str(msg.get('as_of') or datetime.now(timezone.utc).date().isoformat()),
         'auto': 1 if msg.get('auto') in (True, 1, '1', 'on', 'true') else 0, 'limit_minor': minor(msg.get('limit'))}
    datetime.strptime(h['as_of'], '%Y-%m-%d')
    if h['currency'] in ('USD', 'GOLD') and h['kind'] != 'saving':
        raise ValueError('USD and gold are for savings and investments')
    # Gold is kept in grams (balance) and karats (in the rate column, which savings don't otherwise use).
    if h['currency'] == 'GOLD' and h['rate_bp'] // 100 not in KARATS:
        raise ValueError(f'purity must be one of {sorted(KARATS)} karat')
    if (h['kind'] not in ('loan', 'card', 'saving') or h['currency'] not in ('SAR', 'INR', 'USD', 'GOLD') or not 0 < len(h['name']) <= 40
            or not 0 <= h['balance_minor'] < 10 ** 13 or not 0 <= h['rate_bp'] <= 6000 or not 0 <= h['emi_minor'] < 10 ** 11
            or not 0 <= h['due_day'] <= 31 or len(h['pattern']) > 40 or not 0 <= h['limit_minor'] < 10 ** 13):
        raise ValueError('check the name (1-40 letters), amounts, rate (0-60 %) and due day (1-31)')
    return h


def read_import(environ):
    """A batch from the import page: JSON {"messages": [{sender, body, received_at}, ...]}."""
    messages = read_json(environ, MAX_IMPORT_BODY)['messages']
    if not isinstance(messages, list) or len(messages) > MAX_IMPORT_BATCH:
        raise ValueError(f'messages must be a list of at most {MAX_IMPORT_BATCH}')
    return [clean_message(m) for m in messages]


def _admin(environ, method, path, query, respond, redirect):
    """The admin page and its API. Off unless ADMIN_PASSWORD is set; the admin signs in with it alone."""
    if path == '/admin' and method == 'GET':
        return respond('200 OK', (HERE / 'admin.html').read_bytes(), 'text/html; charset=utf-8')
    if (method, path) == ('POST', '/admin/login'):
        if not (_token() and _admin_password()):
            return redirect('/admin?error=off')
        result = sign_in(environ, read_form(environ).get('password', ''), admin=True)[0]
        if result != 'ok':
            return redirect(f'/admin?error={"locked" if result == "locked" else "1"}')
        expires = int(time.time()) + ADMIN_HOURS * 3600
        return redirect('/admin', admin=f'{expires}.{_admin_sign(expires)}')
    if (method, path) == ('POST', '/admin/logout'):
        return redirect('/admin', admin='')
    if not admin_ok(environ):
        return respond('401 Unauthorized', {'error': 'admin sign-in needed' if _admin_password() else 'the admin page is off: set ADMIN_PASSWORD in Vercel'})
    body = read_json(environ, MAX_IMPORT_BODY if path == '/api/admin/import' else MAX_BODY) if method == 'POST' else {}
    space = body.get('space') or query.get('space') or 'public'
    if not SPACE.match(str(space)):
        return respond('400 Bad Request', {'error': 'bad space'})
    pub = DB()
    try:
        if (method, path) == ('GET', '/api/admin/users'):
            return respond('200 OK', users(pub))
        if (method, path) == ('POST', '/api/admin/users'):
            try:
                new_user(pub, str(body.get('email', '')), str(body.get('password', '')), bool(body.get('owner')))
            except ValueError as e:
                return respond('400 Bad Request', {'error': str(e)})
            relink(pub, str(body.get('email', '')).strip().lower())
            return respond('200 OK', users(pub))
        found = pub.run('SELECT * FROM app_users WHERE id = :id', id=int(body.get('id') or 0)) if 'id' in body else []
        if (method, path) == ('POST', '/api/admin/users/password'):
            if not found or len(str(body.get('password', ''))) < MIN_USER_PASSWORD:
                return respond('400 Bad Request', {'error': f'pick a person and a password of {MIN_USER_PASSWORD}+ characters'})
            pub.run('UPDATE app_users SET pw_hash = :h WHERE id = :id', h=hash_password(str(body['password'])), id=found[0]['id'])
            return respond('200 OK', users(pub))
        if (method, path) == ('POST', '/api/admin/users/key'):  # a new phone sync key (the old one stops working)
            if not found:
                return respond('404 Not Found', {'error': 'no such person'})
            pub.run('UPDATE app_users SET sync_key = :k WHERE id = :id', k=secrets.token_urlsafe(24), id=found[0]['id'])
            return respond('200 OK', users(pub))
        if (method, path) == ('POST', '/api/admin/users/delete'):
            if not found:
                return respond('404 Not Found', {'error': 'no such person'})
            pub.run('DELETE FROM app_users WHERE id = :id', id=found[0]['id'])
            pub.run('DELETE FROM shared_splits WHERE to_email = :e OR from_space = :s', e=found[0]['email'], s=found[0]['space'])
            if body.get('data') and found[0]['space'] != 'public':
                drop_space(pub, found[0]['space'])
            return respond('200 OK', users(pub))
        if (method, path) == ('POST', '/api/admin/open'):  # use the app as this person (to add or delete anything there)
            known = space == 'public' or pub.run('SELECT pw_hash FROM app_users WHERE space = :s', s=space)
            if not known:
                return respond('404 Not Found', {'error': 'no such person'})
            cookie = new_session() if space == 'public' else new_session(space, known[0]['pw_hash'])
            return respond('200 OK', {'open': '/'}, headers=[('Set-Cookie', f'session={cookie}; Max-Age={SESSION_DAYS * 86400}; Path=/; HttpOnly; Secure; SameSite=Lax')])
        spaces = users(pub)['spaces']
    finally:
        pub.close()
    if space not in spaces:  # never create an empty space for someone who isn't there
        return respond('404 Not Found', {'error': 'no such person'})
    db = DB(space=space)
    try:
        if (method, path) == ('GET', '/api/admin/tables'):
            return respond('200 OK', admin_tables(db))
        if (method, path) == ('GET', '/api/admin/rows'):
            try:
                return respond('200 OK', admin_rows(db, query.get('table', ''), int(query.get('offset') or 0)))
            except (KeyError, ValueError) as e:
                return respond('400 Bad Request', {'error': str(e)})
        if (method, path) == ('POST', '/api/admin/import'):
            try:
                return respond('200 OK', {'added': import_rows(db, str(body.get('table')), body.get('rows'), bool(body.get('first')))})
            except (ValueError, KeyError, TypeError) as e:
                return respond('400 Bad Request', {'error': str(e)})
        if (method, path) == ('POST', '/api/admin/rows/delete'):
            try:
                admin_delete_row(db, str(body.get('table')), dict(body.get('key') or {}))
            except (KeyError, ValueError, TypeError) as e:
                return respond('400 Bad Request', {'error': str(e)})
            return respond('200 OK', {'deleted': True})
    finally:
        db.close()
    return respond('404 Not Found', {'error': 'not found'})


# ---------- MCP: Claude, ChatGPT and other assistants read your money (and, when asked, add or fix a transaction) ----------
# Streamable HTTP, stateless: each POST carries one JSON-RPC message (or a list) and gets JSON back; no event stream.
# Who: the personal key in the address (/mcp/<key>, for connectors that only take a URL) or 'Authorization: Bearer <key>'
# (a person's sync key, or the site password for the original data). Wrong keys count toward the login lockout.
MCP_VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')
MCP_MAX_BODY = 64 * 1024
MCP_LIST_MAX = 200


def _brief(t):
    out = {'id': t['id'], 'date': t['received_at'][:10], 'amount': t['amount'], 'currency': t['currency'],
           'direction': 'spent' if t['direction'] == 'debit' else 'received', 'merchant': t['merchant'], 'category': t['category'], 'account': t['account']}
    if t.get('friend'):
        out.update(friend=t['friend'], friend_share=t['theirs'], settled=t['settled'])
    return out


def _month_arg(a):
    m = str(a.get('month') or '').strip()
    if m and not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', m):
        raise ValueError('month must look like 2026-10')
    return m or None


def _cur_arg(a, default=None):
    c = str(a.get('currency') or '').strip().upper() or default
    if c and c not in ('SAR', 'INR', 'USD'):
        raise ValueError('currency must be SAR, INR or USD')
    return c


def mcp_overview(db, a):
    S = summary(db, _month_arg(a), _cur_arg(a))
    return {k: S[k] for k in ('currency', 'currencies', 'month', 'by_category', 'top_merchants', 'by_account')} | {
        'last_12_months': S['months'][-12:], 'needing_a_category': len(S['to_review']), 'categories': S['categories']}


def mcp_transactions(db, a):
    month, cur, limit = _month_arg(a), _cur_arg(a), max(1, min(MCP_LIST_MAX, int(a.get('limit') or 50)))
    cat, mer, way = str(a.get('category') or '').lower(), str(a.get('merchant') or '').lower(), str(a.get('direction') or '')
    rows = [t for t in transactions(db, month, cur) if (not cat or t['category'].lower() == cat) and (not mer or mer in t['merchant'].lower())
            and (way not in ('spent', 'received') or (t['direction'] == 'debit') == (way == 'spent'))]
    return {'count': len(rows), 'transactions': [_brief(t) for t in rows[:limit]]}


def mcp_search(db, a):
    q = str(a.get('query') or '').strip()
    if not q:
        raise ValueError('query is required')
    found = search(db, q, _cur_arg(a))
    return {'count': found['count'], 'spent': found['spent'], 'transactions': [_brief(t) for t in found['results']]}


def mcp_debts(db, a):
    M = money(db, 'INR')
    month = datetime.now(ZoneInfo(os.environ.get('EXPENSE_TZ') or 'Asia/Riyadh')).strftime('%Y-%m')
    debt = lambda h: {'name': h['name'], 'kind': h['kind'], 'currency': h['currency'], 'owed_now': h['balance_now'] / 100,
                      'monthly_payment': h['emi_minor'] / 100, 'due_day': h['due_day'], 'interest_pct_a_year': h['rate_bp'] / 100,
                      'auto_debit': bool(h['auto']), 'credit_limit': (h.get('limit_minor') or 0) / 100 or None,
                      'paid_this_month': [{'date': x['date'], 'amount': x['amount'], 'how': x['source']} for x in h['payments'] if x['date'][:7] == month]}
    saving = lambda h: {'name': h['name'], 'currency': h['currency'], 'balance': h['balance_minor'] / 100 if h['currency'] != 'GOLD' else None,
                        'grams': h['balance_minor'] / 100 if h['currency'] == 'GOLD' else None}
    plan = M['plans']['as_is']
    return {'debts': [debt(h) for h in M['holdings'] if h['kind'] in DEBTS], 'savings': [saving(h) for h in M['holdings'] if h['kind'] == 'saving'],
            'inr_per_sar': M['rate'], 'debt_free_in_months_at_current_payments': plan['months'], 'interest_still_to_pay_inr': plan['interest'] / 100}


def mcp_trail(db, a):
    m = _month_arg(a)
    first, last = (f'{m}-01', f'{m}-31') if m else (None, None)
    T = trail(db, first, last)
    return {'flows': [{k: f[k] for k in ('from', 'to', 'kind', 'count', 'sent', 'got', 'last')} for f in T['flows']],
            'accounts': [{k: x[k] for k in ('name', 'kind', 'in', 'out', 'count')} for x in T['accounts']],
            'unmatched_transfers': len(T['unmatched'])}


def mcp_friends(db, a):
    owed = defaultdict(lambda: defaultdict(float))
    for t in transactions(db):
        if t['friend'] and not t['settled']:
            owed[t['friend']][t['currency']] += t['theirs'] if t['direction'] == 'debit' else -t['amount']
    S = shared(db)
    return {'friends_owe_you': {f: {c: round(v, 2) for c, v in cs.items() if v} for f, cs in owed.items()},
            'you_owe_people_on_this_app': [{'to': r['from_email'], 'amount': r['amount_minor'] / 100, 'currency': r['currency'], 'for': r['what'], 'date': r['day']} for r in S['to_me']],
            'saved_friends': S['contacts']}


def mcp_balances(db, a):
    month = _month_arg(a) or datetime.now(ZoneInfo(os.environ.get('EXPENSE_TZ') or 'Asia/Riyadh')).strftime('%Y-%m')
    every = transactions(db)
    return {'month': month, 'bank_accounts': account_months(every, month, db.run('SELECT * FROM accounts')),
            'cards_available_limit': [b for b in balances(every) if b['kind'] == 'limit']}


def mcp_add(db, a):
    """A transaction you tell the assistant about, stored like a bank SMS (so it's categorized and counted the same way)."""
    amount, cur = Decimal(str(a.get('amount') or 0)), _cur_arg(a, 'SAR')
    merchant = ' '.join(str(a.get('merchant') or '').split())[:60]
    if not 0 < amount < 10 ** 9 or not merchant:
        raise ValueError('amount (more than 0) and merchant are required')
    day = str(a.get('date') or '').strip() or datetime.now(ZoneInfo(os.environ.get('EXPENSE_TZ') or 'Asia/Riyadh')).date().isoformat()
    date.fromisoformat(day)
    spent = str(a.get('direction') or 'spent') != 'received'
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    body = f"{'Paid' if spent else 'Received'} {cur} {amount:.2f} {'to' if spent else 'from'} {merchant} on {day} (added by your assistant {now})"
    stored = store_sms(db, 'Assistant', body, f'{day}T12:00:00+03:00')
    if not stored:
        raise ValueError('it could not be stored')
    t = next(x for x in transactions(db) if x['body'] == body)
    if a.get('category'):
        set_category(db, t['id'], ' '.join(str(a['category']).split())[:MAX_CATEGORY], False)
        t = next(x for x in transactions(db) if x['id'] == t['id'])
    return {'added': _brief(t)}


def mcp_categorize(db, a):
    cat = ' '.join(str(a.get('category') or '').split())
    if not 0 < len(cat) <= MAX_CATEGORY:
        raise ValueError(f'category must be 1-{MAX_CATEGORY} characters')
    tid = int(a.get('transaction_id') or 0)
    set_category(db, tid, cat, bool(a.get('all_from_merchant')))
    t = next((x for x in transactions(db) if x['id'] == tid), None)
    return {'transaction': _brief(t) if t else None}


_S = lambda props, req=(): {'type': 'object', 'properties': props, 'required': list(req), 'additionalProperties': False}
_MONTH = {'type': 'string', 'description': 'YYYY-MM, e.g. 2026-10. Leave out for the current month.'}
_CUR = {'type': 'string', 'enum': ['SAR', 'INR', 'USD'], 'description': 'Only this currency (amounts are never mixed).'}
MCP_TOOLS = {
    'get_overview': ('A month at a glance: spending by category, top merchants, by card/account, and the last 12 months of spent vs received.',
                     _S({'month': _MONTH, 'currency': _CUR}), mcp_overview, True),
    'list_transactions': ('Transactions, newest first, optionally for one month, category, merchant (part of the name) or direction.',
                          _S({'month': _MONTH, 'currency': _CUR, 'category': {'type': 'string'}, 'merchant': {'type': 'string'},
                              'direction': {'type': 'string', 'enum': ['spent', 'received']}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': MCP_LIST_MAX}}),
                          mcp_transactions, True),
    'search_transactions': ('Find transactions in every month whose text, merchant, category or card contains all these words.',
                            _S({'query': {'type': 'string'}, 'currency': _CUR}, ['query']), mcp_search, True),
    'get_debts': ('Loans, credit cards and pay-later plans (owed now, monthly payment, due day, interest, paid this month, credit limit), savings, and when debt is cleared at current payments.',
                  _S({}), mcp_debts, True),
    'get_money_trail': ('How money moved between your own accounts (e.g. salary → Saudi bank → Indian bank → loan), with totals per route.',
                        _S({'month': _MONTH}), mcp_trail, True),
    'get_friends': ('What friends owe you for purchases you split, and what you owe people on this app.', _S({}), mcp_friends, True),
    'get_account_balances': ('Each bank account in a month: balance carried from the month before, money in, money out, closing balance, and the latest balance the bank stated; cards’ available limits.',
                             _S({'month': _MONTH}), mcp_balances, True),
    'add_transaction': ('Record a purchase or money received that the bank SMS didn’t cover. Only when the user asks to add it.',
                        _S({'amount': {'type': 'number', 'exclusiveMinimum': 0}, 'currency': _CUR, 'merchant': {'type': 'string'},
                            'direction': {'type': 'string', 'enum': ['spent', 'received']}, 'date': {'type': 'string', 'description': 'YYYY-MM-DD; today if left out'},
                            'category': {'type': 'string'}}, ['amount', 'merchant']), mcp_add, False),
    'set_category': ('Change a transaction’s category (and optionally every transaction from that merchant). Only when the user asks.',
                     _S({'transaction_id': {'type': 'integer'}, 'category': {'type': 'string'}, 'all_from_merchant': {'type': 'boolean'}},
                        ['transaction_id', 'category']), mcp_categorize, False),
}


def mcp_reply(space, msg):
    """One JSON-RPC message in, its response out (None for a notification)."""
    if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0' or 'method' not in msg:
        return {'jsonrpc': '2.0', 'id': msg.get('id') if isinstance(msg, dict) else None, 'error': {'code': -32600, 'message': 'invalid request'}}
    if 'id' not in msg:
        return None  # notifications/initialized and the like need no answer
    ok = lambda result: {'jsonrpc': '2.0', 'id': msg['id'], 'result': result}
    method, params = msg['method'], msg.get('params') or {}
    if method == 'initialize':
        asked = params.get('protocolVersion')
        return ok({'protocolVersion': asked if asked in MCP_VERSIONS else MCP_VERSIONS[0], 'capabilities': {'tools': {'listChanged': False}},
                   'serverInfo': {'name': 'expense', 'title': 'Expenses', 'version': VERSION},
                   'instructions': 'Personal spending, debts and transfers from bank SMS and statements. Amounts are in their own currency (SAR, INR, USD); '
                                   'never add different currencies together. Use the write tools only when the user asks.'})
    if method == 'ping':
        return ok({})
    if method == 'tools/list':
        return ok({'tools': [{'name': n, 'description': d, 'inputSchema': schema, 'annotations': {'readOnlyHint': ro, 'destructiveHint': False}}
                             for n, (d, schema, _, ro) in MCP_TOOLS.items()]})
    if method == 'tools/call':
        tool = MCP_TOOLS.get(params.get('name'))
        if not tool:
            return {'jsonrpc': '2.0', 'id': msg['id'], 'error': {'code': -32602, 'message': f"unknown tool: {params.get('name')}"}}
        db = DB(space=space)
        try:
            data = tool[2](db, params.get('arguments') or {})
            return ok({'content': [{'type': 'text', 'text': json.dumps(data, ensure_ascii=False, default=str)}], 'structuredContent': data, 'isError': False})
        except (ValueError, KeyError, TypeError, ArithmeticError) as e:
            return ok({'content': [{'type': 'text', 'text': f'Could not do that: {e}'}], 'isError': True})
        finally:
            db.close()
    return {'jsonrpc': '2.0', 'id': msg['id'], 'error': {'code': -32601, 'message': f'unknown method: {method}'}}


def _mcp(environ, method, path, respond):
    if method != 'POST':  # no server-to-client event stream: everything is answered in the POST
        return respond('405 Method Not Allowed', {'error': 'POST JSON-RPC messages here'}, headers=[('Allow', 'POST')])
    header = environ.get('HTTP_AUTHORIZATION', '')
    key = path[len('/mcp/'):] if path.startswith('/mcp/') else header[7:] if header.startswith('Bearer ') else ''
    result, space, _ = sign_in(environ, unquote(key), key=True) if key and _token() else ('wrong', None, '')
    if result != 'ok':
        return respond('401 Unauthorized', {'error': 'use your connector link from Settings (it has your key in it)'})
    try:
        length = int(environ.get('CONTENT_LENGTH') or 0)
        if not 0 < length <= MCP_MAX_BODY:
            raise ValueError
        incoming = json.loads(environ['wsgi.input'].read(length))
    except ValueError:
        return respond('400 Bad Request', {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}})
    replies = [r for r in (mcp_reply(space, m) for m in (incoming if isinstance(incoming, list) else [incoming])) if r is not None]
    if not replies:
        return respond('202 Accepted', b'', 'text/plain')
    return respond('200 OK', replies if isinstance(incoming, list) else replies[0])


def app(environ, start_response):
    """Any unexpected error becomes a 500 that says what went wrong (type and message, no stack or data), so the
    page's error bar can show it; the full trace still goes to the server log."""
    try:
        return _app(environ, start_response)
    except Exception as e:
        import traceback
        traceback.print_exc()
        body = json.dumps({'error': f'{type(e).__name__}: {str(e)[:200]}'}).encode()
        start_response('500 Internal Server Error', [('Content-Type', 'application/json'), ('Content-Length', str(len(body))),
                                                    ('Cache-Control', 'no-store')])
        return [body]


def _app(environ, start_response):
    def respond(status, payload, content_type='application/json', headers=()):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        start_response(status, [('Content-Type', content_type), ('Content-Length', str(len(data))),
                                ('Cache-Control', 'no-store'), *headers])
        return [data]

    def redirect(location, cookie=None, admin=None):
        headers = [('Location', location)]
        if cookie is not None:
            max_age = SESSION_DAYS * 86400 if cookie else 0
            headers.append(('Set-Cookie', f'session={cookie}; Max-Age={max_age}; Path=/; HttpOnly; Secure; SameSite=Lax'))
        if admin is not None:
            headers.append(('Set-Cookie', f'admin={admin}; Max-Age={ADMIN_HOURS * 3600 if admin else 0}; Path=/; HttpOnly; Secure; SameSite=Strict'))
        return respond('303 See Other', b'', 'text/plain', headers)

    method, path = environ['REQUEST_METHOD'], environ.get('PATH_INFO') or '/'
    query = {k: v[0] for k, v in parse_qs(environ.get('QUERY_STRING', '')).items()}

    if method == 'GET' and path in PUBLIC:
        return respond('200 OK', (HERE / path.lstrip('/')).read_bytes(), PUBLIC[path])
    if path == '/login':
        if method == 'POST':
            if not _token():  # says only that setup is incomplete, nothing about the password itself
                return redirect('/login?error=setup')
            form = read_form(environ)
            result, space, pw_hash = sign_in(environ, form.get('password', ''), email=form.get('email', ''))
            if result == 'ok':
                return redirect('/', new_session(space, pw_hash))
            return redirect('/login?error=locked' if result == 'locked' else '/login?error=1')
        return respond('200 OK', (HERE / 'login.html').read_bytes(), 'text/html; charset=utf-8')
    if (method, path) == ('POST', '/logout'):
        return redirect('/login', '')
    if path == '/mcp' or path.startswith('/mcp/'):
        return _mcp(environ, method, path, respond)
    if path == '/admin' or path.startswith(('/admin/', '/api/admin/')):
        return _admin(environ, method, path, query, respond, redirect)

    space = None
    if (method, path) == ('POST', '/sms'):
        space = bearer_space(environ)
        if space is None:  # phone only: the cookie isn't accepted here, so no cross-site form can add SMS
            refused = DB()
            try:
                record_delivery(refused, '?', 'refused: wrong password in the phone app')
            finally:
                refused.close()
            return respond('401 Unauthorized', {'error': 'unauthorized'})
        try:
            sms = read_sms(environ)
        except (ValueError, KeyError, TypeError) as e:
            refused = DB()
            try:
                record_delivery(refused, '?', 'refused: the phone app sent an unreadable message')
            finally:
                refused.close()
            return respond('400 Bad Request', {'error': f'bad request: {e}'})
    elif (space := session_space(environ) or bearer_space(environ)) is None:
        return redirect('/login') if method == 'GET' and path in PAGES else respond('401 Unauthorized', {'error': 'unauthorized'})
    elif method == 'GET' and path in PAGES:
        return respond('200 OK', (HERE / PAGES[path]).read_bytes(), 'text/html; charset=utf-8')
    elif (method, path) == ('POST', '/api/import'):
        try:
            batch = read_import(environ)
        except (ValueError, KeyError, TypeError) as e:
            return respond('400 Bad Request', {'error': f'bad request: {e}'})
    elif (method, path) == ('POST', '/api/deliveries/save'):
        try:
            keep = read_json(environ, MAX_BODY)
            amount_minor = int(Decimal(str(keep['amount']).replace(',', '').strip()) * 100)
            if not 0 < amount_minor < 10 ** 11 or keep['direction'] not in ('debit', 'credit') or keep['currency'] not in ('SAR', 'INR'):
                raise ValueError('amount, direction (debit/credit) and currency (SAR/INR) are required')
            keep = (int(keep['id']), amount_minor, keep['direction'], keep['currency'])
        except (ValueError, KeyError, TypeError, ArithmeticError) as e:
            return respond('400 Bad Request', {'error': f'bad request: {e}'})
    elif (method, path) == ('POST', '/api/category'):
        try:
            correction = read_category(environ)
        except (ValueError, KeyError, TypeError) as e:
            return respond('400 Bad Request', {'error': f'bad request: {e}'})
    elif (method, path) == ('POST', '/api/friend'):
        try:
            tag = read_friend(environ)
        except (ValueError, KeyError, TypeError, ArithmeticError) as e:
            return respond('400 Bad Request', {'error': f'bad request: {e}'})
    elif (method, path) == ('POST', '/api/delete'):
        try:
            doomed = int(read_json(environ, MAX_BODY)['id'])
        except (ValueError, KeyError, TypeError) as e:
            return respond('400 Bad Request', {'error': f'bad request: {e}'})
    elif (method, path) == ('POST', '/api/money'):
        try:
            holding = read_holding(environ)
        except (ValueError, KeyError, TypeError, ArithmeticError) as e:
            return respond('400 Bad Request', {'error': f'bad request: {e}'})
    elif (method, path) == ('POST', '/api/money/pay'):
        try:
            payment = read_payment(environ)
        except (ValueError, KeyError, TypeError, ArithmeticError) as e:
            return respond('400 Bad Request', {'error': f'bad request: {e}'})
    elif (method, path) == ('POST', '/api/money/pay/delete'):
        try:
            unpaid = int(read_json(environ, MAX_BODY)['id'])
        except (ValueError, KeyError, TypeError) as e:
            return respond('400 Bad Request', {'error': f'bad request: {e}'})
    elif (method, path) == ('POST', '/api/settings'):
        try:
            setting = read_json(environ, MAX_BODY)
            if setting['key'] not in SETTING_KEYS:
                raise ValueError(f'key must be one of {sorted(SETTING_KEYS)}')
            setting = (setting['key'], json.dumps(setting['value']))
        except (ValueError, KeyError, TypeError) as e:
            return respond('400 Bad Request', {'error': f'bad request: {e}'})
    elif (method, path) == ('POST', '/api/plan'):
        try:
            action = read_action(environ)
        except (ValueError, KeyError, TypeError, ArithmeticError) as e:
            return respond('400 Bad Request', {'error': f'bad request: {e}'})
    elif (method, path) == ('POST', '/api/money/delete'):
        try:
            gone = int(read_json(environ, MAX_BODY)['id'])
        except (ValueError, KeyError, TypeError) as e:
            return respond('400 Bad Request', {'error': f'bad request: {e}'})
    elif (method, path) == ('POST', '/api/statement'):
        try:
            upload = read_json(environ, MAX_STATEMENT_BODY)
            name, kind, cur = ' '.join(str(upload['account']).split())[:60], upload['kind'], upload['currency']
            digits = re.sub(r'\D', '', str(upload.get('digits') or ''))[-4:]
            if not name or kind not in ACCOUNT_KINDS or cur not in ('SAR', 'INR', 'USD'):
                raise ValueError(f'account name, kind ({sorted(ACCOUNT_KINDS)}) and currency (SAR/INR/USD) are required')
            upload = (name, kind, cur, digits, str(upload.get('file_name') or 'statement')[:120],
                      base64.b64decode(upload['data'], validate=True), str(upload.get('password') or '') or None)
        except (ValueError, KeyError, TypeError) as e:
            return respond('400 Bad Request', {'error': f'bad request: {e}'})
    elif (method, path) == ('POST', '/api/link'):
        try:
            pair = read_json(environ, MAX_BODY)
            pair = (int(pair['debit']), None if pair.get('credit') is None else int(pair['credit']))
        except (ValueError, KeyError, TypeError) as e:
            return respond('400 Bad Request', {'error': f'bad request: {e}'})
    elif (method, path) in (('POST', '/api/me/username'), ('POST', '/api/me/password')):
        pass  # handled with /api/me below
    elif (method, path) == ('POST', '/api/shared/paid'):
        try:
            paid = read_json(environ, MAX_BODY)
            paid = (str(paid['from']), str(paid['currency']))
        except (ValueError, KeyError, TypeError) as e:
            return respond('400 Bad Request', {'error': f'bad request: {e}'})
    elif (method, path) == ('POST', '/api/peer/unlink'):
        try:
            unpeer = int(read_json(environ, MAX_BODY)['id'])
        except (ValueError, KeyError, TypeError) as e:
            return respond('400 Bad Request', {'error': f'bad request: {e}'})
    elif (method, path) == ('POST', '/api/account'):
        try:
            acc = read_json(environ, MAX_BODY)
            if acc.get('currency') and acc['currency'] not in ('SAR', 'INR', 'USD'):
                raise ValueError('currency must be SAR, INR or USD')
            if acc.get('balance') not in (None, ''):
                acc['balance'] = Decimal(str(acc['balance']).replace(',', '').strip())
            else:
                acc['balance'] = None
        except (ValueError, KeyError, TypeError, ArithmeticError) as e:
            return respond('400 Bad Request', {'error': f'bad request: {e}'})
    elif (method, path) == ('POST', '/api/contact'):
        try:
            contact = read_json(environ, MAX_BODY)
            contact = (' '.join(str(contact['name']).split())[:MAX_FRIEND], str(contact.get('email') or ''))
            if not contact[0]:
                raise ValueError('a name is needed')
        except (ValueError, KeyError, TypeError) as e:
            return respond('400 Bad Request', {'error': f'bad request: {e}'})
    elif (method, path) == ('POST', '/api/statement/delete'):
        try:
            undone = int(read_json(environ, MAX_BODY)['id'])
        except (ValueError, KeyError, TypeError) as e:
            return respond('400 Bad Request', {'error': f'bad request: {e}'})
    elif (method, path) == ('POST', '/api/friend/settle'):
        try:
            square = str(read_json(environ, MAX_BODY)['friend'])
        except (ValueError, KeyError, TypeError) as e:
            return respond('400 Bad Request', {'error': f'bad request: {e}'})
    elif (method, path) not in {('GET', '/api/summary'), ('GET', '/api/transactions'), ('GET', '/api/health'), ('GET', '/api/search'), ('GET', '/api/latest'), ('GET', '/api/money'), ('GET', '/api/rate'), ('GET', '/api/plan'), ('GET', '/api/settings'), ('GET', '/api/statements'), ('GET', '/api/trail'), ('GET', '/api/balances'), ('GET', '/api/me'), ('GET', '/api/shared'), ('GET', '/api/contact/check'), ('GET', '/api/export'), ('GET', '/api/accounts')}:
        return respond('404 Not Found', {'error': 'not found'})

    started = time.perf_counter()
    if path in ('/api/me/username', '/api/me/password'):  # your own account: a username, a new password
        pub = DB()
        try:
            body = read_json(environ, MAX_BODY)
            if path == '/api/me/username':
                return respond('200 OK', {'username': set_username(pub, space, str(body.get('username') or ''))})
            new_hash = change_password(pub, space, str(body.get('current') or ''), str(body.get('new') or ''))
            cookie = new_session(space, new_hash)  # other devices are signed out; this one stays in
            return respond('200 OK', {'changed': True}, headers=[('Set-Cookie', f'session={cookie}; Max-Age={SESSION_DAYS * 86400}; Path=/; HttpOnly; Secure; SameSite=Lax')])
        except (ValueError, KeyError, TypeError) as e:
            return respond('400 Bad Request', {'error': str(e)})
        finally:
            pub.close()
    if path == '/api/me':  # who is signed in, and their phone sync key
        pub = DB()
        try:
            me = pub.run('SELECT email, username, sync_key FROM app_users WHERE space = :s', s=space)
        finally:
            pub.close()
        return respond('200 OK', {'email': me[0]['email'] if me else None, 'username': me[0]['username'] if me else None, 'owner': space == 'public',
                                  'sync_key': me[0]['sync_key'] if me else None})
    db = DB(space=space)
    try:
        if path == '/api/health':
            db.run('SELECT 1')
            return respond('200 OK', health(db, round((time.perf_counter() - started) * 1000)))
        if path == '/api/deliveries/save':
            try:
                save_skipped(db, *keep)
            except KeyError:
                return respond('404 Not Found', {'error': 'that message is no longer kept'})
            return respond('200 OK', {'saved': True})
        if path == '/api/category':
            try:
                set_category(db, *correction)
            except KeyError:
                return respond('404 Not Found', {'error': 'no such transaction'})
            # What it now shows as (so the page can say if another rule still wins), plus the updated
            # dashboard, so saving is one request and one pass over the data instead of three.
            every = transactions(db)
            now = next(t['category'] for t in every if t['id'] == correction[0])
            return respond('200 OK', {'saved': True, 'category': now, **refreshed(db, query, every)})
        if path == '/api/friend':
            try:
                set_friend(db, *tag[:3])
                if tag[1] and tag[3]:
                    set_contact(db, tag[1], tag[3])
                saved = db.run('SELECT email FROM contacts WHERE LOWER(name) = :n', n=tag[1].lower()) if tag[1] else []
                linked = bool(saved) and on_app(db, saved[0]['email'])
            except KeyError:
                return respond('404 Not Found', {'error': 'no such transaction'})
            except ValueError as e:
                return respond('400 Bad Request', {'error': f'bad request: {e}'})
            return respond('200 OK', {'saved': True, 'linked': linked, 'email': saved[0]['email'] if saved else '', **refreshed(db, query)})
        if path == '/api/delete':
            try:
                delete_transaction(db, doomed)
            except KeyError:
                return respond('404 Not Found', {'error': 'no such transaction'})
            return respond('200 OK', {'deleted': True, **refreshed(db, query)})
        if path == '/api/rate':
            rate = inr_per_sar()
            return respond('200 OK' if rate else '503 Service Unavailable', rate or {'error': 'no exchange-rate service answered'})
        if path == '/api/settings':
            if method == 'POST':
                db.run('INSERT INTO settings (key, value) VALUES (:k, :v) ON CONFLICT (key) DO UPDATE SET value = excluded.value', k=setting[0], v=setting[1])
            return respond('200 OK', {r['key']: json.loads(r['value']) for r in db.run('SELECT key, value FROM settings')})
        if path == '/api/plan':
            if method == 'POST':
                save_action(db, action)
            month = action['month'] if method == 'POST' else query.get('month') or datetime.now(timezone.utc).strftime('%Y-%m')
            try:
                return respond('200 OK', plan_month(db, month))
            except ValueError:
                return respond('400 Bad Request', {'error': 'month must look like 2026-12'})
        if path.startswith('/api/money'):
            if path == '/api/money/pay':
                try:
                    record_payment(db, *payment)
                except KeyError:
                    return respond('404 Not Found', {'error': 'no such loan'})
            elif path == '/api/money/pay/delete':
                db.run('DELETE FROM loan_payments WHERE id = :id', id=unpaid)
            elif path == '/api/money' and method == 'POST':
                try:
                    save_holding(db, holding)
                except KeyError:
                    return respond('404 Not Found', {'error': 'no such loan or saving'})
            elif path == '/api/money/delete':
                db.run('DELETE FROM holdings WHERE id = :id', id=gone)
                db.run('DELETE FROM loan_payments WHERE holding_id = :id', id=gone)
            try:
                extra = int(Decimal(query.get('extra') or 0) * 100)
            except ArithmeticError:
                extra = 0
            try:
                rate = float(query.get('rate') or 0)
            except ValueError:
                rate = 0
            try:
                options = [max(0, min(int(Decimal(x) * 100), 10 ** 11)) for x in (query.get('options') or '').split(',') if x.strip()][:6]
            except ArithmeticError:
                options = []
            result = money(db, query.get('currency') or 'INR', max(0, min(extra, 10 ** 11)), rate=rate if 10 < rate < 100 else None, options=options)
            try:
                usd = float(query.get('usd') or 0)
            except ValueError:
                usd = 0
            result['usd'] = usd if 30 < usd < 300 else None  # for savings held in dollars
            try:
                gold = float(query.get('gold') or 0)  # ₹ a gram of 24K gold, from the page
            except ValueError:
                gold = 0
            result['gold_inr_g'] = gold if 1000 < gold < 100000 else None
            priced = result['gold_inr_g'] or not any(h['currency'] == 'GOLD' for h in result['holdings'])  # else gold would count as 0
            if priced and (result['currency'] == 'INR' and result['rate'] or not any(h['currency'] == 'SAR' for h in result['holdings'])):
                snapshot(db, result)
            result['history'] = db.run('SELECT * FROM snapshots ORDER BY month')
            return respond('200 OK', result)
        if path == '/api/statement':
            try:
                done = import_statement(db, *upload)
                peers_quietly(db)
                return respond('200 OK', done)
            except StatementError as e:
                return respond('422 Unprocessable Entity', {'error': str(e)})
            except Exception as e:  # a file the readers choke on: say so instead of a bare 500
                print(f'statement import failed: {type(e).__name__}: {e}', file=sys.stderr)
                return respond('422 Unprocessable Entity', {'error': f'could not read this file ({type(e).__name__}). Try the Excel or CSV download instead.'})
        if path == '/api/link':
            try:
                link_by_hand(db, *pair)
            except KeyError as e:
                return respond('400 Bad Request', {'error': str(e.args[0])})
            return respond('200 OK', {'saved': True})
        if path == '/api/statement/delete':
            try:
                delete_statement(db, undone)
            except KeyError:
                return respond('404 Not Found', {'error': 'no such statement'})
            return respond('200 OK', statements(db))
        if path == '/api/statements':
            return respond('200 OK', statements(db))
        if path == '/api/export':  # Settings → Download all my data
            return respond('200 OK', export_data(db), headers=[('Content-Disposition', f'attachment; filename="expense-data-{date.today().isoformat()}.json"')])
        if path == '/api/shared':
            return respond('200 OK', shared(db))
        if path == '/api/contact/check':  # while typing a friend's email: are they on this app?
            return respond('200 OK', {'app': on_app(db, query.get('email', ''))})
        if path == '/api/shared/paid':
            try:
                paid_back(db, *paid)
            except KeyError as e:
                return respond('400 Bad Request', {'error': str(e.args[0])})
            return respond('200 OK', shared(db))
        if path == '/api/account':
            try:
                if acc.get('delete'):
                    delete_account(db, str(acc.get('name') or ''))
                else:
                    save_account(db, str(acc.get('label') or ''), str(acc.get('name') or ''), str(acc.get('currency') or ''), acc['balance'],
                                 None if acc.get('hidden') is None else bool(acc['hidden']), str(acc.get('kind') or ''))
            except ValueError as e:
                return respond('400 Bad Request', {'error': str(e)})
            return respond('200 OK', {'saved': True})
        if path == '/api/peer/unlink':
            try:
                unlink_peer(db, unpeer)
            except KeyError:
                return respond('404 Not Found', {'error': 'not linked'})
            return respond('200 OK', {'saved': True, **refreshed(db, query)})
        if path == '/api/contact':
            try:
                set_contact(db, *contact)
            except ValueError as e:
                return respond('400 Bad Request', {'error': str(e)})
            return respond('200 OK', shared(db))
        if path == '/api/accounts':
            month = query.get('month') or datetime.now(ZoneInfo(os.environ.get('EXPENSE_TZ') or 'Asia/Riyadh')).strftime('%Y-%m')
            if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month):
                return respond('400 Bad Request', {'error': 'month must look like 2026-10'})
            every, typed = transactions(db), db.run('SELECT * FROM accounts')
            seen = defaultdict(lambda: {'count': 0, 'currency': ''})
            for t in every:
                if t['category'] != IGNORED:  # an account whose messages are all ignored (e.g. provident-fund SMS) isn't listed
                    seen[t['account']]['count'] += 1
                    seen[t['account']]['currency'] = t['currency']
            hidden = {a['name'] for a in typed if a['hidden']}
            names = sorted((set(seen) | {a['name'] for a in typed}) - hidden)
            kinds = account_kinds(every, typed)
            return respond('200 OK', {'month': month, 'accounts': account_months(every, month, typed),
                                      'all': [{'name': n, 'transactions': seen[n]['count'], 'currency': seen[n]['currency'] or next((a['currency'] for a in typed if a['name'] == n), ''),
                                               'added': any(a['name'] == n and not a['label'] for a in typed),
                                               'kind': kinds.get(n, ('bank', 'name'))[0], 'how': kinds.get(n, ('bank', 'name'))[1]} for n in names],
                                      'hidden': sorted(hidden)})
        if path == '/api/balances':
            every, typed = transactions(db), db.run('SELECT * FROM accounts')
            kinds = account_kinds(every, typed)
            return respond('200 OK', [{**b, 'account_kind': kinds.get(b['account'], ('bank', 'name'))[0]} for b in known_balances(every, typed)])
        if path == '/api/trail':
            return respond('200 OK', trail(db, query.get('from'), query.get('to')))
        if path == '/api/friend/settle':
            settle_friend(db, square, query.get('currency') or 'SAR')
            return respond('200 OK', {'saved': True, **refreshed(db, query)})
        if path == '/api/import':
            stored = store_many(db, batch)
            for _, body, received_at in sorted(batch, key=lambda m: m[2]):  # oldest first, so the newest reminder wins
                card_statement(db, body, received_at)
            if environ.get('HTTP_AUTHORIZATION', '').startswith('Bearer '):  # the Expense Sync phone app (the import page uses the login cookie)
                record_delivery(db, 'Phone sync', f'{len(stored)} new of {len(batch)} checked')
            if stored:
                peers_quietly(db)
            return respond('200 OK', {'received': len(batch), 'stored': len(stored)})
        if method == 'POST':
            if not_money(*sms[:2]):  # a chat or a message with no amount: nothing is kept, not even in the phone log
                return respond('200 OK', {'stored': False, 'ignored': 'not about money'})
            stored = store_sms(db, *sms)
            if stored:
                peers_quietly(db)
            advert = bool(AD_SENDER.search(sms[0].strip()))
            skipped = not stored and not advert and not parse(sms[1])
            card = skipped and card_statement(db, sms[1], sms[2])
            record_delivery(db, sms[0], 'advert, not counted' if advert else f'card due: {card} updated on the Debt tab' if card else 'not a transaction' if skipped
                            else 'saved' if stored else 'already had it', (sms[1], sms[2]) if skipped and not card else None)
            return respond('201 Created' if stored else '200 OK', {'stored': bool(stored), 'transaction': stored})
        if path == '/api/summary':
            return respond('200 OK', summary(db, query.get('month'), query.get('currency')))
        if path == '/api/latest':  # a tiny "anything new?" check the open app polls; one cheap query
            return respond('200 OK', db.run('SELECT COUNT(*) AS count, MAX(id) AS last_id FROM transactions')[0])
        if path == '/api/search':
            return respond('200 OK', search(db, query.get('q', ''), query.get('currency')))
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
