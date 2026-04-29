[**UDMI**](../../) / [**Docs**](../) / [**Specs**](./) / [UUFI](#)

# Unified UDMI Functional Interface (UUFI)

The **Unified UDMI Functional Interface (UUFI)** is a specification for external applications to integrate with a UDMI-managed system. It formalizes the communication channel between an external application (the **Client**) and the UDMI cloud infrastructure (the **System**) using a standardized PubSub mechanism.

UUFI provides a "clean room" interface for programmatic control of UDMI operations, including device management, telemetry consumption, and command injection, all while adhering to the standard UDMI schemas.

## 1. Architecture Overview

UUFI utilizes a **PubSub** transport where the Client interacts with the System via dedicated topics and subscriptions. This connection acts as a gateway for all UDMI messages.

*   **Managed Registry:** The actual IoT registry containing physical or virtual devices being managed.
*   **System Interface:** The set of PubSub topics provided by the UDMI Cloud Functions to handle UUFI traffic.

### Message Flow
- **Publish (into UDMI):** The Client publishes a UDMI message to the `udmi_reflect` topic. The message is wrapped in a UUFI Envelope.
- **Receive (from UDMI):** The System delivers messages from managed devices to the Client via a subscription to the `udmi_reply` topic. Messages are encapsulated in a UUFI Envelope.

## 2. Connectivity and Authentication

The Client must have access to the GCP project where the UDMI system is deployed.

### Connection Parameters
- **Project ID:** The GCP project ID.
- **Publish Topic:** `udmi_reflect` (or a namespace-prefixed version like `prefix-udmi_reflect`).
- **Receive Subscription:** A subscription to the `udmi_reply` topic (e.g., `prefix-udmi_reply-user_id`).

### Authentication
Authentication is handled via standard **GCP IAM**. The Client must have a service account or user identity with the following permissions:
- `pubsub.publisher` on the `udmi_reflect` topic.
- `pubsub.subscriber` on the `udmi_reply` subscription.

### Handshake Protocol
Upon connection, the Client must perform a handshake to synchronize with the System.

1.  **State Declaration:** The Client publishes a UDMI `state` message to the `udmi_reflect` topic. This message must include a `udmi` subfolder with a `setup` block (see `state_udmi.json`).
    -   `functions_ver`: The version of the UDMI functions the Client expects.
    -   `transaction_id`: A unique ID for the handshake transaction.
2.  **Configuration Confirmation:** The System responds via the `udmi_reply` subscription by updating the Client's `config`. This message includes a `udmi` subfolder (see `config_udmi.json`) containing:
    -   `setup`: System version information (min/max supported function versions).
    -   `reply`: A copy of the Client's setup block to confirm receipt.

The Client is considered **Active** only after receiving a matching configuration reply.

## 3. Message Encapsulation

All messages exchanged via UUFI are wrapped in a **UUFI Envelope**. In PubSub, the envelope fields are typically mapped to **PubSub Attributes**, while the UDMI message body is the **PubSub Data**.

### Envelope Attributes
The following attributes must be present in every PubSub message:
- `projectId`: The GCP project ID.
- `deviceRegistryId`: The `registry_id` of the Managed Registry.
- `deviceId`: The target or source device ID in the Managed Registry (e.g., `BLD-1`, `_validator`).
- `subFolder`: The UDMI subfolder (e.g., `pointset`, `system`, `validation`).
- `subType`: The UDMI message type (e.g., `events`, `state`, `config`, `commands`).
- `transactionId`: A unique string used to track requests and responses.
- `publishTime`: RFC 3339 timestamp of when the message was wrapped.
- `source`: An identifier for the Client (e.g., `-user_id`).

### Publishing a Message (into UDMI)
To send a message to a managed device:
1.  Construct the UDMI message (e.g., a `config` object).
2.  Prepare a PubSub message:
    - Set the **Attributes** according to the Envelope schema.
    - Set the **Data** to the JSON-encoded UDMI message.
3.  Publish to the `udmi_reflect` topic.

### Receiving a Message (from UDMI)
When a message arrives from a managed device:
1.  Receive a PubSub message from the `udmi_reply` subscription.
2.  Extract the **Attributes** to identify the source device, subfolder, and type.
3.  The **Data** is the original UDMI message from the device.

## 4. Operational Commands

UUFI supports direct operations on the Cloud Model by setting specific attributes.

### Cloud Model Queries
- Set `subFolder: cloud` and `subType: query`.
- **Payload:** A `CloudModel` object with `operation: READ`.

### Cloud Model Updates
- Set `subFolder: cloud` and `subType: model`.
- **Payload:** A `CloudModel` object specifying the `operation` (e.g., `CREATE`, `UPDATE`, `DELETE`, `BIND`, `UNBIND`).

## 5. Mapping UDMI to UUFI Envelopes

| UDMI Operation | Envelope `subType` | Envelope `subFolder` | Direction |
| :--- | :--- | :--- | :--- |
| Device Config Update | `config` | *varies* (e.g., `pointset`) | Publish |
| Device State Event | `state` | *varies* (e.g., `system`) | Receive |
| Device Telemetry | `events` | `pointset` | Receive |
| Device Discovery | `events` | `discovery` | Receive |
| Handshake State | `state` | `udmi` | Publish |
| Handshake Config | `config` | `udmi` | Receive |

## 7. Examples

The following examples demonstrate how to format PubSub messages for common UUFI operations, grouped by logical exchange.

### 7.1. Handshake Exchange
The handshake synchronizes the Client and the System upon connection.

#### Step 1: Publish Handshake State
The Client initiates the session.

**PubSub Attributes:**
```json
{
  "projectId": "my-gcp-project",
  "deviceRegistryId": "my-managed-registry",
  "deviceId": "my-managed-registry",
  "subFolder": "udmi",
  "subType": "state",
  "transactionId": "UUFI:sess123:001",
  "source": "-my-user-id"
}
```

**PubSub Data (JSON):**
```json
{
  "version": "1.5.2",
  "timestamp": "2026-04-29T10:00:00Z",
  "udmi": {
    "setup": {
      "functions_ver": 9,
      "transaction_id": "UUFI:sess123:001",
      "msg_source": "-my-user-id",
      "user": "my-user-id"
    }
  }
}
```

#### Step 2: Receive Handshake Config
The System confirms the session is active.

**PubSub Attributes:**
```json
{
  "projectId": "my-gcp-project",
  "deviceRegistryId": "my-managed-registry",
  "deviceId": "my-managed-registry",
  "subFolder": "udmi",
  "subType": "config",
  "transactionId": "UUFI:sess123:001"
}
```

**PubSub Data (JSON):**
```json
{
  "version": "1.5.2",
  "timestamp": "2026-04-29T10:00:01Z",
  "udmi": {
    "setup": {
      "functions_min": 9,
      "functions_max": 9,
      "udmi_version": "1.5.2"
    },
    "reply": {
      "functions_ver": 9,
      "transaction_id": "UUFI:sess123:001",
      "msg_source": "-my-user-id"
    }
  }
}
```

### 7.2. Pointset Exchange
Interaction with a device's points (e.g., sensors and setpoints).

#### Action: Publish Config Update
Updating the `room_temperature` setpoint for device `BLD-1`.

**PubSub Attributes:**
```json
{
  "projectId": "my-gcp-project",
  "deviceRegistryId": "my-managed-registry",
  "deviceId": "BLD-1",
  "subFolder": "pointset",
  "subType": "config",
  "transactionId": "UUFI:sess123:002",
  "source": "-my-user-id"
}
```

**PubSub Data (JSON):**
```json
{
  "version": "1.5.2",
  "timestamp": "2026-04-29T10:05:00Z",
  "points": {
    "room_temperature": {
      "set_value": 22.5
    }
  }
}
```

#### Action: Receive Telemetry Event
Receiving the current `room_temperature` reading from device `BLD-1`.

**PubSub Attributes:**
```json
{
  "projectId": "my-gcp-project",
  "deviceRegistryId": "my-managed-registry",
  "deviceId": "BLD-1",
  "subFolder": "pointset",
  "subType": "events",
  "publishTime": "2026-04-29T10:06:00Z"
}
```

**PubSub Data (JSON):**
```json
{
  "version": "1.5.2",
  "timestamp": "2026-04-29T10:06:00Z",
  "points": {
    "room_temperature": {
      "present_value": 22.1
    }
  }
}
```
