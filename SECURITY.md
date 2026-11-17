# Security considerations

This software is a development prototype intended to be reviewed and demonstrated in **dry-run mode**.

- Never commit `.env`, `config.json`, brokerage credentials, account identifiers, or Discord access tokens. `.env.example` contains only placeholders.
- The Chrome extension does not request a Discord bot or user token, but it reads visible message text and forwards selected candidates to a localhost HTTP server.
- The local bridge checks that requests declare a `chrome-extension://` origin. **This is not strong authentication** against untrusted local programs/extensions and is not suitable as the sole protection for live financial orders.
- `--live` requires an explicit command-line activation phrase, but does **not** constitute proof of overall trading safety.
- Automated tests are simulated; no live trading account should be connected based solely on passing tests.
- Only use permitted Discord channels and comply with relevant platform terms and account permissions.
- Treat 0DTE options trading as high risk; the full premium may be lost.

## Reporting

If you find a security issue, contact the repository owner privately rather than publishing active credentials or exploitable deployment details in a public issue.
