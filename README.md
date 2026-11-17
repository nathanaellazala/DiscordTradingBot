# Discord → Webull Alert Automation (Python + Chrome Extension)

A local automation prototype that observes newly rendered Discord messages, extracts structured options alerts, and runs an order-management workflow in **dry-run mode by default**. This project demonstrates parsing, event validation, time-sensitive processing, process state, API integration design, and automated testing.

> **Status:** Development prototype. No verified end-to-end live trading with a real Discord account or Webull brokerage account. **Do not treat this as production-ready trading software or financial advice.** Same-day options can lose their entire premium; a take-profit limit order may never fill.

## Features

- **Local Discord monitor:** Chrome extension watches rendered messages in a selected Discord channel, without Discord user tokens, bot accounts, or Discord API access.
- **Signal extraction:** Matches standalone structured alerts and qualifying entry language with an embedded alert, rejecting ambiguous or multiple signals.
- **Freshness validation:** Discord Snowflake message IDs, startup baseline, age checks, allowed-channel restrictions (required in live mode), and duplicate suppression.
- **Defensive order logic:** Entry limit price cap, monitored resting entry state, take-profit price derived from actual fills, and saved order state.
- **Safe testing:** Dry-run default, mock broker objects in Python unit tests, and separate JavaScript matcher tests.

## Architecture

```text
Discord web client (rendered messages)
                 │
                 ▼
Chrome extension: content.js + matcher.js
  new message ID / text candidate
                 │
                 ▼
background.js → localhost HTTP bridge (127.0.0.1)
                 │
                 ▼
Python signal checks (age, source channel, wording, duplicates)
                 │
                 ▼
TradingEngine → DRY RUN by default
                 │
                 └── optional Webull SDK integration (not production-validated)
```

## Repository map

| Path | Purpose |
| --- | --- |
| `discord_webull_bot.py` | Alert parsing, local bridge, safeguards, order lifecycle, CLI |
| `chrome_extension/` | Extension manifest, DOM reader, message matcher, local forwarding |
| `test_bot.py` | Parser, order schema, price rounding, mock order lifecycle |
| `test_freshness_v4.py` | Age, channel, startup baseline, duplicate and stale-message checks |
| `test_bridge_v5.py` | Loopback HTTP bridge behavior and origin validation |
| `test_embedded_v6.py` | Embedded entry language acceptance/rejection tests |
| `test_matcher_v6.js` | JavaScript candidate-matcher tests |
| `requirements.txt` | Python runtime dependencies |
| `.env.example` | Placeholders only — no real credentials |

## Setup: local dry-run

Requirements: macOS for the included launcher / screen OCR pathway, Python 3.10+ with `zoneinfo` support, Google Chrome for the extension, and (optionally) Node.js to run the JS tests. Chrome-based monitoring does not require an OCR installation; OCR mode uses Tesseract separately.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

Run an offline parser demonstration:

```bash
python3 discord_webull_bot.py --demo-alert 'SPY 750C @ .55'
```

Start a local **dry-run** browser monitor:

```bash
python3 discord_webull_bot.py --web-monitor
```

Load the `chrome_extension` directory as an unpacked Chrome extension at `chrome://extensions`, enable Developer mode, navigate to a Discord channel you have permission to view, and monitor the console and terminal diagnostics. The extension sends candidates to `http://127.0.0.1:8765`. **Do not pass `--live` for portfolio demonstrations.**

On first run, the program generates an untracked `config.json` from built-in defaults. That local configuration may later contain channel IDs and screen coordinates, so it is deliberately excluded from Git.

> Local bridge access uses an extension-origin check but is **not a cryptographic authentication system**. It should not be treated as a hardened security boundary on a machine with untrusted extensions/software.

## Test results

The following tests were run locally against this uploaded v6 source during portfolio preparation:

```bash
python3 -m unittest -q test_bot test_freshness_v4 test_bridge_v5 test_embedded_v6 test_failures
# Ran 46 tests — OK

node --test test_matcher_v6.js
# 6 tests passed
```

Tests use mocks and a localhost server; they do **not** verify actual broker fills, server reconciliation, Discord sender authenticity, or correct operation across Discord UI changes. No live orders were submitted in these tests.

## Current limitations

- Chrome's Discord DOM can change, so message detection requires manual verification after layout updates.
- No end-to-end authenticated sender verification and no hardened local message-bridge authentication.
- The trading execution path has not been proven safe under real market failures, partial fills, disconnections, or restarts.
- The sample strategy has **no stop loss** and should not be assumed profitable or safe.
- Before any production use: isolate credentials, verify order/position reconciliation, tighten bridge authentication, and complete a security and risk review.

## Portfolio notes

This repository is published to demonstrate practical Python automation and testing. The goal is to show engineering choices, defensive checks, and the ability to explain how the system operates—not to claim investment performance or broker integration validation.

## Offline failure regressions and CI

`test_failures.py` checks persisted ambiguous entry state across restart, ambiguous take-profit acknowledgement/API failure, and corrupt-state refusal. All broker calls are synthetic mocks; none prove live broker reconciliation. GitHub Actions runs the Python and JavaScript suites with no trading credentials. An unresolved entry blocks further orders, and uncertain take-profit state retains its client order ID for inspection.
