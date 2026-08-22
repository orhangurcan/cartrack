# Fork setup

This repository can be forked and configured without committing private Encar searches or Telegram credentials.

## 1. Fork and enable Actions

Fork the repository to your own GitHub account. Open the **Actions** tab in the fork and enable workflows.

## 2. Create Encar searches

Build each search normally on Encar, including model, year, mileage, price, fuel and any other filters. Copy the complete Encar search URL from the browser.

New installations must use `format_version: 2`. Version 2 uses every filter contained in the Encar URL exactly as provided; the public code does not impose a fixed model, year, mileage or price.

Example:

```json
{
  "format_version": 2,
  "timezone": "Europe/Berlin",
  "searches": [
    {
      "key": "search_1",
      "label": "My first search",
      "min_expected_results": 1,
      "search_url": "PASTE_FULL_ENCAR_SEARCH_URL_HERE"
    }
  ]
}
```

Add more objects to the `searches` array for additional searches. There is no fixed two-search limit.

`timezone` is optional and defaults to `Europe/Berlin`. Use a valid IANA timezone name if a different local time is preferred for timestamps and the daily status message.

## 3. Add repository secrets

Open **Settings → Secrets and variables → Actions → New repository secret** and add:

- `searchesjson` — the complete JSON configuration
- `telegrambot` — the Telegram bot token from BotFather
- `telegramid` — the numeric Telegram chat/user ID
- `healthcheckurl` — optional health-check endpoint

Never commit these values to the repository. Secrets from the source repository are not copied to forks.

## 4. Start the Telegram bot

Open your Telegram bot and press **Start** or send `/start` before testing. Telegram bots cannot initiate a private conversation with a user who has never started the bot.

## 5. Test the installation

Open **Actions → Cartrack → Run workflow** and run these modes in order:

1. `telegram-test` — verifies the Telegram bot token and chat ID.
2. `dry-run` — verifies every Encar search without changing the saved baseline.
3. `watch` — creates the initial baseline and saves opaque state.

The first `watch` treats all current matches as the baseline and does not report them as new. Later runs notify only listing IDs that have never been seen before.

## 6. Scheduled monitoring

The workflow is scheduled at `:07` and `:37` each hour. GitHub scheduled workflows can start later than the nominal minute when runners are busy.

`state/state.json` stores keyed hashes rather than raw Encar listing IDs. Search URLs and Telegram credentials remain in GitHub Actions secrets.

## Changing filters

Replace the `searchesjson` repository secret with the updated configuration. A configuration fingerprint change creates a fresh baseline on the next `watch`, so existing matches under the new filter are not incorrectly reported as newly listed cars.
