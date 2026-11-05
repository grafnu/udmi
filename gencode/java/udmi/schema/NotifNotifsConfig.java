
package udmi.schema;

import java.util.Date;
import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.annotation.JsonPropertyDescription;
import com.fasterxml.jackson.annotation.JsonPropertyOrder;


/**
 * Notif Notifs Config
 * <p>
 * Server acknowledgment and control configuration for a single active notification.
 * 
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public class NotifNotifsConfig {

    /**
     * RFC 3339 UTC transition timestamp of the most recently received notification event, acknowledging event receipt by the server.
     * 
     */
    @JsonProperty("event_ack")
    @JsonPropertyDescription("RFC 3339 UTC transition timestamp of the most recently received notification event, acknowledging event receipt by the server.")
    public Date event_ack;
    /**
     * Indicates that the notification condition has been acknowledged by an operator (corresponding to ack_required), instructing the device to clear/remove the notification from state.
     * 
     */
    @JsonProperty("acknowledged")
    @JsonPropertyDescription("Indicates that the notification condition has been acknowledged by an operator (corresponding to ack_required), instructing the device to clear/remove the notification from state.")
    public Boolean acknowledged;

    @Override
    public int hashCode() {
        int result = 1;
        result = ((result* 31)+((this.event_ack == null)? 0 :this.event_ack.hashCode()));
        result = ((result* 31)+((this.acknowledged == null)? 0 :this.acknowledged.hashCode()));
        return result;
    }

    @Override
    public boolean equals(Object other) {
        if (other == this) {
            return true;
        }
        if ((other instanceof NotifNotifsConfig) == false) {
            return false;
        }
        NotifNotifsConfig rhs = ((NotifNotifsConfig) other);
        return (((this.event_ack == rhs.event_ack)||((this.event_ack!= null)&&this.event_ack.equals(rhs.event_ack)))&&((this.acknowledged == rhs.acknowledged)||((this.acknowledged!= null)&&this.acknowledged.equals(rhs.acknowledged))));
    }

}
