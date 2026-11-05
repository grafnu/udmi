package com.google.daq.mqtt.sequencer.sequences;

import static com.google.daq.mqtt.util.TimePeriodConstants.TWO_MINUTES_MS;
import static com.google.udmi.util.CleanDateFormat.dateEquals;
import static udmi.schema.Bucket.NOTIFS;
import static udmi.schema.FeatureDiscovery.FeatureStage.ALPHA;

import com.google.daq.mqtt.sequencer.Feature;
import com.google.daq.mqtt.sequencer.SequenceBase;
import com.google.daq.mqtt.sequencer.Summary;
import com.google.daq.mqtt.sequencer.ValidateSchema;
import com.google.daq.mqtt.sequencer.semantic.SemanticDate;
import java.util.HashMap;
import java.util.List;
import java.util.Objects;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.Before;
import org.junit.Test;
import udmi.schema.Envelope.SubFolder;
import udmi.schema.NotifNotifsConfig;
import udmi.schema.NotifNotifsEvents;
import udmi.schema.NotifNotifsState;
import udmi.schema.NotifsConfig;
import udmi.schema.NotifsEvents;

/**
 * Validate notification and alarm lifecycle functionality.
 */
public class NotifsSequences extends SequenceBase {

  private static final String NOTIFS_ALARM_TRIGGER = "notifs_alarm";
  private static final String DEFAULT_NOTIF_ID = "8f3a19c2";
  private static final String OFFNORMAL_STATE = "offnormal";
  private static final String NORMAL_STATE = "normal";
  private static final String TO_OFFNORMAL_TRANSITION = "to_offnormal";

  @Before
  public void setupExpectedParameters() {
    allowDeviceStateChange("notifs");
  }

  @Test(timeout = TWO_MINUTES_MS)
  @Feature(stage = ALPHA, bucket = NOTIFS)
  @Summary("Check basic notification state, event verification, and combined acknowledgment")
  @ValidateSchema(SubFolder.NOTIFS)
  public void basic_notification() {
    untilTrue("no notifications initially active", () -> deviceState.notifs == null
        || deviceState.notifs.notif_id == null || deviceState.notifs.notif_id.isEmpty());

    deviceConfig.system.testing.trigger = NOTIFS_ALARM_TRIGGER;
    untilTrue("notification active in state",
        () -> catchToNull(() -> deviceState.notifs.notif_id.get(DEFAULT_NOTIF_ID)) != null);

    NotifNotifsState stateNotif = deviceState.notifs.notif_id.get(DEFAULT_NOTIF_ID);
    checkThat("notification state is offnormal",
        () -> OFFNORMAL_STATE.equals(stateNotif.current) && stateNotif.timestamp != null);

    AtomicReference<NotifNotifsEvents> receivedNotifEvent = new AtomicReference<>();
    untilTrue("notification event received", () -> {
      List<NotifsEvents> events = popReceivedEvents(NotifsEvents.class);
      events.stream()
          .map(event -> catchToNull(() -> event.notif_id.get(DEFAULT_NOTIF_ID)))
          .filter(Objects::nonNull)
          .findFirst()
          .ifPresent(receivedNotifEvent::set);
      return receivedNotifEvent.get() != null;
    });

    NotifNotifsEvents eventNotif = receivedNotifEvent.get();
    checkThat("notification event matches state timestamp and alarm details", () ->
        dateEquals(stateNotif.timestamp, eventNotif.timestamp)
            && OFFNORMAL_STATE.equals(eventNotif.current)
            && NORMAL_STATE.equals(eventNotif.previous)
            && TO_OFFNORMAL_TRANSITION.equals(eventNotif.transition)
            && eventNotif.alarm != null
            && Boolean.TRUE.equals(eventNotif.alarm.ack_required));

    deviceConfig.system.testing.trigger = null;
    deviceConfig.notifs = new NotifsConfig();
    deviceConfig.notifs.notif_id = new HashMap<>();
    NotifNotifsConfig notifAck = new NotifNotifsConfig();
    notifAck.event_ack = SemanticDate.describe("event timestamp", eventNotif.timestamp);
    notifAck.acknowledged = true;
    deviceConfig.notifs.notif_id.put(DEFAULT_NOTIF_ID, notifAck);

    untilTrue("notification removed from state",
        () -> catchToNull(() -> deviceState.notifs.notif_id.get(DEFAULT_NOTIF_ID)) == null);

    deviceConfig.notifs.notif_id.remove(DEFAULT_NOTIF_ID);
    untilTrue("notification state cleared", () -> deviceState.notifs == null);
  }
}
