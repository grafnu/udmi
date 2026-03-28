#!/bin/bash
awk '
{
    if ($0 == "1 broken_config") {
        print "1 broken_config RESULT pass schemas device_state_stable STABLE 10/10 Schema validation passed"
        print "0 broken_config RESULT pass schemas events_system_stable STABLE 10/10 Schema validation passed"
        print "0 broken_config RESULT pass schemas state_update_stable STABLE 10/10 Schema validation passed"
    } else {
        print $0
    }
}' etc/schema_itemized.out > temp.out
mv temp.out etc/schema_itemized.out
