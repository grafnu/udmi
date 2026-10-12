
package udmi.schema;

import java.util.Date;
import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.annotation.JsonPropertyDescription;
import com.fasterxml.jackson.annotation.JsonPropertyOrder;


/**
 * Notif Notifs Events
 * <p>
 * Object representation for a single notification event
 * 
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public class NotifNotifsEvents {

    /**
     * Target point or entity name associated with the notification
     * 
     */
    @JsonProperty("point")
    @JsonPropertyDescription("Target point or entity name associated with the notification")
    public String point;
    /**
     * Current state value entered as a result of the transition
     * 
     */
    @JsonProperty("current")
    @JsonPropertyDescription("Current state value entered as a result of the transition")
    public String current;
    /**
     * Previous state value immediately prior to the transition
     * 
     */
    @JsonProperty("previous")
    @JsonPropertyDescription("Previous state value immediately prior to the transition")
    public String previous;
    /**
     * RFC 3339 UTC timestamp when the transition occurred
     * 
     */
    @JsonProperty("timestamp")
    @JsonPropertyDescription("RFC 3339 UTC timestamp when the transition occurred")
    public Date timestamp;
    /**
     * Identifier classifying the transition type or trigger
     * 
     */
    @JsonProperty("transition")
    @JsonPropertyDescription("Identifier classifying the transition type or trigger")
    public String transition;

    @Override
    public int hashCode() {
        int result = 1;
        result = ((result* 31)+((this.current == null)? 0 :this.current.hashCode()));
        result = ((result* 31)+((this.previous == null)? 0 :this.previous.hashCode()));
        result = ((result* 31)+((this.point == null)? 0 :this.point.hashCode()));
        result = ((result* 31)+((this.transition == null)? 0 :this.transition.hashCode()));
        result = ((result* 31)+((this.timestamp == null)? 0 :this.timestamp.hashCode()));
        return result;
    }

    @Override
    public boolean equals(Object other) {
        if (other == this) {
            return true;
        }
        if ((other instanceof NotifNotifsEvents) == false) {
            return false;
        }
        NotifNotifsEvents rhs = ((NotifNotifsEvents) other);
        return ((((((this.current == rhs.current)||((this.current!= null)&&this.current.equals(rhs.current)))&&((this.previous == rhs.previous)||((this.previous!= null)&&this.previous.equals(rhs.previous))))&&((this.point == rhs.point)||((this.point!= null)&&this.point.equals(rhs.point))))&&((this.transition == rhs.transition)||((this.transition!= null)&&this.transition.equals(rhs.transition))))&&((this.timestamp == rhs.timestamp)||((this.timestamp!= null)&&this.timestamp.equals(rhs.timestamp))));
    }

}
