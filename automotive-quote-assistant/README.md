# Automotive Quote Assistant

Sanitized portfolio edition of a conversational automotive-parts quotation assistant. It exposes typed tools for catalog search, tax-aware pricing, stock lookup, customer lookup, PDF quotation generation, and vehicle/part compatibility feedback.

## Architecture

```text
Firebird/ERP (read only) -> atomic sync -> local SQLite cache -> typed agent tools
```

The runtime queries SQLite instead of the production ERP. The sync publishes a new cache with an atomic replace only after sanity checks.

## Security

- Read-only ERP synchronization.
- No shell tool is required by the conversational profile.
- Credentials come from `FIREBIRD_USER` and `FIREBIRD_PASSWORD`.
- SQLite databases, logs, generated PDFs and `.env` files are ignored.
- The repository contains synthetic company, warehouse and pricing identifiers only.

## Features

- Deterministic product search.
- Prices calculated with `Decimal` and `ROUND_HALF_UP`.
- Tax-inclusive public prices.
- Stock by branch.
- PDF quotations.
- Vehicle compatibility confirmations in a separate SQLite database.
- Append-only feedback storage.

## Tests

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
```

## Disclaimer

This is an independent portfolio demonstration. It is not affiliated with any ERP vendor or automotive retailer. All included configuration and test data are synthetic.
