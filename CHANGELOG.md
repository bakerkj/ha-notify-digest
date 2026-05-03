# Changelog

## 0.0.2

**Breaking change**

- The `media_policy` digest field has been removed. The notify-entity API is
  text-only, so media never actually reached the buffer through the standard
  path; the policy was paying maintenance cost for an unreachable code path.
  If you have `media_policy:` in `configuration.yaml`, delete the line — the
  schema now rejects it as an unknown key. Send media by calling the
  downstream service directly (e.g. `whatsapp.send_video`) instead of routing
  it through the digest.

**Improvements**

- `max_messages` flushes are now dispatched in the background. A user
  automation that fills the buffer no longer waits for the downstream service
  to ack before continuing.
- Downstream-call failures during a flush log the lost message bodies before
  re-raising, so content is recoverable from logs.
- Per-digest shutdown flush is bounded by a 10 s timeout so a hung downstream
  cannot stall Home Assistant's stop sequence.
- Schema floor for `max_messages` raised from `1` to `2` (a digest of `1` is a
  no-op).
- New `title_separator` field controls the joiner used when
  `title_mode: join`. Default `" / "`.
- New "Ordering caveat" and "Troubleshooting" sections in the README.

## 0.0.1

Initial release.
