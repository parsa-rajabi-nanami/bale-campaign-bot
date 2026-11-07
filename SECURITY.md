# Security policy

Use the latest supported revision. Keep bot tokens, setup codes, webhook path secrets, database credentials, backups, and subscriber exports private. Rotate credentials promptly if exposed.

To report a vulnerability, use GitHub's private vulnerability reporting for this repository when available. Otherwise contact the maintainers privately through the contact method on their GitHub profile. Do not open a public issue containing an exploitable detail or personal data. Include affected revision, impact, and a reproducible description without real bot credentials.

Security-sensitive areas include admin authorization, callback/update input, Bale error handling, file downloads, CSV parsing, campaign recipient state, and secret-bearing URLs. Reports are reviewed as maintainers are available; no response-time guarantee is made.
