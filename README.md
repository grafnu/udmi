# **UDMI Overview**

The Universal Device Management Interface (UDMI) provides a high-level specification for the management and operation of physical IoT systems. It establishes a uniform, robust, and scalable interface layer between devices and applications.

## **UDMI Resources**

The UDMI community provides essential resources to facilitate implementation and engagement:

* Core documentation for tools and specifications is available.  
* The message schema definition is provided with an Interactive Viewer.  
* An email discussion list is available at `udmi-discuss@googlegroups.com`.  
* A bi-weekly video meeting is open to all for discussion.

## **Major Capabilities**

The UDMI platform provides five essential capabilities for enterprise IoT system stakeholders:

* **Secure Ingestion:** Provides core hosted services establishing secure connectivity and device authentication protocols for incoming data streams.  
* **Fleet Management:** Maintains devices in a reliable and secure operational state across all building installations.  
* **Information Access:** Delivers device telemetry to upstream consumers in a consistent and predictable manner. It relies on the curation of semantic modeling that dictates how applications access and interpret normalized data streams.  
* **Building Assembly:** Defines the end-to-end flow for efficiently configuring and provisioning core infrastructure to accept and process data. This process integrates prerequisites, provisioning, and verification steps.  
* **Ecosystem Standardization:** Provides uniform systems and tools necessary to drive consistency and minimize toil across a fleet of heterogeneous devices.

## **Design Philosophy**

The UDMI approach to management automation is rooted in core architectural design principles, driving the platform toward the Software-Defined Building (SDB) model.

* **Declarative Specification:** Configuration defines the desired system state in cloud metadata, relying on an idempotent model to reliably match the actual reported state.  
* **Secure and Authenticated Foundation:** The design mandates a rigorous security foundation, requiring a properly secured and authenticated channel from the device up to the managing infrastructure.  
* **Reduced Ambiguity:** The design philosophy intentionally minimizes complexity by striving toward having a singular, explicit method for implementing core functions.  
* **Application Decoupling:** The standardized interface completely separates device behavior and data structures from consuming applications, ensuring the portability of intelligence.

## **B-Suite Product Components**

The UDMI tools are organized as the B-Suite product portfolio, a collection of separable components that collectively deliver the platform's core capabilities:

* **Barbican:** The core hosted cloud-based system that runs continuous services for secure and robust fleet management operations.  
* **Bridgehead:** The on-prem managed edge compute platform that packages components to translate field bus protocols and hosts the Spotter Node.  
* **BAMBI:** The interface for model curation, system configuration, and provisioning, manifesting as a lightweight web-based interface.  
* **Biquencer:** The dedicated system for automated standardized testing of IoT device compliance.  
* **Butler:** The utility and control tools that provide reliable software rollout, configuration management, and orchestrate automated key rotation protocols across the managed fleet.

## **Key Roles and Associated CUJs**

The UDMI platform serves distinct user personas, each with specific **Critical User Journeys (CUJs)**:

* **The Builder:** In order to *establish a functional operational system*, this persona needs to *physically and logically implement the architectural design*.  
* **The Guardian:** In order to *ensure long-term platform health*, this persona needs to *operate and maintain the integrated technology solution after it transitions to live status*.  
* **The Developer:** In order to *implement the management interface specification*, this persona needs to *program the device to adhere to specified requirements*.

## **Feature Roadmap**

The roadmap is prioritized using P0 (Must), P1 (Should), and P2 (Could) designators.

* **Managed Updates (P0):** Automates the deployment of security credentials (key and certificate rotation) and system software across the device fleet to maintain operational resilience and eliminate manual toil.  
* **Effortless Onboarding (P0):** Integrates advanced interfaces and AI capabilities with the unified building model to streamline system configuration and provisioning for the Builder persona.  
* **Python Library (P0):** Delivers a cleanly architected programming interface in a common scripting language to establish a common software environment for core infrastructure functions and reliable fleet-wide automation.  
* **Spotter Node (P1):** Refactors the on-prem component to serve as a foundational platform for discovery scans, diagnostics, and network tracing, acting as the necessary "agent on the inside" for pre-onboarding validation.  
* **Headend Extraction (P1):** Automates the retrieval of existing model data directly from proprietary Building Management System head-end APIs to ensure model consistency and comprehensiveness.  
* **Testing Cadre (P1):** Creates a standardized testing environment and prescriptive procedure for external partner compliance verification, enabling manufacturers to perform self-testing locally.  
* **Open Infrastructure (P1):** Implements a functionally equivalent open-source architectural replacement for proprietary core messaging components to secure long-term architectural independence and reduce operational overhead.  
* **Responsive Validator (P2):** Enhances internal validation tools for continuous monitoring of telemetry data quality, tracking message rate and value tolerance across all production sites.  
* **Fieldbus Diagnosticator (P2):** Creates an automated capability for extracting low-level fieldbus packet traces from managed systems to the cloud, providing a robust mechanism for triaging foundational system errors.  
* **Batch Provisioning (P2):** Delivers a scalable provisioning capability that automates device setup using unique identifiers to generate secure keys and endpoints, enabling onboarding-at-scale.  
* **Writeback Core (P2):** Standardizes the mechanism for deploying algorithmic control logic from the cloud across the installed fleet, providing the foundation for continuous feature evolution and SDB functionality.  
* **Vendor Catalog (P2):** Delivers a structured output of compliance testing results to foster ecosystem understanding and support procurement decisions.

