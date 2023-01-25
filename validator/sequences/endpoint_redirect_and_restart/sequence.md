
## endpoint_redirect_and_restart

Reconnect the device to a new endpoint, restart, make sure it returns

1. Wait for initial last_config matches config timestamp
1. Update config before blobset phase is apply and stateStatus is null:
    * Add `blobset` = { "blobs": { "_iot_endpoint_config": { "phase": `final`, "generation": `blob generation`, "sha256": `blob data hash`, "url": `endpoint url` } } }
1. Wait for blobset phase is apply and stateStatus is null
1. Check that no interesting system status
1. Test failed: Cannot invoke "com.google.daq.mqtt.util.MessagePublisher.publish(String, String, String)" because the return value of "com.google.daq.mqtt.sequencer.SequenceBase.reflector(boolean)" is null
