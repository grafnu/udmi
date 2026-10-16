
## basic_notification (ALPHA)

Check basic notification state, event verification, and combined acknowledgment

1. Wait for no notifications initially active
1. Wait for notification active in state
1. Check that notification state is offnormal
1. Wait for notification event received
1. Check that notification event matches state timestamp and alarm details
1. Update config before notification removed from state
    * Add `notifs` = { "notif_id": { "8f3a19c2": { "event_ack": `event timestamp`, "acknowledged": `true` } } }
1. Wait for notification removed from state
1. Update config before notification state cleared
    * Remove `notifs.notif_id.8f3a19c2`
1. Wait for notification state cleared

Test passed.
