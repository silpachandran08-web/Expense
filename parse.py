"""Turn a bank/UPI SMS into a transaction, and a transaction into a category."""
import re
from decimal import Decimal, InvalidOperation

AMOUNT = re.compile(r'(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d{1,2})?)', re.I)
DEBIT = re.compile(r'\b(debited|spent|paid|sent|withdrawn|purchase)\b', re.I)
CREDIT = re.compile(r'\b(credited|received|deposited|refunded)\b', re.I)
# Messages that mention money but are not a completed transaction.
SKIP = re.compile(r'\b(otp|one[- ]time password|will be debited|is due|due on|requested|pre-?approved|offer)\b', re.I)
_END = r'(?=\s+(?:on|via|ref|refno|upi|avl|avbl|txn|from|to|using)\b|[.,;(]|$)'
MERCHANT = [
    re.compile(r'\bVPA\s+([\w.\-]+@[\w.\-]+)', re.I),
    re.compile(r'\b(?:at|to|towards|trf to|info:?)\s+([\w&@.\-* ]{2,40}?)' + _END, re.I),
    re.compile(r'\bfrom\s+([\w&@.\-* ]{2,40}?)' + _END, re.I),
]
NOT_MERCHANT = re.compile(r'^(?:your|a/?c|ac|acct|account|card|bank|the|you)\b', re.I)


def parse(body):
    """Return {amount_paise, direction, merchant} or None if this is not a completed transaction."""
    if SKIP.search(body):
        return None
    amount = AMOUNT.search(body)
    debit, credit = DEBIT.search(body), CREDIT.search(body)
    if not amount or not (debit or credit):
        return None
    try:
        paise = int(Decimal(amount.group(1).replace(',', '')) * 100)
    except InvalidOperation:
        return None
    if paise <= 0:
        return None
    # Transfers mention both words; the one that comes first describes this account.
    is_debit = debit and (not credit or debit.start() < credit.start())
    return {'amount_paise': paise, 'direction': 'debit' if is_debit else 'credit', 'merchant': _merchant(body)}


def _merchant(body):
    for pattern in MERCHANT:
        for m in pattern.finditer(body):
            name = m.group(1).strip(' .-*')
            if len(name) >= 2 and not NOT_MERCHANT.match(name) and not name.replace(' ', '').isdigit():
                return name
    return ''


def categorize(merchant, body, rules):
    """First category whose keyword appears in the merchant (preferred) or the SMS text."""
    for text in (merchant.lower(), body.lower()):
        for category, keywords in rules.items():
            # Whole-word match, so "ola" doesn't hit "motorola" and "rent" doesn't hit "current".
            if any(re.search(r'\b' + re.escape(k.lower()) + r'\b', text) for k in keywords):
                return category
    return 'Uncategorized'
