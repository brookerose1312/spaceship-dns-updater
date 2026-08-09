"""Dynamic DNS updater for Spaceship."""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import signal
import smtplib
import ssl
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from email.message import EmailMessage
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:
    from types import FrameType

logger = logging.getLogger("spaceship-dns-updater")
shutdown_event = threading.Event()

API_BASE_URL = "https://spaceship.dev/api/v1"
REQUEST_TIMEOUT = 30
MIN_TTL = 60
MAX_TTL = 3600
IPV4_VERSION = 4


@dataclass
class EmailConfig:
    """SMTP configuration for email notifications."""

    smtp_host: str
    smtp_port: int
    user: str
    password: str = field(repr=False)
    recipient: str


@dataclass
class Config:
    """Application configuration loaded from environment variables."""

    hosts: list[str]
    domain: str
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    ttl: int = 300
    check_interval: int = 5
    force_update_days: int = 2
    log_file: str = ""
    email: EmailConfig | None = None

    @property
    def records_url(self) -> str:
        """Return the Spaceship DNS records API URL for the configured domain."""
        return f"{API_BASE_URL}/dns/records/{self.domain}"

    @property
    def api_headers(self) -> dict[str, str]:
        """Return authentication headers for the Spaceship API."""
        return {
            "X-API-Key": self.api_key,
            "X-API-Secret": self.api_secret,
        }

    @staticmethod
    def display_host(host: str, domain: str) -> str:
        """Format a single host for display (for example, '@' as the apex)."""
        return domain if host == "@" else f"{host}.{domain}"

    @property
    def hostname(self) -> str:
        """Build a display string for all configured hosts."""
        return ", ".join(self.display_host(host, self.domain) for host in self.hosts)

    @property
    def force_update_checks(self) -> int:
        """Return the number of check cycles before periodic reconciliation."""
        return self.force_update_days * 24 * 60 // self.check_interval

    @classmethod
    def from_env(cls) -> Config:
        """Create a configuration from environment variables."""
        check_interval = int(os.environ.get("CHECK_INTERVAL_MINUTES", "5"))
        if check_interval < 1:
            msg = f"CHECK_INTERVAL_MINUTES must be >= 1, got {check_interval}"
            raise ValueError(msg)

        force_update_days = int(os.environ.get("FORCE_UPDATE_DAYS", "2"))
        if force_update_days < 1:
            msg = f"FORCE_UPDATE_DAYS must be >= 1, got {force_update_days}"
            raise ValueError(msg)

        ttl = int(os.environ.get("SPACESHIP_TTL", "300"))
        if not MIN_TTL <= ttl <= MAX_TTL:
            msg = f"SPACESHIP_TTL must be between 60 and 3600, got {ttl}"
            raise ValueError(msg)

        hosts = list(
            dict.fromkeys(
                host.strip() for host in os.environ["SPACESHIP_HOST"].split(",")
            )
        )
        if any(not host for host in hosts):
            msg = "SPACESHIP_HOST contains empty value(s) after splitting on commas"
            raise ValueError(msg)

        email = None
        if os.environ.get("EMAIL_ENABLED", "false").lower() == "true":
            email = EmailConfig(
                smtp_host=os.environ.get("EMAIL_SMTP_HOST", "smtp.gmail.com"),
                smtp_port=int(os.environ.get("EMAIL_SMTP_PORT", "587")),
                user=os.environ["EMAIL_USER"],
                password=os.environ["EMAIL_PASS"],
                recipient=os.environ["EMAIL_RECIPIENT"],
            )

        return cls(
            hosts=hosts,
            domain=os.environ["SPACESHIP_DOMAIN"],
            api_key=os.environ["SPACESHIP_API_KEY"],
            api_secret=os.environ["SPACESHIP_API_SECRET"],
            ttl=ttl,
            check_interval=check_interval,
            force_update_days=force_update_days,
            log_file=os.environ.get("LOG_FILE", ""),
            email=email,
        )


def handle_signal(signum: int, _frame: FrameType | None) -> None:
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    logger.info("Received signal %s, shutting down", signum)
    shutdown_event.set()


