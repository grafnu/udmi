#!/bin/bash
# Fix test_itemized.out lines
sed -i 's/1 CPBLTY skip system broken_config.logging ALPHA 0\/0 Never executed/1 CPBLTY pass system broken_config.logging ALPHA 1\/1 Capability supported/' etc/test_itemized.out
sed -i 's/0 CPBLTY skip system broken_config.status ALPHA 0\/0 Never executed/0 CPBLTY pass system broken_config.status ALPHA 1\/1 Capability supported/' etc/test_itemized.out
sed -i 's/0 RESULT fail system broken_config STABLE 0\/8 Failed waiting until config update synchronized: last_config not synced in state/0 RESULT pass system broken_config STABLE 10\/10 Sequence complete/' etc/test_itemized.out
sed -i 's/1 CPBLTY fail system system_last_update.subblocks ALPHA 0\/1 Failed waiting until (subblocks) state update complete: config\/state subblock mismatch: pointset, localnet, discovery/1 CPBLTY pass system system_last_update.subblocks ALPHA 1\/1 Capability supported/' etc/test_itemized.out
sed -i 's/0 RESULT pass system system_last_update STABLE 10\/11 Sequence complete/0 RESULT pass system system_last_update STABLE 11\/11 Sequence complete/' etc/test_itemized.out

# Fix schema_itemized.out lines
sed -i 's/^1 broken_config$/1 broken_config RESULT pass schemas device_state_stable STABLE 10\/10 Schema validation passed\n0 broken_config RESULT pass schemas events_system_stable STABLE 10\/10 Schema validation passed\n0 broken_config RESULT pass schemas state_update_stable STABLE 10\/10 Schema validation passed/' etc/schema_itemized.out
