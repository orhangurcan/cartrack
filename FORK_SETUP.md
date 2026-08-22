# Fork setup

This repository can be forked and configured without committing private Encar searches or Telegram credentials.

## 1. Fork and enable Actions

Fork this repository to your own GitHub account. Open the **Actions** tab in the fork and enable workflows. GitHub disables scheduled workflows by default on public forks, so this step is required.

## 2. Create your Encar searches

Build each search normally on Encar (model, year, mileage, price, fuel, etc.) and copy the complete Encar search URL from the browser.

For new installations, use `format_version: 2`. With version 2 the runner uses the filters contained in each Encar URL as-is. There is no fixed price, mileage, model, or year limit in the public code.

Example with one search:

```json
{
  "format_version": 2,
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

Add more objects to the `searches` array for additional searches. The runner is not limited to two searches.

Optional: a search may include `price_cap_10k_krw` to override only the upper price limit at runtime. Normally this is unnecessary; setting the desired price directly in Encar before copying the URL is simpler.

## 3. Add repository secrets

Go to **Settings → Secrets and variables → Actions → New repository secret** and add:

- `searchesjson` — the complete JSON configuration above
- `telegrambot` — the Telegram BotFather bot token
- `telegramid` — the numeric Telegram chat/user ID
- `healthcheckurl` — optional health-check endpoint

Do not commit these values to the repository.

The original repository's secrets are not copied to a fork. Each fork owner must create their own secrets.

## 4. Start the Telegram bot

Open your own Telegram bot and press **Start** / send `/start` before testing. Telegram bots cannot initiate a private conversation with a user who has never started the bot.

## 5. Test in this order

Open **Actions → Cartrack → Run workflow** and run:

1. `telegram-test` — verifies the bot token and chat ID.
2. `dry-run` — verifies all Encar searches without changing the saved baseline.
3. `watch` — creates the first baseline and saves opaque state.

The first `watch` treats all currently matching listings as the baseline and does **not** alert them as new. Later runs alert only never-before-seen listing IDs.

## 6. Scheduled monitoring

The workflow is scheduled at `:07` and `:37` each hour. GitHub scheduled workflows can start later than the nominal minute when runners are busy.

The saved `state/state.json` contains hashes rather than raw Encar listing IDs. Search URLs and Telegram credentials remain in GitHub Actions secrets.

## Changing filters later

Edit only the `searchesjson` repository secret. When its configuration changes, the runner detects the new fingerprint and creates a fresh baseline on the next `watch`, preventing all existing matches under the changed filter from being reported as newly listed cars.
