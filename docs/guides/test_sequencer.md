[**UDMI**](../../) / [**Docs**](../) / [**Guides**](./) / [Test_Sequencer](#)

# Test Sequencer

The `bin/test_sequencer` command will run the suite of sequence tests against the built-in reference
device (pubber). It encapsulates all the necessary functionality, and is intended to be used for
test development and system sanity checking (not against actual devices).

## Overview

Optional to clean out the filesystem before doing a run, to remove any inconsistencies with
upstream or other development environments:

`git clean -x -f -d`

Running the entire test sequence requires a properly setup and configured cloud project. The overal
run should take about 15min (as of this writing).

`bin/test_sequencer ${project_id}`

The results end up in `/tmp/sequencer.out`, and are automatically compared against `etc/sequencer.out`.

To just one run (or a few) tests, explicitly indicate them on the command line. Useful for either
iterative debugging, or else re-running a test to see if it's flaky or not.

`bin/test_sequencer ${project_id} ${test_name}...`



