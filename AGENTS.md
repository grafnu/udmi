# Agent Guide

## Adding a new sequencer test

When asked to add a new sequencer test to UDMI, there are multiple files that must be updated to correctly integrate the test into the CI pipeline without causing `test_sequcheck` or `test_itemcheck` regressions.

1. **Implement the test:**
   - Add your test (e.g. `my_new_test`) inside the appropriate sequence class in `validator/src/main/java/com/google/daq/mqtt/sequencer/sequences/`. Use standard annotations like `@Test`, `@Feature(stage=ALPHA, bucket=SYSTEM)`, etc.
2. **Update `etc/sequencer.out`:**
   - Add the exact expected `RESULT pass ...` string. You can figure this out by looking at the existing structure or running the local tests. Make sure the file is sorted properly (e.g., run `sort etc/sequencer.out -o etc/sequencer.out`).
3. **Update `etc/local_tests.txt` (if requested):**
   - Adding it here will make the test run as part of the quick `bin/test_runlocal` validation suite.
4. **Update `etc/schema_itemized.out` and `etc/test_itemized.out`:**
   - The CI will fail the schema output format validation step if the expected itemized test reports are not generated correctly.
   - Run the full suite locally to generate these output artifacts, then copy them:
     - `cp out/test_itemized.tmp etc/test_itemized.out`
     - `cp out/schema_itemized.tmp etc/schema_itemized.out`
   - Doing this correctly requires setting up the entire local test environment first.

## Running test_sequencer locally

Running tests locally requires the entire mock environment stack, otherwise you will receive `java.io.EOFException`, `Connection refused`, and timeout errors from `mosquitto` and `pubber`.

**Follow these exact steps to run sequencer tests locally:**

```bash
# 1. Setup base prerequisites (installs pip and poetry dependencies)
bin/setup_base
bin/run_tests install_dependencies

# 2. Clone the dummy site model and prepare it
bin/clone_model
bin/registrar sites/udmi_site_model

# 3. Start local infrastructure services (etcd, mosquitto, udmis)
# Warning: This launches several background processes!
bin/start_local sites/udmi_site_model //mqtt/localhost
bin/pull_messages //mqtt/localhost > out/message_capture.log 2>&1 &

# 4. Now you can safely run a local sequencer target test:
bin/test_sequencer local full //mqtt/localhost system.my_new_test

# 5. After running tests, run validation to regenerate the itemized schema files
bin/test_itemcheck
# (Then copy the .tmp files to .out as noted in the section above)
```
