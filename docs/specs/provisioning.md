[**UDMI**](../../) / [**Docs**](../) / [**Specs**](./) / [Provisioning](#)

# Provisioning

Provisioning is the third phase in the overall [Onboarding](onboarding.md) flow, following [Discovery](discovery.md) and [Mapping](mapping.md).

Provisioning is the process of setting up various parts of the system to make them
functional in a given integration: for example, assigning authentication keys to
an IoT device.

## Sequence Diagram

```mermaid
sequenceDiagram
  %%{wrap}%%
  participant Devices
  participant Registry
  participant Source Repo
  participant Pipeline
  Registry->>Source Repo: Device Model
  Registry->>Pipeline: Pipeline Config
  Devices-->>Pipeline: Pointset (Telemetry)
```
