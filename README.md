# Expense

Tracks your spending automatically from bank SMS. It's built mainly for Saudi bank SMS (SAR, English or Arabic), and also reads UPI and card SMS from Indian banks (INR).

```
Samsung phone ──new bank SMS──▶ "SMS to URL Forwarder" app ──HTTPS──▶ Vercel (server.py) ──▶ Neon Postgres
                                                                              │
                                                    you, in a browser ◀── dashboard: per currency, month, category, merchant
```

Everything here runs on free plans: **Vercel Hobby** (hosting), **Neon Free** (Postgres, no card needed; years of SMS take only a few MB) and **SMS to URL Forwarder** (an open-source Android app).

- **Private:** only completed transactions are stored. OTPs, verification codes, bill reminders, loan offers and collect requests are dropped (English and Arabic).
- **INR and SAR are kept separate.** The dashboard opens in SAR and has a switch for INR. Amounts are never added together across currencies.
- **Categories** come from `rules.json` (category → keywords, with Indian and Saudi merchants included). They are worked out when you view the data, so editing the file and pushing it re-categorizes all past transactions.

---

## 1. Deploy the server on Vercel (about 10 minutes, free)

1. Merge this branch into `main` on GitHub. Vercel deploys `main` to your public URL.
2. Go to [vercel.com](https://vercel.com), sign up with GitHub (Hobby plan) and choose **Add New → Project**. Import `Expense` and keep the defaults. `vercel.json` already tells Vercel it's a Python app. Click **Deploy**. The first deploy will show "unauthorized" until step 4 is done; that's expected.
3. **Add the database:** open the project, go to **Storage → Create Database → Neon (Serverless Postgres)**, pick the **Free** plan and the region closest to you (Mumbai or Frankfurt both work), and connect it to the project. This adds the `DATABASE_URL` environment variable automatically. The table is created on the first request.
4. **Set the secrets:** go to **Settings → Environment Variables** and add:

   | Name | Value |
   |---|---|
   | `EXPENSE_TOKEN` | Your login password, also used by the phone. At least 6 characters; longer is safer. After 10 wrong tries, that device is locked out for 15 minutes. |
   | `EXPENSE_TZ` | Optional. Defaults to `Asia/Riyadh`; set it to `Asia/Kolkata` only if you move. This decides where one month ends and the next begins. |

5. Go to **Deployments → ⋯ → Redeploy** so the new variables take effect.
6. Open `https://<your-project>.vercel.app` and sign in on the login page with your `EXPENSE_TOKEN` as the password. You stay signed in for 30 days on that device, until you tap **Sign out**. Changing `EXPENSE_TOKEN` signs out every device.

## 2. Forward SMS from the Samsung Galaxy S23 Ultra

Samsung Messages stays your normal SMS app. The forwarder only listens for new SMS and sends matching ones to your server.

**Install:** get **SMS to URL Forwarder** (`tech.bogomolov.incomingsmsgateway`) from [F-Droid](https://f-droid.org/packages/tech.bogomolov.incomingsmsgateway/). Either install the F-Droid app first, or download the APK from that page and allow your browser to "Install unknown apps" when asked.

> If you installed the APK through the browser and Android won't let you grant the SMS permission ("Restricted setting"), go to **Settings → Apps → SMS to URL Forwarder → ⋮ (top right) → Allow restricted settings**, then grant the permission.

**Add a forwarding rule** (tap **+** in the app):

| Field | Value |
|---|---|
| Sender | `*` (all senders) |
| Webhook URL | `https://<your-project>.vercel.app/sms` |
| SMS filter (regex) | `(?i)(rs\.?\|inr\|₹\|sar\|sr\|ريال)\s*:?\s*[0-9٠-٩]\|[0-9٠-٩]\s*(sar\|sr\|ريال)` (only SMS with an amount leave your phone) |
| SIM | Any (works for both the Indian and Saudi SIM / eSIM) |
| JSON template | see below |
| JSON headers | see below |

The filter column above shows `\|` only because of table formatting. Type a plain `|` in the app:
`(?i)(rs\.?|inr|₹|sar|sr|ريال)\s*:?\s*[0-9٠-٩]|[0-9٠-٩]\s*(sar|sr|ريال)`

JSON template:
```json
{"sender": "%from%", "body": "%text%", "received_at": "%sentStamp%"}
```

JSON headers (replace `YOUR_TOKEN`):
```json
{"User-agent": "SMS Forwarder App", "Authorization": "Bearer YOUR_TOKEN"}
```

Save, then send yourself a test SMS such as `Rs 1 paid to TEST`. It should appear on the dashboard. Delete it from Neon's SQL editor afterwards if you like.

**Samsung battery settings (important).** Without these, One UI puts the app to sleep and SMS get missed:
- **Settings → Battery → Background usage limits → Never sleeping apps → +** and add SMS to URL Forwarder.
- **Settings → Apps → SMS to URL Forwarder → Battery → Unrestricted.**

**Good to know:**
- If the phone is offline or the server is slow to wake, the app retries up to 10 times. Duplicate deliveries are ignored.
- Indian bank SMS only arrive while your Indian SIM is active in the phone, including when roaming.

---

## API

`POST /sms` needs `Authorization: Bearer <token>` (the phone). The dashboard and `/api/*` accept either that header or the sign-in cookie from `/login`.

| Method | Path | What |
|---|---|---|
| POST | `/sms` | Fields `sender`, `body`, and optionally `received_at` (ISO 8601, or epoch milliseconds). Send them as JSON or as a form. Returns `201` if stored, `200` if skipped or a duplicate. |
| GET | `/api/summary?currency=SAR&month=YYYY-MM` | Spent and received per month, spending by category, and top 10 merchants. Defaults to the latest currency and month. |
| GET | `/api/transactions?currency=INR&month=YYYY-MM` | Transactions with category. |
| GET | `/` | Dashboard (redirects to `/login` when signed out) |
| GET/POST | `/login` | Login page / sign in with field `password` |
| POST | `/logout` | Sign out |

## Running locally

Uses only the Python 3.12+ standard library, plus `pg8000` if you want Postgres:

```sh
EXPENSE_TOKEN=local-test-token-123 python3 server.py              # SQLite file expense.db, http://127.0.0.1:8000
DATABASE_URL=postgresql://... EXPENSE_TOKEN=... python3 server.py  # Postgres (pip install pg8000)
```

## Improving categories and parsing

- **Uncategorized rows:** hover the row on the dashboard to see the SMS, add a keyword to `rules.json` and push. Matching is case-insensitive and whole-word only, and the first category that matches wins.
- **An SMS parsed wrongly or not at all:** add it, with numbers masked, to `SAMPLES` in `test_expense.py`, fix `parse.py`, and run the tests:

```sh
python3 -m unittest -v
TEST_DATABASE_URL=postgresql://... python3 -m unittest -v   # also tests Postgres (drops the transactions table!)
```
