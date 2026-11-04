#!/usr/bin/env python3
"""Collect Panda (Ireland) household waste data: invoices to Paperless-ngx, bin weights to InfluxDB.

Panda does not email invoices; they are only available from the customer portal
(https://my.panda.ie). This script signs in to the portal the same way the web app does
(Azure AD B2C, authorization code + PKCE), lists the account's transactions, downloads every
invoice PDF Paperless does not have yet and uploads it through the Paperless REST API.

Configuration comes from environment variables, optionally loaded from a .env file
(see .env.example). Nothing is written to Paperless with --dry-run.

Exit codes: 0 ok, 1 a run step failed, 2 configuration error.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests

__version__ = "2.0.0"

LOG = logging.getLogger("panda-waste-data-scraper")

# Public values from the Panda portal's own web app (my.panda.ie), not secrets.
B2C_TENANT = "https://PandaGateway.b2clogin.com/PandaGateway.onmicrosoft.com"
B2C_POLICY = "B2C_1_BDD_singing_sign_up_dev"   # sic: Panda's production sign-in policy name
CLIENT_ID = "65362d56-6131-4630-8d93-91544097b425"
REDIRECT_URI = "https://my.panda.ie/"
SCOPE = "openid offline_access https://PandaGateway.onmicrosoft.com/api/User"
API = "https://pandagateway.azurewebsites.net/api"
USER_AGENT = f"panda-waste-data-scraper/{__version__}"


class RunError(Exception):
    def __init__(self, step: str, message: str):
        super().__init__(f"{step}: {message}")
        self.step = step


class ConfigError(Exception):
    pass


class NoPdf(RunError):
    """Panda has no PDF for this invoice (the portal answers 'Bad Request')."""


# --------------------------------------------------------------------------- config


def load_env_file(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE lines, # comments, optional quotes. Real env wins."""
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass
class Config:
    email: str
    password: str
    paperless_url: str
    paperless_token: str
    state_dir: Path
    timeout: int
    retries: int
    since: dt.date | None
    alert_webhook: str | None
    influx_url: str | None = None
    influx_token: str | None = None
    influx_org: str = ""
    influx_bucket: str = ""
    lifts_measurement: str = "bin_lifts"
    lifts_since: dt.date | None = None
    timezone: str = "Europe/Dublin"

    @property
    def lifts_enabled(self) -> bool:
        return bool(self.influx_url and self.influx_token)

    @classmethod
    def from_env(cls) -> "Config":
        missing = [k for k in ("PANDA_EMAIL", "PANDA_PASSWORD", "PAPERLESS_URL", "PAPERLESS_TOKEN")
                   if not os.environ.get(k)]
        if missing:
            raise ConfigError(f"missing required setting(s): {', '.join(missing)}")
        if os.environ.get("INFLUX_URL") and not all(
                os.environ.get(k) for k in ("INFLUX_TOKEN", "INFLUX_ORG", "INFLUX_BUCKET")):
            raise ConfigError("INFLUX_URL is set, so INFLUX_TOKEN, INFLUX_ORG and INFLUX_BUCKET are required too")
        since = os.environ.get("SINCE") or None
        try:
            since_date = dt.date.fromisoformat(since) if since else None
            lifts_since = os.environ.get("LIFTS_SINCE") or None
            lifts_since_date = dt.date.fromisoformat(lifts_since) if lifts_since else None
            timeout = int(os.environ.get("HTTP_TIMEOUT", "120"))
            retries = int(os.environ.get("HTTP_RETRIES", "3"))
        except ValueError as exc:
            raise ConfigError(f"bad setting: {exc}") from exc
        return cls(
            email=os.environ["PANDA_EMAIL"],
            password=os.environ["PANDA_PASSWORD"],
            paperless_url=os.environ["PAPERLESS_URL"].rstrip("/"),
            paperless_token=os.environ["PAPERLESS_TOKEN"],
            state_dir=Path(os.environ.get("STATE_DIR", "/var/lib/panda-waste-data-scraper")),
            timeout=timeout,
            retries=max(1, retries),
            since=since_date,
            alert_webhook=os.environ.get("ALERT_WEBHOOK") or None,
            influx_url=(os.environ.get("INFLUX_URL") or "").rstrip("/") or None,
            influx_token=os.environ.get("INFLUX_TOKEN") or None,
            influx_org=os.environ.get("INFLUX_ORG", ""),
            influx_bucket=os.environ.get("INFLUX_BUCKET", ""),
            lifts_measurement=os.environ.get("LIFTS_MEASUREMENT") or "bin_lifts",
            lifts_since=lifts_since_date,
            timezone=os.environ.get("PANDA_TIMEZONE") or "Europe/Dublin",
        )


