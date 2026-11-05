package daq.pubber.impl.manager;

import daq.pubber.impl.PubberManager;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import udmi.lib.client.manager.NotifsManager;
import udmi.lib.intf.ManagerHost;
import udmi.schema.NotifNotifsEvents;
import udmi.schema.NotifsConfig;
import udmi.schema.NotifsState;
import udmi.schema.PubberConfiguration;

/**
 * Container class for dealing with the notifs subblock of UDMI.
 */
public class PubberNotifsManager extends PubberManager implements NotifsManager {

  private final NotifsState notifsState;
  private final Map<String, NotifNotifsEvents> pendingEvents = new ConcurrentHashMap<>();
  private final Set<String> ackedEvents = new HashSet<>();
  private NotifsConfig notifsConfig;
  private String previousTrigger;

  /**
   * Create a new container with the given host.
   */
  public PubberNotifsManager(ManagerHost host, PubberConfiguration configuration) {
    super(host, configuration);
    notifsState = new NotifsState();
    notifsState.notif_id = new HashMap<>();
  }

  @Override
  public NotifsConfig getNotifsConfig() {
    return notifsConfig;
  }

  @Override
  public void setNotifsConfig(NotifsConfig notifsConfig) {
    this.notifsConfig = notifsConfig;
  }

  @Override
  public NotifsState getNotifsState() {
    return notifsState;
  }

  @Override
  public Map<String, NotifNotifsEvents> getPendingEvents() {
    return pendingEvents;
  }

  @Override
  public Set<String> getAckedEvents() {
    return ackedEvents;
  }

  @Override
  public String getPreviousTrigger() {
    return previousTrigger;
  }

  @Override
  public void setPreviousTrigger(String trigger) {
    this.previousTrigger = trigger;
  }
}
