# panda-invoice-download

Downloads household waste invoices from the [Panda](https://www.panda.ie) customer portal
(my.panda.ie) and uploads the ones you don't have yet to [Paperless-ngx](https://docs.paperless-ngx.com).

Panda doesn't email invoices; you have to sign in and download each one. This script does that on a
schedule.

## How it works

1. Signs in to the portal the same way its web app does (Azure AD B2C, authorization code + PKCE).
   Plain HTTP, no browser. The account must not have a second sign-in step (2FA).
2. Lists the account's transactions and keeps the invoices.
3. For each invoice, asks Paperless whether it already has a document whose original file name ends
   in `_<transaction number>.pdf`. Panda's own file names look like `INV100477040_70798626.pdf`,
   where `70798626` is the transaction number.
4. Downloads the missing ones and uploads them with Panda's file name, then waits for Paperless to
   consume each one. A Paperless "duplicate" result counts as done.

Anything Paperless does with the document afterwards (title, correspondent, tags) is up to your
Paperless matching rules and workflows. The file name pattern `INV[0-9]*_*.pdf` is handy for a
workflow trigger.

## Setup

Python 3.10+.

```sh
git clone https://github.com/colfin22/panda-invoice-download /opt/panda-invoice-download
cd /opt/panda-invoice-download
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
install -d -m 700 /etc/panda-invoice-download
install -m 600 .env.example /etc/panda-invoice-download/panda.env   # then edit it
```

Settings (all in the env file, see `.env.example`):

| Setting | Required | Meaning |
|---|---|---|
| `PANDA_EMAIL`, `PANDA_PASSWORD` | yes | Your my.panda.ie login |
| `PAPERLESS_URL`, `PAPERLESS_TOKEN` | yes | Paperless base URL and API token |
| `SINCE` | no | Only invoices dated on or after this day (`YYYY-MM-DD`) |
| `HTTP_TIMEOUT`, `HTTP_RETRIES` | no | Per-request timeout in seconds (120) and attempts (3); the portal can be slow |
| `STATE_DIR` | no | Where `last_success` is written (`/var/lib/panda-invoice-download`) |
| `ALERT_WEBHOOK` | no | Gets a JSON POST `{"source": ..., "message": ...}` when a run fails |

Try it without changing anything:

```sh
.venv/bin/python panda_invoice_download.py --env-file /etc/panda-invoice-download/panda.env --dry-run
```

The first real run uploads every invoice on the account that Paperless doesn't have (use `SINCE`
to limit that).

## Running it daily (systemd)

`examples/systemd/` has a service, a timer that fires every 6 hours, and a drop-in that skips a run
when one has already succeeded today. So you get one run a day, and a failed run is retried a few
hours later.

```sh
cp examples/systemd/panda-invoice-download.{service,timer} /etc/systemd/system/
cp -r examples/systemd/panda-invoice-download.service.d /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now panda-invoice-download.timer
```

## Exit codes

`0` ok · `1` a step failed (logged, and sent to `ALERT_WEBHOOK` if set) · `2` configuration error

## Notes

- The portal's `hasTransactionFile` flag is `false` even for invoices that download fine, so the
  script ignores it.
- The B2C client ID and policy name in the script are public values from the portal's web app.
  If Panda changes their sign-in, the run fails at a named step instead of uploading nothing.
- Not affiliated with Panda.

## License

MIT
