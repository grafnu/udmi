[**UDMI**](../../) / [**Docs**](../) / [**Messages**](./) / [Notifs](#)

# Notifs

The `notifs` block defines notification configurations, states, models, and event streams for discrete device notifications and state transitions (such as BACnet alarm transitions). See the [Notifications Specification](../specs/notifs.md) for the underlying architectural rationale and lifecycle design.

## Metadata

**Schema Definition:** [model_notifs.json](../../schema/model_notifs.json) ([_🧬View_](../../gencode/docs/model_notifs.html))

Defines notification metadata entries keyed by an opaque 8-character hexadecimal notification identifier (`^[0-9a-f]{8}$`) in `metadata.notifs.notifs`.

## State

**Schema Definition:** [state_notifs.json](../../schema/state_notifs.json) ([_🧬View_](../../gencode/docs/state_notifs.html))

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

See the [example notification event message](../../tests/schemas/events_notifs/example.json).

## Config

**Schema Definition:** [config_notifs.json](../../schema/config_notifs.json) ([_🧬View_](../../gencode/docs/config_notifs.html))

Specifies device notification sampling parameters and target notification point mappings.