# --------------------------------------------------------------------------- HTTP


def request(session: requests.Session, method: str, url: str, step: str, *, retries: int,
            timeout: int, **kwargs) -> requests.Response:
    """HTTP with retries on connection errors, timeouts and 5xx (the portal is slow at times)."""
    for attempt in range(1, retries + 1):
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            if attempt == retries:
                raise RunError(step, f"request failed after {retries} attempts "
                                     f"({exc.__class__.__name__})") from exc
            LOG.warning("%s: %s, retrying (%d/%d)", step, exc.__class__.__name__, attempt, retries)
        else:
            if response.status_code < 500 or attempt == retries:
                return response
            LOG.warning("%s: HTTP %d, retrying (%d/%d)", step, response.status_code, attempt, retries)
        time.sleep(min(60, 10 * attempt))
    raise RunError(step, "unreachable")


# --------------------------------------------------------------------------- Panda portal


class Panda:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.http = requests.Session()
        self.http.headers["User-Agent"] = USER_AGENT
        self.api = requests.Session()
        self.api.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    def _req(self, session, method, url, step, **kw):
        return request(session, method, url, step, retries=self.cfg.retries,
                       timeout=self.cfg.timeout, **kw)

    def login(self) -> None:
        """Azure AD B2C sign-in as the portal's web app does it: authorize (PKCE) -> submit
        credentials -> confirmed redirect carrying the code -> token endpoint."""
        LOG.info("signing in to the Panda portal")
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        base = f"{B2C_TENANT}/{B2C_POLICY}"

        step = "open sign-in page"
        page = self._req(self.http, "GET", f"{base}/oauth2/v2.0/authorize", step, params={
            "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI, "response_type": "code",
            "scope": SCOPE, "response_mode": "fragment", "code_challenge": challenge,
            "code_challenge_method": "S256", "state": secrets.token_urlsafe(16),
            "nonce": secrets.token_urlsafe(16)})
        match = re.search(r"var SETTINGS\s*=\s*(\{.*?\});", page.text, re.S)
        if page.status_code != 200 or not match:
            raise RunError(step, f"HTTP {page.status_code}, no SETTINGS block "
                                 "(Panda may have changed their sign-in page)")
        settings = json.loads(match.group(1))
        csrf, tx = settings.get("csrf"), settings.get("transId")
        if not csrf or not tx:
            raise RunError(step, "SETTINGS block without csrf/transId")

        step = "submit credentials"
        r = self._req(self.http, "POST", f"{base}/SelfAsserted", step,
                      params={"tx": tx, "p": B2C_POLICY},
                      data={"request_type": "RESPONSE", "email": self.cfg.email,
                            "password": self.cfg.password},
                      headers={"X-CSRF-TOKEN": csrf, "X-Requested-With": "XMLHttpRequest"})
        try:
            status = str(r.json().get("status"))
        except ValueError:
            status = None
        if status != "200":
            msg = ""
            try:
                msg = str(r.json().get("message") or "")
            except ValueError:
                pass
            raise RunError(step, f"rejected by Panda sign-in (status {status}) {msg}".strip())

        step = "sign-in confirmation"
        r = self._req(self.http, "GET", f"{base}/api/CombinedSigninAndSignup/confirmed", step,
                      params={"rememberMe": "false", "csrf_token": csrf, "tx": tx, "p": B2C_POLICY},
                      allow_redirects=False)
        location = r.headers.get("Location", "")
        code = parse_qs(urlparse(location).fragment).get("code", [None])[0]
        if not code:
            raise RunError(step, f"HTTP {r.status_code}, no authorization code in the redirect "
                                 "(a second sign-in step such as 2FA may have been added)")

        step = "token exchange"
        r = self._req(self.http, "POST", f"{base}/oauth2/v2.0/token", step,
                      data={"grant_type": "authorization_code", "client_id": CLIENT_ID,
                            "code": code, "redirect_uri": REDIRECT_URI,
                            "code_verifier": verifier, "scope": SCOPE},
                      headers={"Origin": "https://my.panda.ie"})
        token = r.json().get("id_token") if r.status_code == 200 else None
        if not token:
            raise RunError(step, f"HTTP {r.status_code}, no id_token")
        self.api.headers["Authorization"] = f"Bearer {token}"

    def invoices(self) -> list[dict]:
        step = "list transactions"
        r = self._req(self.api, "GET", f"{API}/domestic/Account/transactions", step)
        body = r.json() if r.status_code == 200 else {}
        if body.get("apiHttpStatus") != 200 or not isinstance(body.get("data"), list):
            raise RunError(step, f"HTTP {r.status_code}, apiHttpStatus {body.get('apiHttpStatus')}")
        # NB: the portal's hasTransactionFile flag is false even for invoices that download fine.
        return [t for t in body["data"] if t.get("transactionType") == "Invoice"]

    def lifts(self) -> list[dict]:
        """Bin collections with their weight: [{liftDate, material, weight, ...}] (portal 'Collection history')."""
        step = "list bin collections"
        r = self._req(self.api, "GET", f"{API}/domestic/Account/lifts", step)
        body = r.json() if r.status_code == 200 else {}
        if body.get("apiHttpStatus") != 200 or not isinstance(body.get("data"), list):
            raise RunError(step, f"HTTP {r.status_code}, apiHttpStatus {body.get('apiHttpStatus')}")
        return body["data"]

    def download(self, number: int) -> tuple[str, bytes]:
        step = f"invoice {number} link"
        r = self._req(self.api, "GET", f"{API}/domestic/Account/invoice-pdf", step,
                      params={"transactionNumber": number})
        body = r.json() if r.status_code == 200 else {}
        sas = (body.get("data") or {}).get("sasUri")
        if not sas:
            if r.status_code == 200 and body.get("apiHttpStatus") == 400:
                raise NoPdf(step, f"Panda has no PDF for it ({body.get('apiMessages')})")
            raise RunError(step, f"HTTP {r.status_code}, apiHttpStatus {body.get('apiHttpStatus')}, no link")
        step = f"invoice {number} download"
        pdf = self._req(self.http, "GET", sas, step)
        if pdf.status_code != 200 or not pdf.content.startswith(b"%PDF"):
            raise RunError(step, f"HTTP {pdf.status_code}, not a PDF ({len(pdf.content)} bytes)")
        name = urlparse(sas).path.rsplit("/", 1)[-1] or f"panda_{number}.pdf"
        return name, pdf.content


