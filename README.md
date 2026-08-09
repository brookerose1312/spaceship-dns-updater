# spaceship-dns-updater

A lightweight Dynamic DNS updater for domains using [Spaceship DNS](https://www.spaceship.com/).

It monitors your public IPv4 address and reconciles selected custom `A` records when the address changes. It is designed to run continuously on a Raspberry Pi or any Docker-capable host.

## Features

- Reconciles DNS immediately on startup
- Polls the public IPv4 address every 5 minutes (configurable)
- Supports apex, wildcard, and named hosts
- Periodically verifies records even when the IP has not changed
- Adds the new address before deleting stale addresses to minimize interruption
- Refuses to modify records managed by another Spaceship product
- Supports optional email notifications and graceful shutdown
- Includes Docker deployment and a one-shot mode

## Prerequisites

1. The domain must use Spaceship nameservers. Records stored at Spaceship are inactive while the domain uses third-party nameservers.
2. In Spaceship's API Manager, create an API key and secret with these permissions:
   - `dnsrecords:read`
   - `dnsrecords:write`
3. Create custom `A` records for the hosts you want the updater to manage, or let the updater create missing records on its first run.

The updater manages custom `A` records only for the exact hosts listed in `SPACESHIP_HOST`. It will not alter Spacemail/product-managed records, other record types, or unconfigured hosts.

## Quick Start with Docker

Clone and configure the project:

```bash
git clone https://github.com/Tiaxi/spaceship-dns-updater.git
cd spaceship-dns-updater
cp .env.example .env
```

Edit `.env` with the domain, host records, and Spaceship API credentials. Then start the updater:

```bash
docker compose up -d
docker compose logs -f
```

## Quick Start without Docker

Python 3.14+ and [uv](https://docs.astral.sh/uv/) are required.

```bash
uv sync
export SPACESHIP_HOST=@,*
export SPACESHIP_DOMAIN=yourdomain.com
export SPACESHIP_API_KEY=your_api_key
export SPACESHIP_API_SECRET=your_api_secret
uv run updater.py
```

## One-Shot Update

Run one reconciliation for all configured hosts and exit:

```bash
docker compose run --rm spaceship-dns-updater --force
```

Or without Docker:

```bash
uv run updater.py --force
```

## Environment Variables

| Variable | Required | Default | Description |
|---|---:|---:|---|
| `SPACESHIP_HOST` | Yes | — | Comma-separated record names to manage (`@`, `*`, `www`, and so on) |
| `SPACESHIP_DOMAIN` | Yes | — | Fully qualified domain name, such as `example.com` |
| `SPACESHIP_API_KEY` | Yes | — | Spaceship API key |
| `SPACESHIP_API_SECRET` | Yes | — | Spaceship API secret |
| `SPACESHIP_TTL` | No | `300` | `A` record TTL in seconds, from 60 through 3600 |
| `CHECK_INTERVAL_MINUTES` | No | `5` | Public IP check interval |
| `FORCE_UPDATE_DAYS` | No | `2` | Periodic record reconciliation interval |
| `LOG_FILE` | No | *(none)* | Optional rotating log file; logs always go to stdout |
| `EMAIL_ENABLED` | No | `false` | Enable SMTP notifications |
| `EMAIL_USER` | When enabled | — | SMTP login and message sender |
| `EMAIL_PASS` | When enabled | — | SMTP password |
| `EMAIL_RECIPIENT` | When enabled | — | Notification recipient |
| `EMAIL_SMTP_HOST` | No | `smtp.gmail.com` | SMTP server hostname |
| `EMAIL_SMTP_PORT` | No | `587` | SMTP server STARTTLS port |

## Updating Multiple Hosts or Domains

Use a comma-separated list for multiple records in one domain:

```dotenv
SPACESHIP_HOST=@,*,www
```

For different domains, run separate containers with different environment files:

```bash
cp .env.example .env.domain1
cp .env.example .env.domain2
ENV_FILE=.env.domain1 docker compose -p domain1 up -d
ENV_FILE=.env.domain2 docker compose -p domain2 up -d
```

## How Record Updates Work

Spaceship's DNS API adds records and updates TTL values, but changing an `A` record address requires deleting the old record. The updater therefore:

1. Reads the domain's current DNS records.
2. Does nothing when a custom `A` record already has the desired address and TTL.
3. Otherwise, saves the desired `A` record with the current public IPv4 address and configured TTL.
4. Deletes stale custom `A` records for that configured host.

When a write is needed, saving before deletion avoids a window where the host has no address. If the cleanup request fails, the updater reports failure and retries; the new address remains present. Periodic reconciliation is read-only while the record remains correct.

See the [Spaceship public API documentation](https://docs.spaceship.dev/) for endpoint details and rate limits.

## Development

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

## License

[MIT](LICENSE)
