# Cartrack

Cartrack monitors private Encar search URLs with GitHub Actions and sends Telegram alerts for listings that have not been seen before.

## Design

- Encar search URLs and Telegram credentials stay in GitHub Actions secrets.
- The first successful watch creates a baseline and does not alert existing matches.
- Later watches notify only previously unseen listing IDs.
- Persisted listing IDs are stored as keyed hashes, not raw Encar IDs.
- A changed search configuration automatically creates a fresh baseline.
- Scheduled checks run at `:07` and `:37` each hour on a GitHub-hosted macOS runner.
- Pushes run syntax checks and unit tests on Ubuntu.

For fork installation and configuration, see [FORK_SETUP.md](FORK_SETUP.md).
