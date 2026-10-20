"""Turn a bank/UPI SMS (India: INR, Saudi Arabia: SAR, English or Arabic) into a transaction, and a transaction into a category."""
import re
from decimal import Decimal, InvalidOperation

ARABIC_DIGITS = str.maketrans('٠١٢٣٤٥٦٧٨٩٫٬', '0123456789.,')
CURRENCIES = {'rs': 'INR', 'inr': 'INR', '₹': 'INR', 'sar': 'SAR', 'sr': 'SAR', 'ريال': 'SAR', 'ر.س': 'SAR'}
_CUR = r'(rs\.?|inr|₹|sar|sr|ريال|ر\.س)'
_NUM = r'(\d[\d,]*(?:\.\d{1,2})?)'
# Currency before the number ("Rs.250", "SAR 45.00") is tried first; number before currency ("45.00 ريال") is the fallback,
# so an account number like "XX1234 Rs 500" isn't mistaken for the amount.
CUR_FIRST = re.compile(rf'(?<![A-Za-z]){_CUR}\s*:?\s*{_NUM}', re.I)
NUM_FIRST = re.compile(rf'(?<![\w.]){_NUM}\s*{_CUR}(?![A-Za-z])', re.I)

# Arabic words are matched as substrings because Arabic attaches prefixes ("بال", "و") to words.
DEBIT = re.compile(r'\b(?:debited|spent|paid|sent|withdrawn|withdrawal|purchase|outgoing|pos)\b|شراء|خصم|سحب|صادرة|سداد|دفع', re.I)
CREDIT = re.compile(r'\b(?:credited|received|deposited|deposit|incoming|refunded|refund|reversal)\b|إيداع|ايداع|واردة|استرداد|مسترد|راتب', re.I)
# Messages that mention money but are not a completed transaction.
SKIP = re.compile(
    r'\b(?:otp|one[- ]time password|verification code|passcode|will be debited|is due|due on|requested|pre-?approved|offer)\b'
    r'|رمز التحقق|رمز التفعيل|كلمة المرور|لمرة واحدة|مستحق', re.I)
_END = r'(?=\s+(?:on|via|ref|refno|upi|avl|avbl|txn|from|to|using|for|في)\b|[,;(\n]|\.(?=\s|$)|$)'  # a dot ends the name only as a full stop, so "NOON.COM" stays whole
MERCHANT = [
    re.compile(r'\bVPA\s+([\w.\-]+@[\w.\-]+)', re.I),
    re.compile(r'\b(?:at|to|towards|trf to|info|لدى|إلى|الى)(?:\s*:\s*|\s+)([\w&@.\-* ]{2,40}?)' + _END, re.I),
    re.compile(r'\b(?:from|من)(?:\s*:\s*|\s+)([\w&@.\-* ]{2,40}?)' + _END, re.I),
]
NOT_MERCHANT = re.compile(r'^(?:(?:your|a/?c|ac|acct|account|card|bank|the|you)\b|حساب|بطاق)', re.I)
MAX_SMS = 1000  # bank SMS are far shorter; also keeps the UNIQUE(body) index under Postgres' key size limit


def parse(body):
    """Return {amount_minor, currency, direction, merchant} or None if this is not a completed transaction."""
    body = body.translate(ARABIC_DIGITS)
    if len(body) > MAX_SMS or SKIP.search(body):
        return None
    amount = CUR_FIRST.search(body)
    currency, number = (amount.group(1), amount.group(2)) if amount else (None, None)
    if not amount and (amount := NUM_FIRST.search(body)):
        number, currency = amount.group(1), amount.group(2)
    debit, credit = DEBIT.search(body), CREDIT.search(body)
    if not amount or not (debit or credit):
        return None
    try:
        minor = int(Decimal(number.replace(',', '')) * 100)  # paise / halalas
    except InvalidOperation:
        return None
    if minor <= 0:
        return None
    # Transfers mention both words; the one that comes first describes this account.
    is_debit = debit and (not credit or debit.start() < credit.start())
    return {
        'amount_minor': minor,
        'currency': CURRENCIES[currency.lower().rstrip('.')],
        'direction': 'debit' if is_debit else 'credit',
        'merchant': _merchant(body),
    }


ACCOUNT = re.compile(
    r'(?:\b(card|mada|visa|master\s?card|credit card|debit card|a/?c|acct|account)\b|(بطاقة|حساب))'
    r'[^\d\n]{0,15}?[x*•.]*(\d{4})(?!\d)', re.I)


def account_of(body):
    """'Card •1234' or 'Account •5678': the card or account an SMS is about, from its last 4 digits, or ''."""
    m = ACCOUNT.search(body.translate(ARABIC_DIGITS))
    if not m:
        return ''
    word = (m.group(1) or '').lower()
    kind = 'Card' if m.group(2) == 'بطاقة' or word in {'card', 'mada', 'visa', 'mastercard', 'master card', 'credit card', 'debit card'} else 'Account'
    return f'{kind} •{m.group(3)}'


def merchant_of(body):
    """The merchant name in an SMS, worked out fresh each time so parser fixes reach old messages too."""
    return _merchant(body.translate(ARABIC_DIGITS))


def _merchant(body):
    for pattern in MERCHANT:
        for m in pattern.finditer(body):
            name = m.group(1).strip(' .-*')
            if len(name) >= 2 and not NOT_MERCHANT.match(name) and not name.replace(' ', '').isdigit():
                return name
    return ''


def compile_rules(rules):
    """rules.json ({category: [keywords]}) as one prepared pattern per category: checking 200 keywords
    one by one for every transaction on every page load was most of the dashboard's load time."""
    # Whole-word match, so "ola" doesn't hit "motorola" and "rent" doesn't hit "current".
    return [(category, re.compile(r'\b(?:' + '|'.join(re.escape(k.lower()) for k in keywords) + r')\b'))
            for category, keywords in rules.items() if keywords]


def categorize(merchant, body, compiled, default='Uncategorized'):
    """First category whose keyword appears in the merchant (preferred) or the SMS text."""
    for text in (merchant.lower(), body.lower()):
        for category, pattern in compiled:
            if pattern.search(text):
                return category
    return default
