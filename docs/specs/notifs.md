[**UDMI**](../../) / [**Docs**](../) / [**Specs**](./) / [Notifications](#)

# Notifications (`notifs`)

## Overview

The UDMI `notifs` subsystem models discrete device notifications, condition transitions, and fault states (such as BACnet intrinsic and algorithmic alarm reporting) in a protocol-neutral form.

At a high level, `notifs` separates **notification lifecycle management** from **supplementary transition detail**:

- **Lifecycle Management (`state.notifs.notif_id`)**: Defined by [state_notifs.json](../../schema/state_notifs.json) and [state_notifs_notif.json](../../schema/state_notifs_notif.json). Represents active and managed notifications as a map keyed by unique notification identifier (`notif_id`), where each entry records:
  - `ref`: Underlying device or protocol object reference (e.g., `BV11.present_value`).
  - `current`: Current condition or state string of the notification.
  - `timestamp`: RFC 3339 UTC timestamp when the notification entered its current state.
  - `status`: Optional diagnostic or operational [Entry](../../schema/entry.json) associated with the notification.
- **Supplementary Transition Detail (`events/notifs`)**: Defined by [events_notifs.json](../../schema/events_notifs.json) and [events_notifs_notif.json](../../schema/events_notifs_notif.json). Provides a streaming channel for rich, transient metadata emitted when a transition occurs (such as transition classification, prior state, or descriptive context) without inflating the sticky `state` payload.
- **Configuration & Site Model (`config.notifs`, `metadata.notifs`)**: Defined by [config_notifs.json](../../schema/config_notifs.json) and [model_notifs.json](../../schema/model_notifs.json). Specifies expected notification targets and downlink configuration parameters.

```mermaid
flowchart LR
  ref["Device Object / Rule (ref)"] --> stateMap["state.notifs.notif_id (Sticky Lifecycle Map)"]
  ref --> eventStream["events/notifs (Supplementary Transition Detail)"]
  stateMap --> lifecycle["Cloud Lifecycle & Out-of-Order Ack Management"]
  eventStream --> audit["Transition Context & History"]
```

---

## Architectural Design Rationale

The design of `notifs` evolved through explicit evaluation of alternative modeling techniques. Future changes or extensions should preserve the invariants behind four core architectural decisions:

### 1. Why a Separate `notifs` Subsystem Instead of `pointset` Extensions?

An earlier design explored embedding state-transition metadata directly inside `pointset` point entries ([events_pointset_point.json](../../schema/events_pointset_point.json)), either inside a nested `stately` block or as peer fields alongside `present_value`.

That approach was rejected for two structural reasons:

1. **Divergent Cadence and Semantics**: `pointset` models sampled scalar telemetry (`present_value`) driven by periodic intervals (`sample_rate_sec`) or analog change-of-value thresholds. Discrete notifications follow an active lifecycle (active, acknowledged, cleared, faulted) that is orthogonal to periodic telemetry sampling. Mixing both concerns inside `pointset` couples continuous trend pipelines to discrete condition state machines.
2. **Notifications Without a 1:1 Telemetry Point**: In fieldbus protocols such as BACnet, notifications frequently originate from standalone alarm or algorithmic objects (e.g., BACnet Event Enrollment objects or controller fault summaries) rather than a trended `pointset` point. Modeling `notifs` as its own top-level subblock allows any notification source to be referenced via `ref` without fabricating synthetic telemetry points in `pointset`.

### 2. Why Generalize `alarmset` to `notifs`?

While BACnet alarm reporting is a primary driver, devices emit discrete notifications across a broader spectrum than traditional alarms — including overrides, maintenance lockouts, equipment mode transitions, and controller fault conditions. Renaming `alarmset` to `notifs` avoids baking BACnet-specific terminology or alarm-only assumptions into UDMI's core message taxonomy while directly supporting BACnet state transitions.

### 3. Why a `notif_id` Map Instead of an Ordered Array with Sequence Numbers (`event_no`)?

An intermediate design modeled notifications as an ordered JSON list (`[]`) carrying a monotonically incrementing sequence counter (`event_no`) and explicit `previous` state values to detect dropped packets and reconstruct transition history.

That list-and-counter design was replaced with a map keyed by notification identifier (`state.notifs.notif_id`) with `event_no` removed:

1. **Out-of-Order Lifecycle Operations**: Notifications are inherently asynchronous and non-FIFO once active. Notification `B` may be acknowledged or cleared by an operator or local controller while earlier notification `A` remains unacknowledged. An ordered list or linear sequence number (`event_no`) models a strictly sequential log and breaks down when representing independent lifecycle updates across concurrent notifications.
2. **Inherent Unique Identity (`notif_id`)**: Every notification managed by a device has an inherent unique identifier (`notif_id`). Using `notif_id` directly as the JSON map key provides O(1) per-notification addressing, guarantees uniqueness within a device's state document, and maps directly to cloud-side lifecycle state tables without sequence-gap reconciliation logic.

### 4. Why Split Lifecycle (`state.notifs`) from Detail (`events/notifs`)?

In UDMI, `state` messages are **sticky** (last-known-good state retained by the transport and reconciled in an idempotent manner), whereas `events` messages are **streaming** (fire-and-forget edge notifications):

- **Lifecycle belongs in `state.notifs`**: Managing active notifications requires cloud and device to converge reliably on which notifications are currently active, what `ref` they correspond to, what `current` condition they hold, and when (`timestamp`) they transitioned — even if transient MQTT packets were dropped or a downstream service restarted. Storing this minimal lifecycle record (`ref`, `current`, `timestamp`, `status`) in `state.notifs.notif_id` provides automatic eventual consistency.
- **Rich detail belongs in `events/notifs`**: Because UDMI republishes the complete `state` message whenever any state subblock changes, keeping `state.notifs.notif_id` entries concise prevents recurring bandwidth overhead. `events/notifs` is retained specifically so devices can publish richer, non-sticky contextual attributes (such as human-readable descriptions, transition causes, or snapshot parameters) at the moment a notification fires.

---

## Comparison of Evaluated Approaches

| Design Aspect | `pointset` Peer / `stately` Block | Ordered Event List (`events` + `event_no`) | Split `state.notifs.notif_id` Map + `events/notifs` (**Selected**) |
| :--- | :--- | :--- | :--- |
| **Primary Location** | `events/pointset` | `events/notifs` array (`[]`) | `state.notifs.notif_id` map (`{}`) + `events/notifs` |
| **Addressing Model** | Keyed by telemetry point name | Sequential array index + `event_no` | Keyed by unique `notif_id` |
| **Non-Point Notifications** | Requires synthetic telemetry points | Supported | Supported directly via `ref` |
| **Out-of-Order Acknowledgment** | Awkward; tied to telemetry updates | Poor; linear sequence conflicts with out-of-order updates | Native; each `notif_id` updates independently |
| **Packet Loss Resilience** | Depends on next periodic sample | Fragile; requires gap detection and replay | Self-healing via sticky `state` delivery |
| **Payload Overhead** | Bloats telemetry schema | Compact per event, no persistent snapshot | Compact sticky lifecycle state; rich detail isolated to `events/notifs` |

---

## Related Documentation

- [Notifs Message & Schema Reference](../messages/notifs.md)
- [Status Objects](../messages/status.md)
- [Messages Overview](../messages/readme.md)
