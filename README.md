# Notify Digest

A Home Assistant custom integration that **coalesces notifications inside a time
window** and forwards a single digest message to a downstream notify service.
Useful when an automation tends to fire several closely-spaced notifications
(door events, alarm chatter, sensor flapping) and the recipient channel —
WhatsApp, SMS, email — would prefer one combined message over a burst.

## How it works

1. You declare one or more **digests** in `configuration.yaml`. Each digest has
   a name, a downstream `target_service` (anything with a `message:` field —
   `notify.*`, `script.*`, `whatsapp.*`, etc.), and a coalescing window.
2. Each digest is exposed as a notify entity (`notify.<digest_name>`). Callers
   use the standard `notify.send_message` action with `entity_id:` to append a
   message to the digest's buffer; the integration arms a flush timer.
3. When the window expires (or `max_messages` is hit, or `notify_digest.flush`
   is called), every buffered message is joined with `separator` and dispatched
   as a single call to `target_service`.

Two windowing modes:

- **`tumbling`** (default) — timer is fixed at the first message in the buffer.
  Predictable upper bound on flush latency.
- **`sliding`** — every new message resets the timer; flushes only after a quiet
  gap. Pair with `max_buffer_seconds` so a continuous stream still flushes
  eventually.

## Installation (HACS)

1. HACS → Integrations → ⋮ → Custom repositories.
2. Add `https://github.com/bakerkj/ha-notify-digest` as category
   **Integration**.
3. Install **Notify Digest**, restart Home Assistant.

## Installation (manual)

Copy `custom_components/notify_digest/` into your HA `config/custom_components/`
directory and restart.

## Configuration

A digest is keyed by **destination**, not by event source. Anything that should
be coalesced together (because it's all going to the same recipient) shares one
digest — typical examples are a WhatsApp group, a Telegram chat, or a notify
entity. Name digests after the channel they feed.

```yaml
notify_digest:
  digests:
    - name: whatsapp_house
      target_service: whatsapp.send_message
      target_service_data:
        target: "<group-id>@g.us"
      window_seconds: 30
      max_messages: 10
      window_mode: sliding
      max_buffer_seconds: 120
      separator: "\n• "
      header: "Recent activity:"
      title_mode: first
      dedupe: true
      media_policy: flush_then_send

    - name: telegram_house
      target_service: notify.send_message
      target_service_data:
        entity_id: notify.telegram_bot_<id>_<chat>
      window_seconds: 30
```

| Field                 | Default           | Description                                                                   |
| --------------------- | ----------------- | ----------------------------------------------------------------------------- |
| `name`                | required          | Digest name. Exposed as the notify entity `notify.<name>`.                    |
| `target_service`      | required          | Downstream service in `domain.service` form.                                  |
| `target_service_data` | `{}`              | Extra fields merged into the downstream call (e.g. `target:`, `chat_id:`).    |
| `window_seconds`      | `30`              | Coalescing window in seconds.                                                 |
| `max_messages`        | `20`              | Flush early when the buffer reaches this many entries.                        |
| `max_buffer_seconds`  | unset             | Hard ceiling on how long a message can sit buffered (sliding-mode safety).    |
| `window_mode`         | `tumbling`        | `tumbling` or `sliding`.                                                      |
| `separator`           | `"\n• "`          | Joined between messages (and after the header).                               |
| `header`              | `""`              | Optional prefix prepended to the joined message.                              |
| `title_mode`          | `first`           | `first`, `last`, or `join` (deduped, joined with `" / "`).                    |
| `dedupe`              | `false`           | Drop a message if its body already exists in the current buffer.              |
| `media_policy`        | `flush_then_send` | How to handle calls that carry a `data:` payload. See "Media handling" below. |

## Sending into a digest

```yaml
action: notify.send_message
target:
  entity_id: notify.whatsapp_house
data:
  title: "Front Door"
  message: "Front Door has been unlocked"
```

The digest's notify entity speaks the standard `notify.send_message` contract,
so any blueprint, automation, or script that targets a notify entity will work —
no integration-specific call shape is required. Door events, warnings, alarm
chatter — anything that should land in the same recipient — all target the same
entity and get coalesced together.

The integration also exposes:

- `notify_digest.flush` — flush a single digest immediately.
  ```yaml
  service: notify_digest.flush
  data:
    digest: whatsapp_house
  ```
- `notify_digest.flush_all` — flush every configured digest.

Pending buffers are also flushed automatically when Home Assistant shuts down,
so messages are not lost on restart.

## Media handling

The notify entity contract is text-only — `notify.send_message` accepts only
`message:` and `title:`. So media never enters the digest through the standard
notify path; the API itself prevents it. This is the modern HA pattern and
exactly the separation of concerns we want for coalescing.

Where the `media_policy` setting still matters: any **direct programmatic
caller** that invokes `DigestBuffer.async_add(..., data=...)` from inside
another integration. That path remains as a defensive backstop, with the
following per-digest policies:

- **`flush_then_send`** (default) — drain any pending text first (so events
  arrive in chronological order), then dispatch the media call to
  `target_service` with `data:` merged in. Best when a digest sees an occasional
  photo/video alongside text events.
- **`passthrough`** — dispatch the media call immediately without touching the
  text buffer. Lower latency for media; pending text may arrive after.
- **`drop`** — silently discard media calls. Useful when a digest is
  intentionally text-only and any media leaking through is a configuration smell
  you'd rather not see surface as a broken downstream call.

For media in your automations, **don't try to route media through the digest** —
call `whatsapp.send_video` (or whichever direct service) for media, and use
`notify.send_message` with the digest entity only for text events.

## Development

```bash
make venv      # uv sync --all-groups
make test      # uv run pytest tests/ -v
make lint      # uv run pre-commit run --all-files
```

The repo uses Python 3.14, `uv`, `ruff`, `mypy`, and the same pre-commit /
GitHub Actions stack as the author's other HA integrations (`ha-aruba-ap`,
`ha-cpu-capacity-integration`, `ha-recorder-tuning`).

## License

Copyright © 2026 Kenneth Baker. All rights reserved.
