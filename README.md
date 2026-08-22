# Cartrack

Cartrack monitors private Encar search URLs with GitHub Actions and sends Telegram alerts for listings that have not been seen before.

## Design

- Search URLs and Telegram credentials stay in GitHub Actions secrets.
- The first successful watch creates a baseline; an empty result set is a valid baseline.
- Later watches notify only previously unseen listing IDs.
- Runtime state is authenticated-encrypted and stored on a dedicated `state` branch, not on `main`.
- Unchanged checks do not create state commits.
- Search configuration is fingerprinted semantically, so labels, timezone changes and JSON formatting do not reset listing history.
- Scheduled checks run at `:07` and `:37` each hour on a GitHub-hosted macOS runner.
- Pushes run syntax checks and unit tests on Ubuntu.
- GitHub Actions dependencies are pinned to immutable commit SHAs.

Cartrack uses an unofficial Encar web endpoint and is not affiliated with or endorsed by Encar. The endpoint may change without notice.

For fork installation and configuration, see [FORK_SETUP.md](FORK_SETUP.md).
