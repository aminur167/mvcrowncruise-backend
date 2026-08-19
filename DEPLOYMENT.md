# Deployment — scheduled jobs (REQUIRED)

The payment system depends on scheduled maintenance jobs. **Without them, the
money path is broken in production** even though every unit of it is correct in
isolation — this was QA finding C6.

Concretely, if nothing is scheduled:

- `reconcile_pending_payments` never runs, so a payment whose IPN was lost stays
  `PENDING` forever and the customer's money is never credited; and
- because `expire_stale_bookings` (correctly) refuses to release a room that
  still has a `PENDING` payment on it, **every abandoned checkout permanently
  removes a cabin from inventory.**

## What must be scheduled

One command runs everything in the correct order — the order is a *safety*
property (releasing a room before reconciling its payment is how a paid room
gets resold), so prefer this over five independent crons:

| Schedule | Command | Why |
|---|---|---|
| every 10 min | `python manage.py run_payment_jobs --quick` | Reconciles in-flight payments with the gateway, then releases dead room holds. Time-sensitive: this is what returns abandoned-checkout cabins to inventory. |
| daily, 02:00 | `python manage.py run_payment_jobs` | The above **plus** balance-deadline reminders/cancellations, closing sailed bookings, and retrying failed invoice emails. |

`run_payment_jobs` isolates failures (one job erroring never stops the others)
and exits non-zero if any job failed, so cron alerting picks it up.

## Railway

Railway crons are **separate services** in the same project, sharing the repo
and environment variables, each with a *Cron Schedule* set in the service
settings. Create two:

1. **`payments-quick`**
   - Cron Schedule: `*/10 * * * *`
   - Custom Start Command: `python manage.py run_payment_jobs --quick`
2. **`payments-daily`**
   - Cron Schedule: `0 2 * * *`
   - Custom Start Command: `python manage.py run_payment_jobs`

Both need the same `DATABASE_URL`, `SSLCOMMERZ_*` and email variables as the web
service. Set *Restart Policy: Never* on cron services — a cron that restarts on
exit will loop.

> Railway crons do not overlap: if a run is still going when the next is due, the
> next is skipped. That is the behaviour we want here.

## Any other host (Render, Fly, a VM, Kubernetes)

The equivalent crontab, for a plain container/VM:

```cron
# Reconcile in-flight payments, then release dead room holds.
*/10 * * * * cd /app && python manage.py run_payment_jobs --quick >> /var/log/payments.log 2>&1

# Full set: + balance deadlines, sailed bookings, unsent invoices.
0 2 * * * cd /app && python manage.py run_payment_jobs >> /var/log/payments.log 2>&1
```

## Monitoring

`reconcile_pending_payments` prints an `ALERT:` line to stderr whenever any
payment has been `PENDING` for more than 24 hours. **That number should always
be zero.** Anything else means a cabin is being held out of inventory against a
payment nobody can resolve — wire this to a real alerting channel, not just
stdout.

Payments the gateway will not resolve are escalated to `needs_manual_review` and
appear in the staff dashboard's manual-review queue
(`GET /api/staff/payments/?needs_manual_review=true`), where staff resolve them
with `POST /api/staff/payments/<id>/resolve/`. They are also retried on a slow
back-off (`PAYMENT_ESCALATED_RETRY_MINUTES`, default 60 min), so a gateway
outage that ends resolves the backlog by itself.
