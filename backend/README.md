# Purely Maurly Backend (Flask + SQLite)

This lightweight backend provides account signup/login using SQLite.

## Features
* Frontend checkout page (checkout.html) with cart integration and success/cancel pages
## Endpoints
| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | /api/health | Health check |
| POST | /api/auth/signup | Create account `{ name, email, password }` |
| POST | /api/auth/login | Login `{ email, password }` |

Example session create body:
```
{
  "cart": [
    {"name": "Lavender Soap", "price": 12.5, "qty": 2},
    {"name": "Vanilla Candle", "price": 18, "qty": 1}
  ],
  "coupon": "OPTIONALCODE"
}
```

The frontend `checkout.html` posts this structure and redirects the user to the hosted Stripe Checkout page. After payment they land on `checkout-success.html?sid=cs_test_...` which fetches `/api/checkout/session/<id>` for final status.
| POST | /api/auth/logout | Destroy current session |
| GET | /api/auth/me | Return current user |
| GET/PATCH | /api/account/preferences | Get or update preference fields |

## Run Locally
1. Create virtual environment (optional)
2. Install requirements:
```
pip install -r backend/requirements.txt
```
3. Start server:
```
python backend/app.py
```
4. Browse: http://127.0.0.1:5000/api/health

### Stripe Setup (Fixing Pay Securely error)
If the checkout button returns an error, you likely have not set your Stripe credentials. Create a `.env` file in the project root (same folder that contains `backend/`) with:
```
STRIPE_SECRET_KEY=sk_test_yourKeyHere
STRIPE_WEBHOOK_SECRET=whsec_yourWebhookSecret (optional for local unless testing webhooks)
```
Restart the server after adding the file.

Development without Stripe: set `ALLOW_FAKE_CHECKOUT=1` in the same `.env` (or environment) to simulate a successful checkout without contacting Stripe. The app will generate a fake session id and redirect directly to the success page.

Example `.env` for local testing:
```
STRIPE_SECRET_KEY=sk_test_1234567890abcdefghijkl
ALLOW_FAKE_CHECKOUT=1
```
Remove `ALLOW_FAKE_CHECKOUT` (or set to 0) when you want to use real Stripe.

Database file: `backend/app.db` (auto-created on first run).

## Integrating Frontend
Replace localStorage mock in `auth.js` with real API calls:
```js
async function api(path, opts={}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers||{}) },
    credentials: 'include',
    ...opts
  });
  const data = await res.json();
  if(!data.ok) throw new Error(data.error||'Request failed');
  return data;
}
```
Then in signup handler:
```js
const data = await api('/api/auth/signup', { method:'POST', body: JSON.stringify({ name, email, password }) });
```
And login:
```js
const data = await api('/api/auth/login', { method:'POST', body: JSON.stringify({ email, password }) });
```
Call `/api/auth/me` on load to set session state.

## Security Notes
- Set `secure=True` on cookies when using HTTPS in production.
- Rotate session tokens on privilege changes.
- Add email verification & rate limiting for production.

## Next Steps
- Password reset flow
- Email verification table
- Admin role + RBAC
- CSRF protection for non-JSON modifying routes

---
Happy building!
