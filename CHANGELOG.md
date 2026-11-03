# Changelog

## 0.7.0

Reliability. No change to the wire format or to what the app sends.

- Pushes go through a per-phone send queue. A slow or unreachable ntfy server now delays only the phones that use it.
- A push that ntfy didn't take is kept on disk (`outbox.json`, sealed, never the plaintext) and retried after 5 s, 30 s, 5 min, then hourly. Items older than 24 h are dropped, and a phone keeps at most 200.
- An answer retrying can't fix (most 4xx) is dropped instead of retried forever.
- A bot's database is not read again while it is unchanged (still re-read at least once a minute).
- `info` and `register` answers list the `reliable_push` feature.
- Internal: `/herald-push` dispatch is a handler table with one shared JSON parser.

## 0.6.0 and earlier

See the history of the repository this plugin continues: https://github.com/Giton22/hermes-herald-push