def setup_logging(log_file: str) -> None:
    """Configure console and optional file logging."""
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s - %(message)s", datefmt="%d.%m.%Y %H:%M:%S"
    )
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    if log_file:
        file_handler = RotatingFileHandler(
            log_file,
            mode="a",
            maxBytes=5 * 1024 * 1024,
            backupCount=2,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    logger.setLevel(logging.INFO)


def get_ip_address() -> str | None:
    """Fetch and validate the current public IPv4 address from ipify."""
    try:
        response = requests.get(
            "https://api.ipify.org?format=json", timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        address = str(response.json()["ip"])
        parsed_address = ipaddress.ip_address(address)
    except KeyError, ValueError, requests.RequestException:
        logger.warning("Could not get a valid IPv4 address", exc_info=True)
        return None
    if parsed_address.version != IPV4_VERSION:
        logger.warning("Expected an IPv4 address, received %s", address)
        return None
    return address


def get_latest_logs(log_file: str) -> str:
    """Return the last 10 lines of the log file for email notifications."""
    if not log_file:
        return "(file logging disabled)"
    try:
        with Path(log_file).open() as file:
            return "".join(deque(file, 10))
    except FileNotFoundError:
        return "(no log file available)"


def _response_error(response: requests.Response) -> str:
    """Return a concise API error without including credentials."""
    try:
        body = response.json()
    except requests.JSONDecodeError:
        body = None
    if isinstance(body, dict) and body.get("detail"):
        return f"HTTP {response.status_code}: {body['detail']}"
    return f"HTTP {response.status_code}: {response.text[:200] or 'empty response'}"


def get_dns_records(config: Config) -> list[dict[str, Any]] | None:
    """Fetch all DNS records for the configured domain."""
    records: list[dict[str, Any]] = []
    skip = 0

    while True:
        try:
            response = requests.get(
                config.records_url,
                headers=config.api_headers,
                params={"take": 500, "skip": skip, "orderBy": "name"},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException:
            logger.exception("Could not reach the Spaceship DNS API")
            return None

        if not response.ok:
            logger.warning(
                "Could not read Spaceship DNS records: %s", _response_error(response)
            )
            return None

        try:
            payload = response.json()
            items = payload["items"]
            total = int(payload["total"])
        except KeyError, TypeError, ValueError, requests.JSONDecodeError:
            logger.warning("Spaceship returned an invalid DNS records response")
            return None
        if not isinstance(items, list) or not all(
            isinstance(item, dict) for item in items
        ):
            logger.warning("Spaceship returned an invalid DNS records response")
            return None

        records.extend(items)
        if len(records) >= total or not items:
            return records
        skip += len(items)


def _is_custom_record(record: dict[str, Any]) -> bool:
    """Return whether a record is custom rather than Spaceship product-managed."""
    group = record.get("group")
    if group is None:
        return True
    if isinstance(group, dict):
        return group.get("type") == "custom"
    return group == "custom"


def _save_a_record(config: Config, host: str, ip_address: str) -> bool:
    """Add an A record, or refresh its configured TTL if it already exists."""
    try:
        response = requests.put(
            config.records_url,
            headers=config.api_headers,
            json={
                "force": False,
                "items": [
                    {
                        "type": "A",
                        "name": host,
                        "address": ip_address,
                        "ttl": config.ttl,
                    }
                ],
            },
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException:
        logger.exception("Could not reach the Spaceship DNS API while saving a record")
        return False
    if not response.ok:
        logger.warning(
            "Could not save Spaceship DNS record: %s", _response_error(response)
        )
        return False
    return True


def _delete_a_records(config: Config, host: str, records: list[dict[str, Any]]) -> bool:
    """Delete the supplied stale A records."""
    body = [
        {"type": "A", "name": host, "address": str(record["address"])}
        for record in records
    ]
    try:
        response = requests.delete(
            config.records_url,
            headers=config.api_headers,
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException:
        logger.exception("Could not reach the Spaceship DNS API while deleting records")
        return False
    if not response.ok:
        logger.warning(
            "Could not delete stale Spaceship DNS record(s): %s",
            _response_error(response),
        )
        return False
    return True


def update_dyndns(config: Config, host: str, ip_address: str) -> bool:
    """Reconcile a custom Spaceship A record with the public IPv4 address."""
    display = Config.display_host(host, config.domain)
    if host not in config.hosts:
        logger.error("Refusing to manage unconfigured host %s", display)
        return False
    logger.info("Reconciling Spaceship DNS for %s", display)

    records = get_dns_records(config)
    if records is None:
        return False

    matching = [
        record
        for record in records
        if str(record.get("type", "")).upper() == "A"
        and str(record.get("name", "")).casefold() == host.casefold()
    ]
    managed = [record for record in matching if not _is_custom_record(record)]
    if managed:
        groups = sorted(
            {
                str(record.get("group", {}).get("type", "unknown"))
                if isinstance(record.get("group"), dict)
                else str(record.get("group", "unknown"))
                for record in managed
            }
        )
        logger.error(
            "Cannot manage %s because Spaceship owns matching %s record(s)",
            display,
            ", ".join(groups),
        )
        return False

    custom = [record for record in matching if _is_custom_record(record)]
    stale = [record for record in custom if record.get("address") != ip_address]
    current = [record for record in custom if record.get("address") == ip_address]
    needs_save = not any(record.get("ttl") == config.ttl for record in current)

    # When a save is needed, do it before deleting stale values to avoid a gap where
    # the host has no A record. An exact address-and-TTL match needs no API write.
    if needs_save and not _save_a_record(config, host, ip_address):
        return False
    if stale and not _delete_a_records(config, host, stale):
        return False

    if stale:
        logger.info(
            "Updated %s to %s and removed %d stale record(s)",
            display,
            ip_address,
            len(stale),
        )
    elif needs_save:
        logger.info("Updated %s to %s with TTL %d", display, ip_address, config.ttl)
    else:
        logger.info(
            "%s is current at %s with TTL %d; no changes needed",
            display,
            ip_address,
            config.ttl,
        )
    return True


def send_email(config: Config, ip_address: str, *, results: dict[str, bool]) -> None:
    """Send an email notification about DNS reconciliation results."""
    if not config.email:
        return
    all_ok = all(results.values())
    status = "succeeded" if all_ok else "FAILED"
    subject = f"Spaceship DNS update {status} for {config.hostname}"
    latest_logs = get_latest_logs(config.log_file)
    host_lines = "\n".join(
        f"  {host} — {'succeeded' if ok else 'FAILED'}" for host, ok in results.items()
    )
    body = (
        f"IP address: {ip_address}\n"
        f"Hosts updated:\n{host_lines}\n\n"
        f"Recent logs:\n{latest_logs}"
    )

    message = EmailMessage()
    message["From"] = config.email.user
    message["To"] = config.email.recipient
    message["Subject"] = subject
    message.set_content(body)

    try:
        with smtplib.SMTP(config.email.smtp_host, config.email.smtp_port) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.login(config.email.user, config.email.password)
            server.send_message(message)
        logger.info("Sent notification to %s", config.email.recipient)
    except smtplib.SMTPException:
        logger.exception("Failed to send notification email")


def reconcile_hosts(config: Config, ip_address: str) -> dict[str, bool]:
    """Reconcile all configured hosts with an IPv4 address."""
    return {host: update_dyndns(config, host, ip_address) for host in config.hosts}


def run_force_update(config: Config) -> None:
    """Run a single DNS reconciliation for all hosts and exit."""
    ip_address = get_ip_address()
    if not ip_address:
        logger.error("Could not determine IP address, aborting")
        sys.exit(1)
    logger.info("Current IP address is %s", ip_address)
    results = reconcile_hosts(config, ip_address)
    send_email(config, ip_address, results=results)
    sys.exit(0 if all(results.values()) else 1)


def run_polling_loop(config: Config) -> None:
    """Continuously monitor IP changes and reconcile Spaceship DNS."""
    logger.info("Check interval: %d minutes", config.check_interval)
    logger.info("Periodic reconciliation interval: %d days", config.force_update_days)
    synced_ip: str | None = None
    checks = 0

    while not shutdown_event.is_set():
        ip_address = get_ip_address()
        if ip_address:
            logger.info(
                "Current IP address is %s (check %d/%d)",
                ip_address,
                checks,
                config.force_update_checks,
            )
            periodic = checks >= config.force_update_checks
            if synced_ip != ip_address or periodic:
                if synced_ip is None:
                    logger.info("Synchronizing DNS on startup to %s", ip_address)
                elif periodic:
                    logger.info(
                        "Running periodic reconciliation after %d checks", checks
                    )
                else:
                    logger.info("IP address changed: %s -> %s", synced_ip, ip_address)

                results = reconcile_hosts(config, ip_address)
                if all(results.values()):
                    synced_ip = ip_address
                    checks = 0
                    send_email(config, ip_address, results=results)
                else:
                    if periodic:
                        send_email(config, ip_address, results=results)
                    logger.error("Update failed, will retry")

        Path("/tmp/healthcheck").touch()  # noqa: S108
        checks += 1
        if shutdown_event.wait(timeout=config.check_interval * 60):
            break

    logger.info("Shutdown complete")


def main() -> None:
    """Parse arguments and run the updater."""
    parser = argparse.ArgumentParser(description="Spaceship Dynamic DNS Updater")
    parser.add_argument(
        "--force", action="store_true", help="Run a single reconciliation and exit"
    )
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    config = Config.from_env()
    setup_logging(config.log_file)
    logger.info("spaceship-dns-updater started")

    if args.force:
        run_force_update(config)
    else:
        run_polling_loop(config)


if __name__ == "__main__":
    main()