# --------------------------------------------------------------------------- Paperless


class Paperless:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.http = requests.Session()
        self.http.headers.update({"Authorization": f"Token {cfg.paperless_token}",
                                  "Accept": "application/json; version=9",
                                  "User-Agent": USER_AGENT})

    def _req(self, method, path, step, **kw):
        return request(self.http, method, self.cfg.paperless_url + path, step,
                       retries=self.cfg.retries, timeout=self.cfg.timeout, **kw)

    def check_filter(self) -> None:
        """Paperless ignores filters it does not know and returns every document. If the
        original-filename filter ever stopped working, every invoice would look 'already in
        Paperless' and nothing would ever be uploaded, silently. Refuse to run instead."""
        step = "check Paperless filter"
        r = self._req("GET", "/api/documents/", step,
                      params={"original_filename__icontains": "_no-such-panda-invoice-0.pdf",
                              "page_size": 1, "fields": "id"})
        if r.status_code != 200:
            raise RunError(step, f"HTTP {r.status_code} (wrong PAPERLESS_URL or token?)")
        if r.json().get("count", 0) != 0:
            raise RunError(step, "the original_filename filter matched documents it should not; "
                                 "this Paperless version may not support it")

    def has_transaction(self, number: int) -> bool:
        """Panda file names end in _<transaction number>.pdf, e.g. INV100477040_70798626.pdf."""
        step = "check Paperless"
        r = self._req("GET", "/api/documents/", step,
                      params={"original_filename__icontains": f"_{number}.pdf", "page_size": 1,
                              "fields": "id"})
        if r.status_code != 200:
            raise RunError(step, f"HTTP {r.status_code}")
        return r.json().get("count", 0) > 0

    def upload(self, name: str, content: bytes) -> str:
        step = f"upload {name}"
        r = self._req("POST", "/api/documents/post_document/", step,
                      files={"document": (name, content, "application/pdf")})
        if r.status_code != 200:
            raise RunError(step, f"HTTP {r.status_code} {r.text[:200]}")
        return r.json() if isinstance(r.json(), str) else str(r.json())

    def wait(self, task_id: str, name: str, limit: int = 600) -> str:
        """Wait for Paperless to consume the upload. Returns SUCCESS, DUPLICATE or raises."""
        step = f"consume {name}"
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            r = self._req("GET", "/api/tasks/", step, params={"task_id": task_id})
            tasks = r.json() if r.status_code == 200 else []
            task = tasks[0] if isinstance(tasks, list) and tasks else {}
            status, result = task.get("status"), str(task.get("result") or "")
            if status == "SUCCESS":
                return "SUCCESS"
            if status == "FAILURE":
                if "duplicate" in result.lower():
                    return "DUPLICATE"
                raise RunError(step, f"Paperless failed: {result[:200]}")
            time.sleep(5)
        raise RunError(step, f"not consumed within {limit}s")


