# Render deploy — MV Alaska backend (temporary demo)

Temporary demo deploy: Render (web service) + Supabase Postgres (Transaction
Pooler, port 6543). Not the final production setup.

## Render service settings

| Setting | Value |
|---|---|
| Root Directory | `backend` (this folder) |
| Runtime | Python 3 |
| Build Command | `pip install -r requirements.txt` |
| Start Command | *(from Procfile)* `python manage.py migrate && python manage.py collectstatic --noinput && gunicorn config.wsgi:application` |
| Instance type | Free |

Or use the committed `render.yaml` via **New → Blueprint**.

## Environment variables (set on Render → Environment)

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | ✅ | Supabase **Transaction Pooler** connection string (host `...pooler.supabase.com`, port **6543**). Include `?sslmode=require`. |
| `SECRET_KEY` | ✅ | Long random string. Render can auto-generate (`generateValue`). |
| `DEBUG` | ✅ | `False`. |
| `ALLOWED_HOSTS` | ✅ | Your Render host, e.g. `mv-alaska-backend.onrender.com` (comma-separated for multiple). |
| `CORS_ALLOWED_ORIGINS` | ✅ | Frontend origin(s), e.g. `https://your-app.vercel.app` (comma-separated, **no trailing slash**). |
| `BACKEND_URL` | ✅ | Full https URL of this service, e.g. `https://mv-alaska-backend.onrender.com`. Used to build SSLCommerz callback URLs. |
| `FRONTEND_URL` | ✅ | Vercel app URL, e.g. `https://your-app.vercel.app`. Used for post-payment redirects. |
| `DRF_NUM_PROXIES` | ✅ | Number of proxies in front of Django — `1` on Render. Getting this wrong means throttles key on the proxy's IP, so **every visitor shares one bucket**; re-check it after any hosting move. |
| `SSLCOMMERZ_STORE_ID` | ✅ | SSLCommerz store id (sandbox for demo). |
| `SSLCOMMERZ_STORE_PASSWORD` | ✅ | SSLCommerz store password. |
| `SSLCOMMERZ_IS_SANDBOX` | ✅ | `True` for the demo. |
| `EMAIL_BACKEND` | ⬜ | Default `...console.EmailBackend` (prints to logs). Set SMTP backend to send real invoice emails. |
| `EMAIL_HOST` / `EMAIL_PORT` / `EMAIL_USE_TLS` | ⬜ | SMTP settings if using real email. |
| `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` | ⬜ | SMTP credentials. |
| `DEFAULT_FROM_EMAIL` | ⬜ | From address on outgoing mail. |
| `BOOKING_HOLD_MINUTES` | ⬜ | Default `30`. |
| `PAYMENT_SESSION_MINUTES` | ⬜ | Default `30`. |
| `BALANCE_DUE_REMINDER_DAYS` | ⬜ | Default `2`. |
| `AUTHORITY_PHONES` | ⬜ | Comma-separated helpline numbers on reports/invoices. |

`PYTHON_VERSION` (e.g. `3.12`) can be set to pin the runtime.

## Notes for the demo

- **Free tier has no cron**, so the payment reconciliation jobs from
  `DEPLOYMENT.md` won't run. Fine for a walkthrough; not for real money.
- Free web services **sleep after inactivity** — the first request after idle
  takes ~30–60s to wake. Warm it up before the client demo.
- Supabase pooler (6543) is already handled in `settings.py`
  (`conn_max_age=0`, `DISABLE_SERVER_SIDE_CURSORS=True`). Do **not** switch to
  the direct connection (5432) for the web service on Render's free tier.

## Frontend (Vercel)

Set this on Vercel → Project → Environment Variables:

```
VITE_API_BASE_URL=https://<your-render-host>.onrender.com/api
```

(The frontend reads `VITE_API_BASE_URL` and expects the `/api` suffix.)

## The backend host shows up in customer links

Invoice download links are absolute URLs built from whatever host this service
answers on, so on Render a customer's receipt reads
`https://<your-render-host>.onrender.com/api/invoices/…`.

This is cosmetic, not a leak: the API origin is compiled into the frontend
bundle and visible in devtools regardless, so nothing is being concealed either
way. It is left as-is deliberately — the links are short-lived (30 minutes, see
`apps/bookings/invoice_access.py`), and the URLs follow the request host with
no configuration, so **the day this moves to a branded host the links follow by
themselves**. Serve the API from `api.<your-domain>` and every link reads that
way with nothing to set.

Two things to redo on any hosting move: `ALLOWED_HOSTS` must list the new host,
and `DRF_NUM_PROXIES` must match the real number of proxies in front of Django
(see the table above — this one is easy to miss and quietly breaks rate
limiting).
