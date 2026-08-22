# Fork setup

This repository can be forked and configured without committing private Encar searches or Telegram credentials.

## 1. Fork and enable Actions

Fork the repository to your own GitHub account. Open the **Actions** tab in the fork and enable workflows.

## 2. Create Encar searches

Build each search normally on Encar and copy the complete Encar search URL from the browser.

New installations must use `format_version: 2`. Version 2 uses every filter contained in the Encar URL exactly as provided.

```json
{
  "format_version": 2,
  "timezone": "Europe/Berlin",
  "searches": [
    {
      "key": "search_1",
      "label": "My first search",
      "search_url": "PASTE_FULL_ENCAR_SEARCH_URL_HERE"
    }
  ]
}
```

Add more objects to `searches` for additional searches. `key` must be unique and should stay stable over time. `label` can be changed without resetting listing history.

An empty result set is valid. If you want a defensive lower-bound check, add `"min_expected_results": 1` (or a higher number) to a search. Do not set it unless zero results should truly be considered abnormal.

`timezone` is optional and defaults to `Europe/Berlin`.

## 3. Add repository secrets

Open **Settings → Secrets and variables → Actions → New repository secret** and add:

- `searchesjson` — the complete JSON configuration
- `telegrambot` — the Telegram bot token from BotFather
- `telegramid` — the numeric Telegram chat/user ID
- `statekey` — recommended: a long random secret used only for encrypted runtime state

Optional health monitoring:

- `healthcheck_success_url` — pinged after a successful scheduled check
- `healthcheck_failure_url` — pinged after a failed scheduled check

For compatibility, `healthcheckurl` is also accepted; its `/fail` endpoint is used for failures. A dead-man healthcheck is strongly recommended because no application can send its own warning if GitHub scheduling stops entirely.

Never commit secret values. Secrets from the source repository are not copied to forks.

If `statekey` is absent, Cartrack derives state protection from the Telegram bot token so existing installations continue to work. New installations should use a dedicated `statekey`.

## 4. Start the Telegram bot

Open your Telegram bot and press **Start** or send `/start` before testing.

## 5. Test the installation

Open **Actions → Cartrack → Run workflow** and run these modes in order:

1. `telegram-test` — verifies the Telegram bot token and chat ID.
2. `dry-run` — verifies every Encar search without changing the baseline.
3. `watch` — creates the initial encrypted baseline.

A zero-result first watch still creates a valid empty baseline, so the first future matching listing will be notified.

## 6. Scheduled monitoring

The workflow is scheduled at `:07` and `:37` each hour. GitHub scheduled workflows can start late during periods of high load and public-repository schedules can be disabled after prolonged repository inactivity. Use an external dead-man healthcheck if missed runs matter.

Runtime state is stored as authenticated encrypted data on the `state` branch. The application branch does not publish result counts, timestamps or listing hashes.

## Changing filters

Replace `searchesjson` with the updated configuration. A semantic filter change creates a fresh baseline. Changing only labels, timezone or JSON formatting does not reset the seen-listing history.