# --------------------------------------------------------------------------- main


def alert(cfg: Config, message: str) -> None:
    if not cfg.alert_webhook:
        return
    try:
        requests.post(cfg.alert_webhook, json={"source": "panda-waste-data-scraper", "message": message},
                      timeout=20)
    except requests.RequestException as exc:
        LOG.error("alert webhook failed: %s", exc)


def _tag(value: str) -> str:
    return re.sub(r"([ ,=])", r"\\\1", value)


def lift_lines(cfg: Config, lifts: list[dict]) -> list[str]:
    """InfluxDB line protocol, one point per bin per collection. The portal gives local times
    without an offset; they are read in PANDA_TIMEZONE. weight_kg is ALWAYS written as a float:
    InfluxDB fixes a field's type at its first write in each shard and refuses the other type."""
    tz = ZoneInfo(cfg.timezone)
    lines = []
    for lift in lifts:
        when = dt.datetime.fromisoformat(str(lift["liftDate"]))
        if when.tzinfo is None:
            when = when.replace(tzinfo=tz)
        weight = float(lift["weight"])
        lines.append(f"{cfg.lifts_measurement},material={_tag(str(lift['material']))} "
                     f"weight_kg={weight} {int(when.timestamp())}")
    return lines


def write_lifts(cfg: Config, lines: list[str]) -> None:
    step = "write bin collections to InfluxDB"
    r = request(requests.Session(), "POST", f"{cfg.influx_url}/api/v2/write", step,
                retries=cfg.retries, timeout=cfg.timeout,
                params={"org": cfg.influx_org, "bucket": cfg.influx_bucket, "precision": "s"},
                headers={"Authorization": f"Token {cfg.influx_token}",
                         "Content-Type": "text/plain; charset=utf-8"},
                data="\n".join(lines).encode())
    if r.status_code != 204:
        raise RunError(step, f"HTTP {r.status_code} {r.text[:300]}")


NO_PDF_GRACE_DAYS = 30   # a newer invoice without a PDF may not be generated yet: retry it


