[**UDMI**](../../) / [**Docs**](../) / [**Messages**](./) / [Notifs](#)

# Notifs

The `notifs` block defines notification configurations, states, models, and event streams for discrete device notifications and state transitions (such as BACnet alarm transitions). See the [Notifications Specification](../specs/notifs.md) for the underlying architectural rationale and lifecycle design.

## Metadata

**Schema Definition:** [model_notifs.json](../../schema/model_notifs.json) ([_🧬View_](../../gencode/docs/metadata.html#notifs))

Defines notification metadata entries keyed by an opaque 8-character hexadecimal notification identifier (`^[0-9a-f]{8}$`) in `metadata.notifs.notifs`.

## State

**Schema Definition:** [state_notifs.json](../../schema/state_notifs.json) ([_🧬View_](../../gencode/docs/state.html#notifs))

Notification state entries are published under `state.notifs.notif_id` as a map keyed by an opaque 8-character hexadecimal notification identifier (`^[0-9a-f]{8}$`, e.g., `42a8a8f1`), where each object ([state_notifs_notif.json](../../schema/state_notifs_notif.json)) reports:

* `ref`: Mapping for the notification target to an internal device resource (e.g. BACnet object reference).
* `current`: Current state value of the notification.
* `timestamp`: RFC 3339 UTC timestamp recording when the notification state last changed.
* `status`: Optional diagnostic status entry associated with the notification target.

See the [example notification state message](../../tests/schemas/state_notifs/example.json).

## Telemetry

**Schema Definition:** [events_notifs.json](../../schema/events_notifs.json) ([_🧬View_](../../gencode/docs/events_notifs.html))

Notification events published on `events/notifs` are structured under `notif_id` as a map keyed by an opaque 8-character hexadecimal notification identifier (`^[0-9a-f]{8}$`, e.g., `42a8a8f1`, matching the map structure of `state` and `config`), where each entry ([events_notifs_notif.json](../../schema/events_notifs_notif.json)) carries additional transition detail:

* `point`: Logical point or entity identifier associated with the notification (distinct from the opaque `notif_id` key).
* `current`: Destination state reached at the conclusion of the transition.
* `previous`: Prior state preceding the transition.
* `timestamp`: RFC 3339 UTC timestamp recording when the transition occurred.
* `transition`: Transition identifier or category describing the nature of the state change.
* `alarm`: Optional alarm-specific detail object ([events_notifs_alarm.json](../../schema/events_notifs_alarm.json)) capturing protocol-level alarm properties (such as BACnet alarm parameters):
  * `priority`: Alarm priority level (`0`–`255`, e.g., BACnet Notification Class priority).
  * `notification_class`: Alarm notification class identifier governing routing and acknowledgment behavior.
  * `event_type`: Alarm algorithm or event classification (e.g., `out_of_range`, `change_of_state`, `floating_limit`, `command_failure`).
  * `ack_required`: Boolean indicating whether operator acknowledgment is required for this alarm transition.
  * `message`: Descriptive alarm text or operator message emitted with the alarm event.

See the [example notification event message](../../tests/schemas/events_notifs/example.json).

## Config

**Schema Definition:** [config_notifs.json](../../schema/config_notifs.json) ([_🧬View_](../../gencode/docs/config.html#notifs))

Notification acknowledgments and control directives are published under `config.notifs.notif_id` as a map keyed by an opaque 8-character hexadecimal notification identifier (`^[0-9a-f]{8}$`, e.g., `42a8a8f1`), matching the map structure of `state` and `events`. The presence of a `<notif_id>` entry in `config.notifs.notif_id` indicates that the server has observed the active notification in `state.notifs.notif_id`, and each entry ([config_notifs_notif.json](../../schema/config_notifs_notif.json)) specifies:

* `event_ack`: RFC 3339 UTC transition `timestamp` (`date-time`) of the most recently received `events/notifs` message, acknowledging receipt of the notification event by the server. The device expects `event_ack` within 1 minute of sending an event; if not received within 1 minute, the device re-sends the idempotent event.
* `acknowledged`: Boolean (`boolean`) indicating that the notification condition has been acknowledged by an operator (corresponding to `ack_required`), instructing the device to clear/remove the notification from `state.notifs.notif_id`.

See the [example notification config message](../../tests/schemas/config_notifs/example.json).
