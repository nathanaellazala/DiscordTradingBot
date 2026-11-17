# Discord Alert Automation — Python

> **Project status:** Development / dry-run prototype. This repository documents software engineering and automation work; it is not a verified production trading system or financial advice.

## Overview

A Python-based experiment in capturing time-sensitive Discord alerts, extracting structured information, and validating an automated order workflow. The project emphasizes message freshness, duplicate prevention, defensive handling of uncertain states, and reproducible tests.

## Features demonstrated in development logs

- Parse structured options-alert messages into normalized fields.
- Monitor alerts through local screen capture/OCR and experiment with a browser-extension message monitor.
- Use freshness checks to ignore stale alerts and reduce repeated submissions.
- Support dry-run workflows for development and debugging.
- Exercise order lifecycle and parsing behavior through Python unit tests.

## System flow

```text
Discord alert
    ↓
Message capture (screen/OCR or browser monitor)
    ↓
Freshness + duplicate checks
    ↓
Alert parser / validation
    ↓
Order-workflow logic
    ↓
Dry-run logging / testable outcomes
```

## Tests

Development logs show the following test command passing nine tests:

```bash
python3 -m unittest -v test_bot.py
```

Before presenting this repo as complete, run the command against the **exact published version**, and record the actual output and environment.

## Setup

**To complete after inspecting this repository's current files.** Confirm the supported Python version, install commands, required configuration, extension setup, and exact dry-run launch command. Do not publish operational credentials or account details.

## Security and scope

- Do not commit `.env`, tokens, API keys, brokerage credentials, or customer/financial records.
- Keep example configuration files free of real secrets.
- Keep demonstrations in dry-run mode; do not claim independently verified live execution.
- Review the entire Git history, not only the latest working directory, before making an existing repository public.

## Roadmap

- [ ] Add a safe, reproducible sample-alert demo
- [ ] Verify test suite against the uploaded revision
- [ ] Document exact setup and configuration
- [ ] Add a sanitized screenshot or short demo GIF
- [ ] Document known limitations and failure handling