def run(cfg: Config, dry_run: bool) -> tuple[int, list[str]]:
    """Returns (number of new invoices, list of failures). One failed invoice does not stop the run."""
    panda, paperless = Panda(cfg), Paperless(cfg)
    paperless.check_filter()
    panda.login()
    invoices = panda.invoices()
    if cfg.since:
        invoices = [t for t in invoices if dt.date.fromisoformat(t["transactionDate"][:10]) >= cfg.since]
    LOG.info("%d invoice(s) on the Panda account%s", len(invoices),
             f" since {cfg.since}" if cfg.since else "")
    no_pdf_file = cfg.state_dir / "no_pdf.json"
    try:
        no_pdf = set(json.loads(no_pdf_file.read_text()))
    except (OSError, ValueError):
        no_pdf = set()
    new, failures = 0, []
    for inv in sorted(invoices, key=lambda t: t["transactionDate"]):
        number, date = inv["transactionNumber"], inv["transactionDate"][:10]
        if number in no_pdf:
            LOG.debug("invoice %s (%s): Panda has no PDF (known), skipped", number, date)
            continue
        if paperless.has_transaction(number):
            LOG.debug("invoice %s (%s) already in Paperless", number, date)
            continue
        if dry_run:
            LOG.info("would download + upload invoice %s (%s, €%s)", number, date,
                     inv.get("transactionAmount"))
            new += 1
            continue
        try:
            name, content = panda.download(number)
            result = paperless.wait(paperless.upload(name, content), name)
        except NoPdf as exc:
            age = (dt.date.today() - dt.date.fromisoformat(date)).days
            if age > NO_PDF_GRACE_DAYS:
                LOG.warning("invoice %s (%s, %d days old): Panda has no PDF, recorded and skipped from now on",
                            number, date, age)
                no_pdf.add(number)
                continue
            LOG.error("%s", exc)
            failures.append(f"invoice {number} ({date}): no PDF yet")
            continue
        except RunError as exc:
            LOG.error("%s", exc)
            failures.append(f"invoice {number} ({date}): {exc}")
            continue
        LOG.info("invoice %s (%s, €%s) -> Paperless: %s (%s)", number, date,
                 inv.get("transactionAmount"), result, name)
        new += 1
    LOG.info("%s %d new invoice(s), %d failed", "would upload" if dry_run else "uploaded", new, len(failures))
    if cfg.lifts_enabled:
        try:
            lifts = panda.lifts()
            if cfg.lifts_since:
                lifts = [x for x in lifts if dt.date.fromisoformat(str(x["liftDate"])[:10]) >= cfg.lifts_since]
            lines = lift_lines(cfg, lifts)
            if dry_run:
                LOG.info("would write %d bin collection(s) to InfluxDB", len(lines))
            else:
                write_lifts(cfg, lines)
                LOG.info("wrote %d bin collection(s) to InfluxDB measurement %s", len(lines),
                         cfg.lifts_measurement)
        except (RunError, KeyError, ValueError, TypeError) as exc:
            LOG.error("bin collections: %s", exc)
            failures.append(f"bin collections: {exc}")
    if not dry_run:
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        no_pdf_file.write_text(json.dumps(sorted(no_pdf)) + "\n")
        if not failures:
            (cfg.state_dir / "last_success").write_text(dt.datetime.now().isoformat(timespec="seconds") + "\n")
    return new, failures


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env-file", type=Path, help="load settings from this .env file")
    ap.add_argument("--dry-run", action="store_true", help="list what would be uploaded, change nothing")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    try:
        if a.env_file:
            load_env_file(a.env_file)
        elif Path(".env").is_file():
            load_env_file(Path(".env"))
        cfg = Config.from_env()
    except (ConfigError, OSError) as exc:
        LOG.error("configuration: %s", exc)
        return 2
    try:
        _, failures = run(cfg, a.dry_run)
    except RunError as exc:
        LOG.error("%s", exc)
        if not a.dry_run:
            alert(cfg, f"Panda run failed at '{exc.step}': {exc}")
        return 1
    if failures:
        if not a.dry_run:
            alert(cfg, f"{len(failures)} Panda step(s) failed: " + "; ".join(failures[:5]))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
