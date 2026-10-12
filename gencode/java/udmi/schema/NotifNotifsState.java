
package udmi.schema;

import java.util.Date;
import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.annotation.JsonPropertyDescription;
import com.fasterxml.jackson.annotation.JsonPropertyOrder;


/**
 * Notif Notifs State
 * <p>
 * Object representation for a single notification state
 * 
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public class NotifNotifsState {

    /**
     * Mapping for the notification target to an internal resource (e.g. BACnet object reference)
     * 
     */
    @JsonProperty("ref")
    @JsonPropertyDescription("Mapping for the notification target to an internal resource (e.g. BACnet object reference)")
    public String ref;
    /**
     * Current state value of the notification
     * 
     */
    @JsonProperty("current")
    @JsonPropertyDescription("Current state value of the notification")
    public String current;
    /**
     * RFC 3339 UTC timestamp when the notification state last changed
     * 
     */
    @JsonProperty("timestamp")
    @JsonPropertyDescription("RFC 3339 UTC timestamp when the notification state last changed")
    public Date timestamp;
    /**
     * Entry
     * <p>
     * 
     * 
     */
    @JsonProperty("status")
    public Entry status;

    @Override
    public int hashCode() {
        int result = 1;
        result = ((result* 31)+((this.ref == null)? 0 :this.ref.hashCode()));
        result = ((result* 31)+((this.current == null)? 0 :this.current.hashCode()));
        result = ((result* 31)+((this.timestamp == null)? 0 :this.timestamp.hashCode()));
        result = ((result* 31)+((this.status == null)? 0 :this.status.hashCode()));
        return result;
    }

    @Override
    public boolean equals(Object other) {
        if (other == this) {
            return true;
        }
        if ((other instanceof NotifNotifsState) == false) {
            return false;
        }
        NotifNotifsState rhs = ((NotifNotifsState) other);
        return (((((this.ref == rhs.ref)||((this.ref!= null)&&this.ref.equals(rhs.ref)))&&((this.current == rhs.current)||((this.current!= null)&&this.current.equals(rhs.current))))&&((this.timestamp == rhs.timestamp)||((this.timestamp!= null)&&this.timestamp.equals(rhs.timestamp))))&&((this.status == rhs.status)||((this.status!= null)&&this.status.equals(rhs.status))));
    }

}
