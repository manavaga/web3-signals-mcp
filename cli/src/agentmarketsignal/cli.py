"""
agentmarketsignal CLI — pipe-friendly JSON interface to the Web3 Signals API.

Layer 2 agent readiness: every command writes clean JSON to stdout so that
upstream agents / scripts can consume output via pipes.
"""
from __future__ import annotations

import json
import sys

import click
import httpx

DEFAULT_API_URL = "https://web3-signals-api-production.up.railway.app"
TIMEOUT = 30.0  # seconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _api_get(base_url: str, path: str) -> dict:
    """GET *path* from the API, return parsed JSON or exit on error."""
    url = f"{base_url.rstrip('/')}{path}"
    try:
        resp = httpx.get(url, timeout=TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        _die(f"HTTP {exc.response.status_code} from {url}")
    except httpx.RequestError as exc:
        _die(f"Request failed: {exc}")
    except json.JSONDecodeError:
        _die(f"Invalid JSON response from {url}")


def _die(msg: str) -> None:
    """Print an error object to stderr and exit 1."""
    err = {"error": msg}
    click.echo(json.dumps(err), err=True)
    sys.exit(1)


def _output(data, fmt: str) -> None:
    """Write *data* to stdout as JSON or as a simple table."""
    if fmt == "json":
        click.echo(json.dumps(data, indent=2))
    elif fmt == "table":
        _print_table(data)
    else:
        click.echo(json.dumps(data, indent=2))


def _print_table(data) -> None:
    """Best-effort human-readable table from arbitrary JSON."""
    if isinstance(data, list):
        if not data:
            click.echo("(empty)")
            return
        # List of dicts -> columnar table
        if isinstance(data[0], dict):
            keys = list(data[0].keys())
            widths = {k: max(len(k), *(len(str(row.get(k, ""))) for row in data)) for k in keys}
            header = "  ".join(k.ljust(widths[k]) for k in keys)
            click.echo(header)
            click.echo("  ".join("-" * widths[k] for k in keys))
            for row in data:
                click.echo("  ".join(str(row.get(k, "")).ljust(widths[k]) for k in keys))
        else:
            for item in data:
                click.echo(item)
    elif isinstance(data, dict):
        max_key = max(len(str(k)) for k in data) if data else 0
        for k, v in data.items():
            click.echo(f"{str(k).ljust(max_key)}  {v}")
    else:
        click.echo(data)


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group()
@click.option(
    "--api-url",
    envvar="AGENTMARKETSIGNAL_API_URL",
    default=DEFAULT_API_URL,
    show_default=True,
    help="Base URL of the Web3 Signals API.",
)
@click.option(
    "--format", "fmt",
    type=click.Choice(["json", "table"], case_sensitive=False),
    default="json",
    show_default=True,
    help="Output format. Use 'json' for machine consumption, 'table' for humans.",
)
@click.version_option(package_name="agentmarketsignal")
@click.pass_context
def cli(ctx: click.Context, api_url: str, fmt: str) -> None:
    """Web3 Signals x402 -- crypto market signals for AI agents.

    Every command outputs clean JSON to stdout by default so that agents and
    scripts can consume output via pipes.

    Paid endpoints (signals, signal) require x402 micropayments. Pass
    --private-key when x402 client-side payment is implemented (future work).
    """
    ctx.ensure_object(dict)
    ctx.obj["api_url"] = api_url
    ctx.obj["fmt"] = fmt


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@cli.command()
@click.option(
    "--private-key",
    envvar="AGENTMARKETSIGNAL_PRIVATE_KEY",
    default=None,
    help="Wallet private key for x402 payment (future — not yet wired).",
)
@click.pass_context
def signals(ctx: click.Context, private_key: str | None) -> None:
    """Fetch all 20 asset signals (x402 paid endpoint, $0.001 USDC)."""
    if private_key:
        click.echo("Note: x402 client-side payment not yet implemented.", err=True)
    data = _api_get(ctx.obj["api_url"], "/signal")
    _output(data, ctx.obj["fmt"])


@cli.command()
@click.argument("asset")
@click.option(
    "--private-key",
    envvar="AGENTMARKETSIGNAL_PRIVATE_KEY",
    default=None,
    help="Wallet private key for x402 payment (future — not yet wired).",
)
@click.pass_context
def signal(ctx: click.Context, asset: str, private_key: str | None) -> None:
    """Fetch signal for a single ASSET (e.g. BTC, ETH, SOL)."""
    if private_key:
        click.echo("Note: x402 client-side payment not yet implemented.", err=True)
    data = _api_get(ctx.obj["api_url"], f"/signal/{asset.upper()}")
    _output(data, ctx.obj["fmt"])


@cli.command()
@click.pass_context
def reputation(ctx: click.Context) -> None:
    """Fetch accuracy and reputation data (free endpoint)."""
    data = _api_get(ctx.obj["api_url"], "/api/performance/reputation")
    _output(data, ctx.obj["fmt"])


@cli.command()
@click.pass_context
def analytics(ctx: click.Context) -> None:
    """Fetch API usage analytics summary (free endpoint)."""
    data = _api_get(ctx.obj["api_url"], "/api/analytics/summary")
    _output(data, ctx.obj["fmt"])


@cli.command()
@click.pass_context
def health(ctx: click.Context) -> None:
    """Health check — verify the API is reachable."""
    data = _api_get(ctx.obj["api_url"], "/health")
    _output(data, ctx.obj["fmt"])


# ---------------------------------------------------------------------------
# Allow `python -m agentmarketsignal`
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    cli()
