# panda-waste-data-scraper

Collects your household waste data from the [Panda](https://www.panda.ie) customer portal
(my.panda.ie) on a schedule:

- **Invoices:** uploads the ones you don't have yet to [Paperless-ngx](https://docs.paperless-ngx.com).
  Panda doesn't email invoices; you have to sign in and download each one.
- **Bin collection weights (optional):** writes the weight of every collection to InfluxDB.

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

If InfluxDB is set up, each run also sends the bin collections
([details below](#optional-bin-collection-weights-in-influxdb)).

## Setup

Python 3.10+.

```sh
git clone https://github.com/colfin22/panda-waste-data-scraper /opt/panda-waste-data-scraper
cd /opt/panda-waste-data-scraper
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
install -d -m 700 /etc/panda-waste-data-scraper
install -m 600 .env.example /etc/panda-waste-data-scraper/panda.env   # then edit it
```

Settings (all in the env file, see `.env.example`):

| Setting | Required | Meaning |
|---|---|---|
| `PANDA_EMAIL`, `PANDA_PASSWORD` | yes | Your my.panda.ie login |
| `PAPERLESS_URL`, `PAPERLESS_TOKEN` | yes | Paperless base URL and API token |
| `SINCE` | no | Only invoices dated on or after this day (`YYYY-MM-DD`) |
| `HTTP_TIMEOUT`, `HTTP_RETRIES` | no | Per-request timeout in seconds (120) and attempts (3); the portal can be slow |
| `STATE_DIR` | no | Where `last_success` is written (`/var/lib/panda-waste-data-scraper`) |
| `ALERT_WEBHOOK` | no | Gets a JSON POST `{"source": ..., "message": ...}` when a run fails |
| `INFLUX_URL`, `INFLUX_TOKEN`, `INFLUX_ORG`, `INFLUX_BUCKET` | no | Also record bin collections in InfluxDB 2.x (see below) |
| `LIFTS_SINCE` | no | Only bin collections on or after this day (`YYYY-MM-DD`) |
| `LIFTS_MEASUREMENT`, `PANDA_TIMEZONE` | no | Measurement name (`bin_lifts`) and the time zone of the portal's collection times (`Europe/Dublin`) |

Try it without changing anything:

```sh
.venv/bin/python panda_waste_data_scraper.py --env-file /etc/panda-waste-data-scraper/panda.env --dry-run
```

The first real run uploads every invoice on the account that Paperless doesn't have (use `SINCE`
to limit that).

## Optional: bin collection weights in InfluxDB

The portal also lists every bin collection with its weight (the "Collection history" on the
waste page). If `INFLUX_URL` is set, each run writes all of them to InfluxDB as one point per bin
per collection:

```
bin_lifts,material=Waste weight_kg=13.0 <collection time>
bin_lifts,material=Recycling weight_kg=3.0 <collection time>
bin_lifts,material=Compost weight_kg=11.6 <collection time>
```

Rewriting a collection overwrites the same point, so the whole history is simply re-sent each
run. `weight_kg` is always a float. A failure here is reported like a failed invoice and does not
stop invoices being uploaded.

The portal's collection history can have gaps of months; the invoices still list the kg per billing
period for those times.

## Running it daily (systemd)

`examples/systemd/` has a service, a timer that fires every 6 hours, and a drop-in that skips a run
when one has already succeeded today. So you get one run a day, and a failed run is retried a few
hours later.

```sh
cp examples/systemd/panda-waste-data-scraper.{service,timer} /etc/systemd/system/
cp -r examples/systemd/panda-waste-data-scraper.service.d /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now panda-waste-data-scraper.timer
```

## Upgrading from 1.x

2.0.0 renames the script, the systemd units and the default folders from `panda-invoice-download`
to `panda-waste-data-scraper`. Nothing else changes. Stop and disable the old timer, then:

```sh
mv /etc/panda-invoice-download /etc/panda-waste-data-scraper
mv /var/lib/panda-invoice-download /var/lib/panda-waste-data-scraper
```

Change `STATE_DIR` in `panda.env` if it's set, clone and set up the new folder as above, install the
new units, and remove the old units and `/opt/panda-invoice-download`. Moving the state folder keeps
`no_pdf.json` and `last_success`.

## Exit codes

`0` ok · `1` a step failed (logged, and sent to `ALERT_WEBHOOK` if set) · `2` configuration error

## Notes

- Some old invoices have no PDF on Panda's side (the portal answers "Bad Request"). An invoice
  older than 30 days with no PDF is recorded in `STATE_DIR/no_pdf.json` and skipped from then on;
  a newer one counts as a failure and is retried on the next run (Panda may not have generated it yet).
- One failed invoice does not stop the run; failures are reported together at the end.
- The portal's `hasTransactionFile` flag is `false` even for invoices that download fine, so the
  script ignores it.
- The B2C client ID and policy name in the script are public values from the portal's web app.
  If Panda changes their sign-in, the run fails at a named step instead of uploading nothing.
- Not affiliated with Panda.

## License

MIT
