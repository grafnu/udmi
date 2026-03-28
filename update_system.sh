#!/bin/bash
cat << 'REPLACE' > patch.diff
<<<<<<< SEARCH
  @Test(timeout = THREE_MINUTES_MS)
  @Feature(stage = ALPHA, bucket = SYSTEM)
  @Summary("Check that state messages aren't spuriously reported too frequently")
  public void too_much_state() {
=======
  @Test(timeout = TWO_MINUTES_MS)
  @Feature(stage = ALPHA, bucket = SYSTEM)
  @Summary("Check that the device has a valid system block")
  public void valid_system_block() {
    ensureStateUpdate();
    checkThat("system block is not null", () -> deviceState.system != null);
  }

  @Test(timeout = THREE_MINUTES_MS)
  @Feature(stage = ALPHA, bucket = SYSTEM)
  @Summary("Check that state messages aren't spuriously reported too frequently")
  public void too_much_state() {
>>>>>>> REPLACE
REPLACE
