# Expense

Tracks your spending automatically from bank and UPI SMS on Android.

```
Android phone ──(new SMS, via MacroDroid)──▶ server.py ──▶ SQLite ──▶ dashboard (charts by category, month, merchant)
```

- **No dependencies:** only Python 3.10+ standard library.
- **Private:** only SMS that look like a completed transaction are stored. OTPs, bill reminders, loan offers and collect requests are dropped.
- **Categories** come from `rules.json` (category → keywords). They are worked out when you view the data, so editing the file re-categorizes all past transactions instantly.

## 1. Run the server

```sh
export EXPENSE_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
echo "$EXPENSE_TOKEN"   # save this, the phone and browser need it
python3 server.py       # listens on 0.0.0.0:8000 (change with HOST / PORT, DB path with EXPENSE_DB)
```

Open `http://<server>:8000/`. When the browser asks for a login, use any username and the token as the password.

### Where to host it

The phone must be able to reach the server. **Don't expose plain HTTP to the internet**, because the token would travel unencrypted. Pick one of these:

- **Tailscale (easiest, recommended):** install Tailscale on the phone and on the server (a home PC, Raspberry Pi or cheap VPS). Then use `http://<server-tailscale-name>:8000`. It's private, needs no open ports and no certificates.
- **VPS with HTTPS:** put [Caddy](https://caddyserver.com) in front (`caddy reverse-proxy --from expense.yourdomain.com --to localhost:8000`) and run `server.py` with `HOST=127.0.0.1`.

To keep it running, use a systemd service, `tmux`, or `nohup python3 server.py &`.

## 2. Set up the phone (MacroDroid, free)

Install **MacroDroid** from the Play Store and create a macro:

1. **Trigger:** *SMS Received* → *Any number*. Optionally, under *Content*, choose *Contains* and enter `Rs` so other SMS never leave the phone.
2. **Action:** *Connectivity → HTTP Request*
   - Method: `POST`
   - URL: `http://<server>:8000/sms`
   - Headers: `Authorization` = `Bearer <your token>`
   - Body / content type: **Form URL encoded** (handles quotes and newlines in SMS safely), with two parameters:
     - `sender` = `[sms_number]`
     - `body` = `[sms_message]`
3. Save, and allow MacroDroid's SMS permission and "no battery optimisation" when Android asks.

Tasker, Automate or any other automation app works too, as long as it POSTs `sender` and `body` (form or JSON).

## API

All endpoints need `Authorization: Bearer <token>` (or browser Basic auth with the token as the password).

| Method | Path | What |
|---|---|---|
| POST | `/sms` | `sender`, `body`, and optionally `received_at` (ISO 8601, default: now). Returns `201` if stored, `200` if skipped or a duplicate. |
| GET | `/api/summary?month=YYYY-MM` | Spent and received per month, spending by category, and top 10 merchants (default: latest month). |
| GET | `/api/transactions?month=YYYY-MM` | Transactions with category. |
| GET | `/` | Dashboard |

Test it from your computer:

```sh
curl -H "Authorization: Bearer $EXPENSE_TOKEN" --data-urlencode sender=VM-HDFCBK \
  --data-urlencode 'body=Sent Rs.250.00 From HDFC Bank A/C *1234 To SWIGGY On 18/10/26 Ref 6287' \
  http://localhost:8000/sms
```

## Improving categories

Open the dashboard and look for **Uncategorized** rows. Hover a row to see the original SMS, then add a keyword to `rules.json`. Matching is case-insensitive and whole-word only, and the first category that matches wins.

## Tests

```sh
python3 -m unittest -v
```

When your bank uses an SMS format that isn't parsed correctly, add it to `SAMPLES` in `test_expense.py` and adjust the patterns in `parse.py`.
