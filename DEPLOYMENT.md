# Deploying PurelyMaurly with Porkbun domain

This guide uses Render (free tier) to host the Flask backend and static frontend, and connects your Porkbun domain.

## 1) Prepare repo

- Ensure these files exist (already added):
  - `backend/requirements.txt` (includes `gunicorn`)
  - `render.yaml` (Render blueprint)
  - `backend/app.py` supports `DATABASE_PATH` env var

## 2) Create the Render service

1. Push this repo to GitHub (public or private).
2. Go to https://render.com, create an account, then "New +" → "Blueprint".
3. Point to your repo. Render detects `render.yaml`.
4. Set environment variables during creation:
   - `ADMIN_PANEL_PASSWORD`: a strong password
   - Optionally set Stripe keys (`STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET`)
   - `ALLOW_FAKE_CHECKOUT=1` lets you test without Stripe credentials.
5. Render will build and start your web service. Visit the provided URL to verify.

Notes:
- The app serves the frontend from the repo root (static files) and the Flask API under `/api/*`.
- SQLite database is stored on a persistent disk mounted at `/var/data/app.db`.

## 3) Point Porkbun domain

Assume your Render URL is `https://purelymaurly.onrender.com`.

Option A: Use Render's custom domain and Porkbun DNS
1. In Render, open your service → Settings → Custom domains → Add custom domain (e.g., `www.yourdomain.com`).
2. Render shows DNS targets. In Porkbun dashboard:
   - Add a CNAME record:
     - Host: `www`
     - Type: `CNAME`
     - Answer/Value: the Render domain target (e.g., `your-service.onrender.com`)
     - TTL: default
3. (Optional) Root domain `@`:
   - Render provides A/ALIAS/ANAME options. If offered ALIAS/ANAME, use that with the value Render shows.
   - If not available, point `@` to a lightweight redirect at Porkbun to `https://www.yourdomain.com`.
4. Back in Render, wait for verification; TLS is auto-provisioned.

Option B: Use only `www` and redirect apex in Porkbun
- Create `CNAME www → your-service.onrender.com`.
- Enable Porkbun URL redirect for `@` to `https://www.yourdomain.com`.

Propagation can take up to an hour (often minutes). Test both `www.yourdomain.com` and apex.

## 4) Stripe webhook (optional)

If using Stripe in production:
- In Stripe Dashboard → Developers → Webhooks → Add endpoint
  - URL: `https://yourdomain.com/api/checkout/webhook`
  - Events: `checkout.session.completed`
- Copy the Signing secret into Render env var `STRIPE_WEBHOOK_SECRET`.

## 5) Local dev quickstart

```powershell
# Windows PowerShell
$env:FLASK_SECRET = "dev-secret-key"; $env:ALLOW_FAKE_CHECKOUT="1"
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r backend/requirements.txt
python backend/app.py
```
Visit http://127.0.0.1:5000

## 6) Troubleshooting

- 500 at `/api/checkout/session`: set `STRIPE_SECRET_KEY` or keep `ALLOW_FAKE_CHECKOUT=1`.
- Admin page asks for password: set `ADMIN_PANEL_PASSWORD` in Render env vars.
- Database resets on deploy: ensure the Render disk is attached (see `render.yaml`).
- Static 404: files must be in repo root next to `index.html` as currently structured.
