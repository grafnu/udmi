package udmi.lib.client.manager;

import static com.google.udmi.util.CleanDateFormat.cleanDate;
import static com.google.udmi.util.CleanDateFormat.dateEquals;
import static com.google.udmi.util.GeneralUtils.getNow;
import static com.google.udmi.util.JsonUtil.isoConvert;
import static java.lang.String.format;

import java.util.ArrayList;
import java.util.Date;
import java.util.HashMap;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.concurrent.ScheduledFuture;
import udmi.lib.intf.SubBlockManager;
import udmi.schema.AlarmNotifsEvents;
import udmi.schema.NotifNotifsConfig;
import udmi.schema.NotifNotifsEvents;
import udmi.schema.NotifNotifsState;
import udmi.schema.NotifsConfig;
import udmi.schema.NotifsEvents;
import udmi.schema.NotifsState;

/**
 * Notifications client manager.
 */
public interface NotifsManager extends SubBlockManager {

  String NOTIFS_ALARM_TRIGGER = "notifs_alarm";
  String DEFAULT_NOTIF_ID = "8f3a19c2";
  String DEFAULT_NOTIF_POINT = "filter_alarm_pressure_status";
  String DEFAULT_NOTIF_REF = "BV/11#present_value";
  String OFFNORMAL_STATE = "offnormal";
  String NORMAL_STATE = "normal";
  String TO_OFFNORMAL_TRANSITION = "to_offnormal";
  String CHANGE_OF_STATE_EVENT_TYPE = "change_of_state";
  String DEFAULT_ALARM_MESSAGE = "Filter differential pressure alarm active";
  int DEFAULT_ALARM_PRIORITY = 3;
  int DEFAULT_NOTIFICATION_CLASS = 1;
  long EVENT_ACK_TIMEOUT_MS = 60 * 1000L;

  NotifsConfig getNotifsConfig();

  void setNotifsConfig(NotifsConfig notifsConfig);

  NotifsState getNotifsState();

  Map<String, NotifNotifsEvents> getPendingEvents();

  Set<String> getAckedEvents();

  String getPreviousTrigger();

  void setPreviousTrigger(String trigger);

  ScheduledFuture<?> scheduleFuture(Date futureTime, Runnable futureTask);

  /**
   * Update notification state in device state holder.
   */
  default void updateState() {
    NotifsState state = getNotifsState();
    boolean hasActiveNotifs = state != null && state.notif_id != null && !state.notif_id.isEmpty();
    updateState(hasActiveNotifs ? state : NotifsState.class);
  }

  /**
   * Trigger a synthetic alarm notification and emit the corresponding event.
   *
   * @param notifId Opaque 8-char hex notification identifier.
   */
  default void triggerAlarm(String notifId) {
    Date transitionTime = cleanDate(getNow());

    NotifNotifsState stateEntry = new NotifNotifsState();
    stateEntry.ref = DEFAULT_NOTIF_REF;
    stateEntry.current = OFFNORMAL_STATE;
    stateEntry.timestamp = transitionTime;
    getNotifsState().notif_id.put(notifId, stateEntry);

    NotifNotifsEvents eventEntry = new NotifNotifsEvents();
    eventEntry.point = DEFAULT_NOTIF_POINT;
    eventEntry.current = OFFNORMAL_STATE;
    eventEntry.previous = NORMAL_STATE;
    eventEntry.timestamp = transitionTime;
    eventEntry.transition = TO_OFFNORMAL_TRANSITION;
    eventEntry.alarm = new AlarmNotifsEvents();
    eventEntry.alarm.priority = DEFAULT_ALARM_PRIORITY;
    eventEntry.alarm.notification_class = DEFAULT_NOTIFICATION_CLASS;
    eventEntry.alarm.event_type = CHANGE_OF_STATE_EVENT_TYPE;
    eventEntry.alarm.ack_required = true;
    eventEntry.alarm.message = DEFAULT_ALARM_MESSAGE;

    getPendingEvents().put(notifId, eventEntry);
    getAckedEvents().remove(notifId);

    updateState();
    publishNotifsEvent(notifId, eventEntry);
  }

  /**
   * Publish a notification event and schedule a 1-minute timeout for idempotent retry.
   *
   * @param notifId    Opaque notification identifier.
   * @param eventEntry Notification event payload.
   */
  default void publishNotifsEvent(String notifId, NotifNotifsEvents eventEntry) {
    NotifsEvents events = new NotifsEvents();
    events.notif_id = new HashMap<>();
    events.notif_id.put(notifId, eventEntry);
    getHost().publish(events);

    Date timeoutDate = new Date(getNow().getTime() + EVENT_ACK_TIMEOUT_MS);
    scheduleFuture(timeoutDate, () -> checkEventAckTimeout(notifId, eventEntry.timestamp));
  }

  /**
   * Resend notification event if not acknowledged within the 1-minute timeout window.
   *
   * @param notifId           Opaque notification identifier.
   * @param expectedTimestamp Transition timestamp of the sent event.
   */
  default void checkEventAckTimeout(String notifId, Date expectedTimestamp) {
    if (getPendingEvents().containsKey(notifId) && !getAckedEvents().contains(notifId)) {
      NotifNotifsEvents currentEvent = getPendingEvents().get(notifId);
      if (currentEvent != null && dateEquals(currentEvent.timestamp, expectedTimestamp)) {
        warn(format("Event ACK timeout for notification %s (%s), resending event",
            notifId, isoConvert(expectedTimestamp)));
        publishNotifsEvent(notifId, currentEvent);
      }
    }
  }

  /**
   * Update notification configuration and process any out-of-band test trigger.
   *
   * @param notifs  Notifs configuration block.
   * @param trigger Out-of-band test trigger from system.testing.trigger.
   */
  default void updateConfig(NotifsConfig notifs, String trigger) {
    setNotifsConfig(notifs);
    String prevTrigger = getPreviousTrigger();
    setPreviousTrigger(trigger);

    if (trigger == null && notifs == null) {
      if (!getNotifsState().notif_id.isEmpty()) {
        getNotifsState().notif_id.clear();
        getPendingEvents().clear();
        getAckedEvents().clear();
        updateState();
      }
      return;
    }

    if (NOTIFS_ALARM_TRIGGER.equals(trigger) && !Objects.equals(trigger, prevTrigger)) {
      info(format("Triggering test alarm notification %s", DEFAULT_NOTIF_ID));
      triggerAlarm(DEFAULT_NOTIF_ID);
    }

    if (notifs != null && notifs.notif_id != null) {
      boolean stateChanged = false;
      for (String notifId : new ArrayList<>(getNotifsState().notif_id.keySet())) {
        NotifNotifsConfig notifConfig = notifs.notif_id.get(notifId);
        if (notifConfig == null) {
          continue;
        }
        NotifNotifsEvents pendingEvent = getPendingEvents().get(notifId);
        if (notifConfig.event_ack != null && pendingEvent != null
            && dateEquals(notifConfig.event_ack, pendingEvent.timestamp)) {
          if (getAckedEvents().add(notifId)) {
            info(format("Notification %s event acknowledged at %s",
                notifId, isoConvert(notifConfig.event_ack)));
          }
        }
        if (Boolean.TRUE.equals(notifConfig.acknowledged)) {
          info(format("Notification %s acknowledged by operator, removing from state", notifId));
          getNotifsState().notif_id.remove(notifId);
          getPendingEvents().remove(notifId);
          getAckedEvents().remove(notifId);
          stateChanged = true;
        }
      }
      if (stateChanged) {
        updateState();
      }
    }
  }
}
