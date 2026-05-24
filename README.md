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

> Changes to `configuration.yaml` are picked up by calling
> `notify_digest.reload` (or via Developer Tools → Services). Pending messages
> on existing digests are flushed first, then the new configuration is
> installed; no Home Assistant restart needed.

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
      title_mode: join
      title_separator: " / "
      dedupe: true

    - name: telegram_house
      target_service: notify.send_message
      target_service_data:
        entity_id: notify.telegram_bot_<id>_<chat>
      window_seconds: 30
```

| Field                 | Default    | Description                                                                |
| --------------------- | ---------- | -------------------------------------------------------------------------- |
| `name`                | required   | Digest name. Exposed as the notify entity `notify.<name>`.                 |
| `target_service`      | required   | Downstream service in `domain.service` form.                               |
| `target_service_data` | `{}`       | Extra fields merged into the downstream call (e.g. `target:`, `chat_id:`). |
| `window_seconds`      | `30`       | Coalescing window in seconds.                                              |
| `max_messages`        | `20`       | Flush early when the buffer reaches this many entries (minimum `2`).       |
| `max_buffer_seconds`  | unset      | Hard ceiling on how long a message can sit buffered (sliding-mode safety). |
| `window_mode`         | `tumbling` | `tumbling` or `sliding`.                                                   |
| `separator`           | `"\n• "`   | Joined between messages (and after the header).                            |
| `header`              | `""`       | Optional prefix prepended to the joined message.                           |
| `title_mode`          | `first`    | `first`, `last`, or `join` (deduped).                                      |
| `title_separator`     | `" / "`    | Joined between titles when `title_mode: join`.                             |
| `dedupe`              | `false`    | Drop a message if its body already exists in the current buffer.           |

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
- `notify_digest.reload` — re-read `configuration.yaml` and rebuild the digest
  set. Pending messages on existing digests are flushed first.

Pending buffers are also flushed automatically when Home Assistant shuts down,
so messages are not lost on restart.

## Ordering caveat

Buffered messages are dispatched in the order they reached the digest — strictly
FIFO, no sorting. That arrival order isn't always the chronological order of the
underlying events. Home Assistant fires automations concurrently, and a
fast-reporting source (a contact sensor on a door) can land at the buffer before
a slow-reporting source (a Z-Wave/Zigbee lock event), even if the lock event
physically happened first. With each notification going out separately the small
reorder is rarely visible; coalesced into one digest, it can be.

If strict chronological order matters, build the message in the source
automation rather than relying on coalescing — fire one `notify.send_message`
with the sequence already templated, instead of relying on multiple events
landing in the right order.

## Media

The digest is text-only. The notify entity contract (`notify.send_message`)
accepts only `message:` and `title:`, so media never reaches the buffer through
the standard path. For media in your automations, call the downstream service
directly — `whatsapp.send_video`, `notify.<provider>` with `data:`, etc. — and
use the digest entity only for text events.

## Troubleshooting

Enable debug logging to see when the buffer arms timers, deduplicates, and
flushes:

```yaml
logger:
  default: warning
  logs:
    custom_components.notify_digest: debug
```

Each digest gets its own child logger, so a busy `whatsapp_house` digest's
output can be isolated:

```yaml
logger:
  logs:
    custom_components.notify_digest.whatsapp_house: debug
```

What the log lines tell you:

- `flush (window): N message(s) → notify.foo` — timer fired and the digest
  dispatched normally.
- `flush (max_messages): …` — the buffer hit `max_messages` and forced an early
  flush.
- `flush (shutdown): …` — HA stopped and the listener drained the buffer.
- `dedupe: dropping duplicate message` — `dedupe: true` matched an existing
  entry and silently dropped this one.
- `flush (...): downstream foo.bar failed; N message(s) lost from digest 'x': ['…', …]`
  — the downstream service raised. The dropped message bodies are in the line
  itself so the content is recoverable from logs.

If a flush never seems to happen, check that `target_service` actually exists
(`Developer Tools → Services`) and that nothing else is competing for the same
notify entity name.

## Development

```bash
uv sync --all-groups      # set up the dev environment
uv run pytest tests/ -v   # run tests
uvx prek run --all-files  # run lint/format hooks
```

The repo uses Python 3.14, `uv`, `ruff`, `mypy`, and the same prek / GitHub
Actions stack as the author's other HA integrations (`ha-aruba-ap`,
`ha-cpu-capacity-integration`, `ha-recorder-tuning`).

Commits must follow the
[Conventional Commits](https://www.conventionalcommits.org/) spec —
release-please uses commit prefixes (`feat:`, `fix:`, etc.) to drive version
bumps. Install the hooks **including the commit-msg hook** (the default
`prek install` only wires up pre-commit-stage hooks):

```
uvx prek install --overwrite --hook-type pre-commit --hook-type commit-msg
```

Without `--hook-type commit-msg`, malformed commit messages won't be caught
locally — CI (`Commitlint` workflow) will still reject them.

## License

Copyright © 2026 Kenneth Baker. All rights reserved.
