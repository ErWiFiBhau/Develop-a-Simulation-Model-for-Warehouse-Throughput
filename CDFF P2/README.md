# CDFF P2: Warehouse Throughput Simulation

## Project Overview
This project simulates the end-to-end flow of goods through a simplified warehouse using Python and the SimPy discrete-event simulation framework. It demonstrates how varying resource capacities (e.g., adding workers or equipment) affects warehouse throughput, order lead times, and resource utilization. 

The simulation evaluates operational bottlenecks by running multiple scenarios and comparing the output statistically over multiple replications.

## Modeled Processes
The simulation tracks two major workflows:

1. **Inbound Flow:**
   * **Receiving:** Inbound shipments arrive and are unloaded at the dock.
   * **Putaway:** Forklifts move the received goods into storage.
   * **Inventory Replenishment:** Stock levels are updated and become available for customer orders.

2. **Outbound Flow:**
   * **Order Arrival:** Customer orders enter the system.
   * **Inventory Allocation:** Orders wait in a queue if sufficient stock is unavailable.
   * **Picking:** Pickers travel to locations and retrieve items.
   * **Packing:** Items are boxed, wrapped, and labeled at packing stations.
   * **Shipping:** Completed boxes are staged and loaded onto outbound carriers.

## Installation
Ensure you have Python 3.8+ installed. Install the required dependencies using:

```bash
python -m pip install -r requirements.txt
```

## Execution
Run the simulation script from your terminal:

```bash
python warehouse_simulator.py
```

## Generated Outputs
The script automatically executes the scenarios and generates the following artifacts in the execution directory:
* **Console Scenario Report:** A detailed readout of KPIs and dynamic management insights.
* **CSV Comparison Results (`warehouse_simulation_results.csv`):** Tabular data covering throughput, lead times, backlog, and utilization metrics for each scenario.
* **Project Report (`warehouse_simulation_report.md`, `.html`, `.txt`):** End-to-end explanation of planning assumptions, execution method, scenario results, interpretation, limitations, and recommended next steps.
* **Operations Dashboard (`operations_dashboard.png`):** Four-panel KPI summary covering throughput, lead time, backlog, and bottleneck utilization.
* **Throughput Chart (`throughput_comparison.png`):** Bar chart comparing the completed orders per hour across scenarios.
* **Lead-Time Chart (`lead_time_comparison.png`):** Grouped bar chart comparing average, median, and 95th-percentile order lead times.
* **Backlog Chart (`backlog_comparison.png`):** Bar chart comparing end-of-shift incomplete order counts.
* **Resource-Utilization Chart (`resource_utilization_comparison.png`):** Grouped bar chart comparing the percentage utilization of all warehouse resources.
* **Queue-Time Heatmap (`queue_time_heatmap.png`):** Heatmap showing average queue time by scenario and warehouse resource.
* **Utilization Heatmap (`utilization_heatmap.png`):** Heatmap showing saturation patterns across scenarios and resources.
* **Inventory-Wait Chart (`inventory_wait_events.png`):** Bar chart showing how often orders waited for available inventory.

## Scenarios
The model evaluates three default scenarios:
1. **Scenario A — Baseline:** The current simplified configuration with potential bottlenecks (e.g., 2 Pickers, 1 Packer, 1 Shipper).
2. **Scenario B — Additional packing capacity:** Increases packing capacity from 1 to 2 stations/workers to assess if queueing at packing can be resolved.
3. **Scenario C — Faster picking equipment:** Keeps baseline staffing but assumes a 20% reduction in picking service time through upgraded equipment.

## Metrics
* **Throughput:** The number of completed customer orders divided by the total operating hours (8 hours).
* **Lead Time:** The total elapsed time from when a customer order arrives to when it completes shipping.
* **Queue Time:** The time an entity spends waiting for a resource to become available.
* **Utilization:** The percentage of the operating period a resource spends actively processing work (busy time / available time).
* **Backlog:** The count of customer orders that arrived but were not completed by the end of the operating period.
* **Bottleneck:** The resource with the highest utilization percentage, which most heavily constrains system throughput.

## Assumptions
The following values are illustrative engineering assumptions used for this simulation:
* **Operating Period:** 480 minutes (8-hour shift) with a 420-minute order arrival cutoff.
* **Service Times:** Modeled using triangular distributions (e.g., Picking takes 5 to 15 minutes, with a mode of 9 minutes).
* **Arrival Rates:** Modeled using exponential distributions (e.g., mean order inter-arrival time is 6 minutes).
* **Quantities:** Randomly assigned between minimum and maximum bounds.

## Limitations
This project is a simplified warehouse discrete-event simulation, **not a complete WMS digital twin**. It does not model:
* Detailed 3D facility layout or aisle geometry.
* Exact picker routing, batch picking, zone picking, or wave planning.
* Worker breaks, shift overlap, overtime, or equipment breakdowns.
* Carrier scheduling, receiving yard management, or returns processing.

## Sources
This model uses general process assumptions informed by standard industry practices and academic principles of discrete-event logistics modeling.
* Banks, J., Carson, J. S., Nelson, B. L., & Nicol, D. M. (2014). *Discrete-Event System Simulation* (5th ed.). Pearson.
* Bartholdi, J. J., & Hackman, S. T. (2019). *Warehouse & Distribution Science*. Supply Chain and Logistics Institute, Georgia Institute of Technology. (Accessed June 2026).
