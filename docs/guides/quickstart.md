# UDMI Quickstart Guide

`git clone git@github.com:faucetsdn/udmi.git`

All commands from here on out are expeted to be run in the `udmi` directory that was just created.

## Setup

* `bin/setup_base`: Makes sure basic system packages and python venv are properly setup.
* `bin/clone_model`: Make a local clone of the default `udmi_site_model` for testing.
* `bin/start_local sites/udmi_site_model //mqtt/localhost`: Runs local `mqtt`, `etcd`, and `udmis` servers
  to provide all the requisite backend functionality.

## Registrar

Should do one of the following (they both aren't necessary to proceed to later steps):
* `bin/test_regclean //mqtt/localhost`: Runs a variety of `registrar` invocations to test that everything
  is setup and functioning correctly.
* `bin/registrar sites/udmi_site_model //mqtt/localhost`: Runs an explicit instance of `registrar` to
  registrar the given site model with the target project.

## Pubber

For the basic versions of validator and sequencer (so no `test_` prefix), there needs to be a pubber
instance running (in a separate terminal window):

* `bin/pubber sites/udmi_site_model //mqtt/localhost AHU-1 742132`: Runs a standalone pubber reference
  instance, will not (normally) terminate.

## Validator

* `bin/test_validator //mqtt/localhost`: Runs some test `pubber` instances and validates them for a while,
  making sure everything is working-as-intended.
* `bin/validator sites/udmi_site_model //mqtt/localhost`: Runs the raw `validator` that will listen to all
  devices communicating with the given site (requires a running `pubber` instance for testing).

## Sequencer

* `bin/test_sequencer //mqtt/localhost`: Runs `sequencer` tests against a spawned `pubber` instance. Takes
  a while. Generally there to just make sure everything is working-as-intended.
* `bin/sequencer sites/udmi_site_model //mqtt/localhost AHU-1`: Runs sequencer against a specific target
  device (requires running separate `pubber` instance for testing).
