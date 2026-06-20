"""
================================================================================
  WAREHOUSE THROUGHPUT SIMULATION MODEL
================================================================================
  A discrete-event simulation of warehouse operations covering
  inbound receiving, putaway, picking, packing, and shipping.

  Entities Modeled:
    - Customer Orders
    - Inbound Shipments
    - Resources (Receiving, Putaway, Picking, Packing, Shipping)
    - Inventory Container

  Scenarios:
    A. Baseline - Current simplified resource configuration.
    B. Additional packing capacity - Increase packing workers.
    C. Faster picking equipment - Reduce picking service times by 20%.

  Research Sources & Assumptions:
    Publicly available activity-time data varies substantially by warehouse design, 
    product mix, equipment, and work method. The model therefore uses documented 
    illustrative assumptions that can be replaced with site-specific observations.
    - Average pick time: 5-15 mins (Illustrative based on general manual piece-pick)
    - Packing time: 3-8 mins (Illustrative)
    - Order arrivals: Exponentially distributed
    - Operating period: 480 minutes (8 hour shift) with 420 minute cutoff.

  Exclusions:
    This is a simplified warehouse simulation and not a complete digital twin.
    It excludes 3D layout, slotting, worker breaks, shift schedules, overtime,
    equipment breakdowns, returns processing, and real WMS integration.
================================================================================
"""

import simpy
import random
import statistics
import math
import csv
import os
from datetime import datetime
from html import escape
from dataclasses import dataclass, field
from typing import List, Dict
from pathlib import Path
os.environ.setdefault('MPLCONFIGDIR', str(Path(__file__).resolve().parent / '.matplotlib_cache'))
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# ==============================================================================
# CONFIGURATION
# ==============================================================================

@dataclass
class ProcessTimeParameters:
    # Service times: (min, mode, max) in minutes
    receiving_times: tuple = (8.0, 12.0, 18.0)
    putaway_times: tuple = (6.0, 10.0, 16.0)
    picking_times: tuple = (5.0, 9.0, 15.0)
    packing_times: tuple = (3.0, 5.0, 8.0)
    shipping_times: tuple = (2.0, 4.0, 7.0)

    # Inbound shipment arrivals
    inbound_arrival_mean: float = 60.0
    inbound_qty_min: int = 40
    inbound_qty_max: int = 80

    # Customer order arrivals
    outbound_arrival_mean: float = 6.0
    outbound_qty_min: int = 1
    outbound_qty_max: int = 5
    
    starting_inventory: int = 300

@dataclass
class ScenarioConfig:
    name: str
    receiving_capacity: int
    putaway_capacity: int
    picking_capacity: int
    packing_capacity: int
    shipping_capacity: int
    picking_multiplier: float
    packing_multiplier: float

@dataclass
class SimulationConfig:
    operating_minutes: float = 480.0
    arrival_cutoff: float = 420.0
    replications: int = 30
    base_seed: int = 42

# Optional Chart Settings
SAVE_CHARTS = True
SHOW_CHARTS = False

# ==============================================================================
# ENTITIES & METRICS
# ==============================================================================

@dataclass
class CustomerOrder:
    order_id: int
    arrival_time: float
    quantity: int
    inventory_avail_time: float = 0.0
    picking_start: float = 0.0
    picking_end: float = 0.0
    packing_start: float = 0.0
    packing_end: float = 0.0
    shipping_start: float = 0.0
    shipping_end: float = 0.0
    completed: bool = False
    
    @property
    def lead_time(self):
        if self.completed:
            return self.shipping_end - self.arrival_time
        return 0.0

@dataclass
class InboundShipment:
    shipment_id: int
    arrival_time: float
    quantity: int
    receiving_start: float = 0.0
    receiving_end: float = 0.0
    putaway_start: float = 0.0
    putaway_end: float = 0.0

@dataclass
class ReplicationResult:
    completed_orders: int
    throughput_per_hr: float
    backlog: int
    avg_lead_time: float
    median_lead_time: float
    p95_lead_time: float
    utilization: Dict[str, float]
    avg_queue_time: Dict[str, float]
    max_queue_time: Dict[str, float]
    processed: Dict[str, int]
    inventory_wait_events: int

@dataclass
class ScenarioSummary:
    scenario: ScenarioConfig
    completed_orders_mean: float
    throughput_per_hr_mean: float
    throughput_per_hr_std: float
    backlog_mean: float
    backlog_std: float
    avg_lead_time_mean: float
    avg_lead_time_std: float
    median_lead_time_mean: float
    p95_lead_time_mean: float
    utilization_mean: Dict[str, float]
    avg_queue_time_mean: Dict[str, float]
    max_queue_time_mean: Dict[str, float]
    processed_mean: Dict[str, float]
    inventory_wait_events_mean: float
    bottleneck: str = ""

# ==============================================================================
# SIMULATION LOGIC
# ==============================================================================

class ResourceTracker:
    """Tracks busy time for a SimPy resource to accurately calculate utilization."""
    def __init__(self, env, name, capacity):
        self.env = env
        self.name = name
        self.capacity = capacity
        self.resource = simpy.Resource(env, capacity)
        self.busy_time = 0.0
        self.last_update = 0.0
        
    def update(self):
        dt = self.env.now - self.last_update
        # Count represents the number of resources currently in use
        self.busy_time += self.resource.count * dt
        self.last_update = self.env.now

class WarehouseSimulation:
    def __init__(self, env, scenario: ScenarioConfig, params: ProcessTimeParameters, config: SimulationConfig):
        self.env = env
        self.scenario = scenario
        self.params = params
        self.config = config
        
        # Resources
        self.trackers = {
            'receiving': ResourceTracker(env, 'receiving', scenario.receiving_capacity),
            'putaway': ResourceTracker(env, 'putaway', scenario.putaway_capacity),
            'picking': ResourceTracker(env, 'picking', scenario.picking_capacity),
            'packing': ResourceTracker(env, 'packing', scenario.packing_capacity),
            'shipping': ResourceTracker(env, 'shipping', scenario.shipping_capacity)
        }
        
        self.inventory = simpy.Container(env, init=params.starting_inventory)
        
        # Entity Tracking
        self.generated_orders: List[CustomerOrder] = []
        self.completed_orders: List[CustomerOrder] = []
        self.generated_shipments: List[InboundShipment] = []
        
        # Metrics Tracking
        self.inventory_wait_events = 0
        self.queue_times = {
            'receiving': [], 'putaway': [], 'picking': [], 'packing': [], 'shipping': []
        }

    def process_inbound(self, shipment: InboundShipment):
        """Models the flow of an inbound shipment through receiving and putaway."""
        # 1. Receiving
        req = self.trackers['receiving'].resource.request()
        yield req
        self.trackers['receiving'].update()
        
        queue_time = self.env.now - shipment.arrival_time
        self.queue_times['receiving'].append(queue_time)
        shipment.receiving_start = self.env.now
        
        service_time = random.triangular(*self.params.receiving_times)
        yield self.env.timeout(service_time)
        
        self.trackers['receiving'].update()
        self.trackers['receiving'].resource.release(req)
        shipment.receiving_end = self.env.now
        
        # 2. Putaway
        req2 = self.trackers['putaway'].resource.request()
        yield req2
        self.trackers['putaway'].update()
        
        queue_time2 = self.env.now - shipment.receiving_end
        self.queue_times['putaway'].append(queue_time2)
        shipment.putaway_start = self.env.now
        
        service_time2 = random.triangular(*self.params.putaway_times)
        yield self.env.timeout(service_time2)
        
        self.trackers['putaway'].update()
        self.trackers['putaway'].resource.release(req2)
        shipment.putaway_end = self.env.now
        
        # 3. Replenish Inventory
        yield self.inventory.put(shipment.quantity)

    def process_order(self, order: CustomerOrder):
        """Models the flow of a customer order through picking, packing, and shipping."""
        # 1. Inventory Allocation
        qty = order.quantity
        if self.inventory.level < qty:
            self.inventory_wait_events += 1
        yield self.inventory.get(qty)
        order.inventory_avail_time = self.env.now
        
        # 2. Picking
        req = self.trackers['picking'].resource.request()
        yield req
        self.trackers['picking'].update()
        
        queue_time = self.env.now - order.inventory_avail_time
        self.queue_times['picking'].append(queue_time)
        order.picking_start = self.env.now
        
        service_time = random.triangular(*self.params.picking_times) * self.scenario.picking_multiplier
        yield self.env.timeout(service_time)
        
        self.trackers['picking'].update()
        self.trackers['picking'].resource.release(req)
        order.picking_end = self.env.now
        
        # 3. Packing
        req2 = self.trackers['packing'].resource.request()
        yield req2
        self.trackers['packing'].update()
        
        queue_time2 = self.env.now - order.picking_end
        self.queue_times['packing'].append(queue_time2)
        order.packing_start = self.env.now
        
        service_time2 = random.triangular(*self.params.packing_times) * self.scenario.packing_multiplier
        yield self.env.timeout(service_time2)
        
        self.trackers['packing'].update()
        self.trackers['packing'].resource.release(req2)
        order.packing_end = self.env.now
        
        # 4. Shipping
        req3 = self.trackers['shipping'].resource.request()
        yield req3
        self.trackers['shipping'].update()
        
        queue_time3 = self.env.now - order.packing_end
        self.queue_times['shipping'].append(queue_time3)
        order.shipping_start = self.env.now
        
        service_time3 = random.triangular(*self.params.shipping_times)
        yield self.env.timeout(service_time3)
        
        self.trackers['shipping'].update()
        self.trackers['shipping'].resource.release(req3)
        order.shipping_end = self.env.now
        
        # Completion
        order.completed = True
        self.completed_orders.append(order)

    def generate_inbound(self):
        shipment_id = 0
        while self.env.now < self.config.operating_minutes:
            yield self.env.timeout(random.expovariate(1.0 / self.params.inbound_arrival_mean))
            if self.env.now >= self.config.operating_minutes:
                break
            shipment_id += 1
            qty = random.randint(self.params.inbound_qty_min, self.params.inbound_qty_max)
            shipment = InboundShipment(shipment_id, self.env.now, qty)
            self.generated_shipments.append(shipment)
            self.env.process(self.process_inbound(shipment))

    def generate_orders(self):
        order_id = 0
        while self.env.now < self.config.arrival_cutoff:
            yield self.env.timeout(random.expovariate(1.0 / self.params.outbound_arrival_mean))
            if self.env.now >= self.config.arrival_cutoff:
                break
            order_id += 1
            qty = random.randint(self.params.outbound_qty_min, self.params.outbound_qty_max)
            order = CustomerOrder(order_id, self.env.now, qty)
            self.generated_orders.append(order)
            self.env.process(self.process_order(order))

    def get_metrics(self) -> ReplicationResult:
        completed = len(self.completed_orders)
        backlog = len(self.generated_orders) - completed
        throughput_per_hr = completed / (self.config.operating_minutes / 60.0)
        
        lead_times = [o.lead_time for o in self.completed_orders]
        avg_lt = statistics.mean(lead_times) if lead_times else 0.0
        med_lt = statistics.median(lead_times) if lead_times else 0.0
        
        if len(lead_times) > 1:
            p95_lt = statistics.quantiles(lead_times, n=100)[94]
        elif len(lead_times) == 1:
            p95_lt = lead_times[0]
        else:
            p95_lt = 0.0
            
        utilization = {}
        for name, tracker in self.trackers.items():
            utilization[name] = min(1.0, max(0.0, tracker.busy_time / (tracker.capacity * self.config.operating_minutes)))
            
        avg_queue = {name: (statistics.mean(q) if q else 0.0) for name, q in self.queue_times.items()}
        max_queue = {name: (max(q) if q else 0.0) for name, q in self.queue_times.items()}
        processed = {name: len(q) for name, q in self.queue_times.items()}
        
        return ReplicationResult(
            completed_orders=completed,
            throughput_per_hr=throughput_per_hr,
            backlog=backlog,
            avg_lead_time=avg_lt,
            median_lead_time=med_lt,
            p95_lead_time=p95_lt,
            utilization=utilization,
            avg_queue_time=avg_queue,
            max_queue_time=max_queue,
            processed=processed,
            inventory_wait_events=self.inventory_wait_events
        )

# ==============================================================================
# EXPERIMENT & ANALYSIS
# ==============================================================================

def validate_configuration(sim_config: SimulationConfig, params: ProcessTimeParameters):
    """Validate configuration inputs before execution."""
    if sim_config.operating_minutes <= 0:
        raise ValueError("Operating minutes must be positive.")
    if not (0 <= sim_config.arrival_cutoff <= sim_config.operating_minutes):
        raise ValueError("Arrival cutoff must be between zero and simulation duration.")
    if sim_config.replications <= 0:
        raise ValueError("Replications must be positive.")
    for t_tuple in [params.receiving_times, params.putaway_times, params.picking_times, params.packing_times, params.shipping_times]:
        if not (0 <= t_tuple[0] <= t_tuple[1] <= t_tuple[2]):
            raise ValueError("Triangular distribution times must be non-negative and min <= mode <= max.")
    if params.starting_inventory < 0:
        raise ValueError("Starting inventory must be non-negative.")

def run_replication(scenario: ScenarioConfig, sim_config: SimulationConfig, params: ProcessTimeParameters, seed: int) -> ReplicationResult:
    random.seed(seed)
    env = simpy.Environment()
    sim = WarehouseSimulation(env, scenario, params, sim_config)
    env.process(sim.generate_inbound())
    env.process(sim.generate_orders())
    env.run(until=sim_config.operating_minutes)
    
    # Finalize trackers
    for tracker in sim.trackers.values():
        tracker.update()
        
    return sim.get_metrics()

def summarize_replications(scenario: ScenarioConfig, results: List[ReplicationResult]) -> ScenarioSummary:
    def mean_of(attr):
        return statistics.mean([getattr(r, attr) for r in results])
    def std_of(attr):
        if len(results) < 2: return 0.0
        return statistics.stdev([getattr(r, attr) for r in results])
        
    utilization_mean = {
        res: statistics.mean([r.utilization[res] for r in results])
        for res in results[0].utilization.keys()
    }
    avg_queue_time_mean = {
        res: statistics.mean([r.avg_queue_time[res] for r in results])
        for res in results[0].avg_queue_time.keys()
    }
    max_queue_time_mean = {
        res: statistics.mean([r.max_queue_time[res] for r in results])
        for res in results[0].max_queue_time.keys()
    }
    processed_mean = {
        res: statistics.mean([r.processed[res] for r in results])
        for res in results[0].processed.keys()
    }
    
    return ScenarioSummary(
        scenario=scenario,
        completed_orders_mean=mean_of('completed_orders'),
        throughput_per_hr_mean=mean_of('throughput_per_hr'),
        throughput_per_hr_std=std_of('throughput_per_hr'),
        backlog_mean=mean_of('backlog'),
        backlog_std=std_of('backlog'),
        avg_lead_time_mean=mean_of('avg_lead_time'),
        avg_lead_time_std=std_of('avg_lead_time'),
        median_lead_time_mean=mean_of('median_lead_time'),
        p95_lead_time_mean=mean_of('p95_lead_time'),
        utilization_mean=utilization_mean,
        avg_queue_time_mean=avg_queue_time_mean,
        max_queue_time_mean=max_queue_time_mean,
        processed_mean=processed_mean,
        inventory_wait_events_mean=mean_of('inventory_wait_events')
    )

def identify_bottleneck(summary: ScenarioSummary) -> str:
    """Identify the bottleneck based on highest resource utilization."""
    return max(summary.utilization_mean, key=summary.utilization_mean.get)

def run_scenario(scenario: ScenarioConfig, sim_config: SimulationConfig, params: ProcessTimeParameters) -> ScenarioSummary:
    results = []
    for i in range(sim_config.replications):
        seed = sim_config.base_seed + i
        results.append(run_replication(scenario, sim_config, params, seed))
    summary = summarize_replications(scenario, results)
    summary.bottleneck = identify_bottleneck(summary)
    return summary

def compare_scenarios(summaries: List[ScenarioSummary]):
    """Print the final formatted comparison report to console."""
    print("\n" + "="*80)
    print("WAREHOUSE THROUGHPUT SIMULATION")
    print("="*80)
    
    print("\nMODEL ASSUMPTIONS")
    print("- Operating minutes: 480 (8 hours)")
    print("- Order arrival cutoff: 420 minutes")
    print("- Replications per scenario: 30")
    print("- Excludes: worker breaks, layout specifics, optimal routing")
    
    for s in summaries:
        print(f"\nSCENARIO: {s.scenario.name}")
        print("-" * 40)
        print(f"Capacities: Recv={s.scenario.receiving_capacity}, Putaway={s.scenario.putaway_capacity}, "
              f"Pick={s.scenario.picking_capacity}, Pack={s.scenario.packing_capacity}, Ship={s.scenario.shipping_capacity}")
        print(f"Avg Completed Orders: {s.completed_orders_mean:.1f}")
        print(f"Throughput (Orders/Hr): {s.throughput_per_hr_mean:.2f}")
        print(f"Avg Lead Time (min): {s.avg_lead_time_mean:.2f}")
        print(f"Avg Backlog (orders): {s.backlog_mean:.1f}")
        print(f"Bottleneck: {s.bottleneck.capitalize()} ({s.utilization_mean[s.bottleneck]*100:.1f}%)")

    print("\n" + "="*80)
    print("SCENARIO COMPARISON")
    print("="*80)
    print(f"{'Scenario':<35} | {'Orders/Hr':<10} | {'Avg Lead Time':<15} | {'Backlog':<10} | {'Bottleneck'}")
    print("-" * 80)
    for s in summaries:
        print(f"{s.scenario.name:<35} | {s.throughput_per_hr_mean:<10.2f} | {s.avg_lead_time_mean:<15.2f} | {s.backlog_mean:<10.1f} | {s.bottleneck.capitalize()}")

    print("\n" + "="*80)
    print("MANAGEMENT INSIGHTS")
    print("="*80)
    
    # Dynamic conclusion generation
    best_throughput = max(summaries, key=lambda x: x.throughput_per_hr_mean)
    best_lead_time = min(summaries, key=lambda x: x.avg_lead_time_mean)
    baseline = summaries[0]
    
    print(f"1. Highest Throughput: {best_throughput.scenario.name} ({best_throughput.throughput_per_hr_mean:.2f} orders/hr)")
    print(f"2. Lowest Lead Time: {best_lead_time.scenario.name} ({best_lead_time.avg_lead_time_mean:.2f} mins)")
    print(f"3. Baseline Bottleneck: {baseline.bottleneck.capitalize()}")
    
    if summaries[1].bottleneck != baseline.bottleneck:
        print(f"4. Adding capacity shifted the bottleneck from {baseline.bottleneck} to {summaries[1].bottleneck}.")
    else:
        print(f"4. Adding capacity did not resolve the {baseline.bottleneck} bottleneck.")
        
    print(f"5. Faster picking resulted in {summaries[2].throughput_per_hr_mean:.2f} orders/hr compared to baseline {baseline.throughput_per_hr_mean:.2f}.")
    print("6. Recommendation: Adopt the scenario with the most balanced utilization and highest throughput, prioritizing resolution of the primary bottleneck.")
    print("7. Key Limitation: Processing times are illustrative; actual warehouse geometry and real WMS wave planning may shift results.")

def save_results_to_csv(summaries: List[ScenarioSummary], output_directory: Path):
    out_path = output_directory / 'warehouse_simulation_results.csv'
    with open(out_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            'Scenario', 'Avg Completed Orders', 'Throughput per Hour',
            'Avg Lead Time (min)', 'Median Lead Time (min)', 'P95 Lead Time (min)',
            'Backlog', 'Receiving Util (%)', 'Putaway Util (%)',
            'Picking Util (%)', 'Packing Util (%)', 'Shipping Util (%)',
            'Avg Receiving Queue (min)', 'Avg Putaway Queue (min)',
            'Avg Picking Queue (min)', 'Avg Packing Queue (min)', 'Avg Shipping Queue (min)',
            'Avg Inventory Wait Events',
            'Identified Bottleneck'
        ])
        for s in summaries:
            writer.writerow([
                s.scenario.name,
                f"{s.completed_orders_mean:.2f}",
                f"{s.throughput_per_hr_mean:.2f}",
                f"{s.avg_lead_time_mean:.2f}",
                f"{s.median_lead_time_mean:.2f}",
                f"{s.p95_lead_time_mean:.2f}",
                f"{s.backlog_mean:.2f}",
                f"{s.utilization_mean['receiving']*100:.2f}",
                f"{s.utilization_mean['putaway']*100:.2f}",
                f"{s.utilization_mean['picking']*100:.2f}",
                f"{s.utilization_mean['packing']*100:.2f}",
                f"{s.utilization_mean['shipping']*100:.2f}",
                f"{s.avg_queue_time_mean['receiving']:.2f}",
                f"{s.avg_queue_time_mean['putaway']:.2f}",
                f"{s.avg_queue_time_mean['picking']:.2f}",
                f"{s.avg_queue_time_mean['packing']:.2f}",
                f"{s.avg_queue_time_mean['shipping']:.2f}",
                f"{s.inventory_wait_events_mean:.2f}",
                s.bottleneck
            ])
    print(f"Created CSV: {out_path.name}")

# ==============================================================================
# CHART GENERATION
# ==============================================================================

def create_throughput_chart(scenario_summaries: List[ScenarioSummary], output_directory: Path) -> Path:
    scenarios = [s.scenario.name.split(' — ')[0] for s in scenario_summaries]
    throughput = [s.throughput_per_hr_mean for s in scenario_summaries]
    errors = [s.throughput_per_hr_std for s in scenario_summaries]
    
    plt.figure(figsize=(10, 6))
    plt.bar(scenarios, throughput, yerr=errors, capsize=5, color='#4C72B0', alpha=0.9)
    plt.title('Warehouse Throughput by Scenario')
    plt.xlabel('Scenario')
    plt.ylabel('Completed orders per hour')
    plt.grid(axis='y', linestyle='--', alpha=0.7)
    
    out_path = output_directory / 'throughput_comparison.png'
    if SAVE_CHARTS:
        plt.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close()
    print(f"Created chart: {out_path.name}")
    return out_path

def create_lead_time_chart(scenario_summaries: List[ScenarioSummary], output_directory: Path) -> Path:
    scenarios = [s.scenario.name.split(' — ')[0] for s in scenario_summaries]
    avg_lt = [s.avg_lead_time_mean for s in scenario_summaries]
    med_lt = [s.median_lead_time_mean for s in scenario_summaries]
    p95_lt = [s.p95_lead_time_mean for s in scenario_summaries]
    
    x = range(len(scenarios))
    width = 0.25
    
    plt.figure(figsize=(10, 6))
    plt.bar([i - width for i in x], avg_lt, width, label='Average', color='#55A868')
    plt.bar(x, med_lt, width, label='Median', color='#C44E52')
    plt.bar([i + width for i in x], p95_lt, width, label='95th Percentile', color='#8172B2')
    
    plt.title('Order Lead Time by Scenario')
    plt.xlabel('Scenario')
    plt.ylabel('Lead time in minutes')
    plt.xticks(x, scenarios)
    plt.legend()
    plt.grid(axis='y', linestyle='--', alpha=0.7)
    
    out_path = output_directory / 'lead_time_comparison.png'
    if SAVE_CHARTS:
        plt.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close()
    print(f"Created chart: {out_path.name}")
    return out_path

def create_backlog_chart(scenario_summaries: List[ScenarioSummary], output_directory: Path) -> Path:
    scenarios = [s.scenario.name.split(' — ')[0] for s in scenario_summaries]
    backlog = [s.backlog_mean for s in scenario_summaries]
    errors = [s.backlog_std for s in scenario_summaries]
    
    plt.figure(figsize=(10, 6))
    plt.bar(scenarios, backlog, yerr=errors, capsize=5, color='#DD8452', alpha=0.9)
    plt.title('End-of-Shift Order Backlog by Scenario')
    plt.xlabel('Scenario')
    plt.ylabel('Incomplete orders')
    plt.grid(axis='y', linestyle='--', alpha=0.7)
    
    out_path = output_directory / 'backlog_comparison.png'
    if SAVE_CHARTS:
        plt.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close()
    print(f"Created chart: {out_path.name}")
    return out_path

def create_resource_utilization_chart(scenario_summaries: List[ScenarioSummary], output_directory: Path) -> Path:
    resources = ['receiving', 'putaway', 'picking', 'packing', 'shipping']
    scenarios = [s.scenario.name.split(' — ')[0] for s in scenario_summaries]
    
    x = range(len(resources))
    width = 0.8 / len(scenarios)
    
    plt.figure(figsize=(12, 6))
    colors = ['#4C72B0', '#55A868', '#C44E52']
    
    for i, s in enumerate(scenario_summaries):
        utils = [s.utilization_mean[res] * 100 for res in resources]
        offset = (i - len(scenarios)/2 + 0.5) * width
        plt.bar([pos + offset for pos in x], utils, width, label=scenarios[i], color=colors[i % len(colors)])
        
    plt.title('Warehouse Resource Utilization by Scenario')
    plt.xlabel('Warehouse resource')
    plt.ylabel('Utilization percentage')
    plt.xticks(x, [r.capitalize() for r in resources])
    plt.ylim(0, 100)
    plt.legend()
    plt.grid(axis='y', linestyle='--', alpha=0.7)
    
    out_path = output_directory / 'resource_utilization_comparison.png'
    if SAVE_CHARTS:
        plt.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close()
    print(f"Created chart: {out_path.name}")
    return out_path

def create_queue_time_heatmap(scenario_summaries: List[ScenarioSummary], output_directory: Path) -> Path:
    resources = ['receiving', 'putaway', 'picking', 'packing', 'shipping']
    scenarios = [s.scenario.name.split(' — ')[0] for s in scenario_summaries]
    matrix = [[s.avg_queue_time_mean[res] for res in resources] for s in scenario_summaries]
    
    fig, ax = plt.subplots(figsize=(11, 5.5))
    image = ax.imshow(matrix, cmap='YlOrRd')
    ax.set_title('Average Queue Time Heatmap')
    ax.set_xlabel('Warehouse resource')
    ax.set_ylabel('Scenario')
    ax.set_xticks(range(len(resources)))
    ax.set_xticklabels([r.capitalize() for r in resources])
    ax.set_yticks(range(len(scenarios)))
    ax.set_yticklabels(scenarios)
    
    for i, row in enumerate(matrix):
        for j, value in enumerate(row):
            ax.text(j, i, f"{value:.1f}", ha='center', va='center', color='black')
            
    colorbar = fig.colorbar(image, ax=ax)
    colorbar.set_label('Average wait in minutes')
    fig.tight_layout()
    
    out_path = output_directory / 'queue_time_heatmap.png'
    if SAVE_CHARTS:
        plt.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close()
    print(f"Created chart: {out_path.name}")
    return out_path

def create_utilization_heatmap(scenario_summaries: List[ScenarioSummary], output_directory: Path) -> Path:
    resources = ['receiving', 'putaway', 'picking', 'packing', 'shipping']
    scenarios = [s.scenario.name.split(' — ')[0] for s in scenario_summaries]
    matrix = [[s.utilization_mean[res] * 100 for res in resources] for s in scenario_summaries]
    
    fig, ax = plt.subplots(figsize=(11, 5.5))
    image = ax.imshow(matrix, cmap='Blues', vmin=0, vmax=100)
    ax.set_title('Resource Utilization Heatmap')
    ax.set_xlabel('Warehouse resource')
    ax.set_ylabel('Scenario')
    ax.set_xticks(range(len(resources)))
    ax.set_xticklabels([r.capitalize() for r in resources])
    ax.set_yticks(range(len(scenarios)))
    ax.set_yticklabels(scenarios)
    
    for i, row in enumerate(matrix):
        for j, value in enumerate(row):
            text_color = 'white' if value > 55 else 'black'
            ax.text(j, i, f"{value:.0f}%", ha='center', va='center', color=text_color)
            
    colorbar = fig.colorbar(image, ax=ax)
    colorbar.set_label('Utilization percentage')
    fig.tight_layout()
    
    out_path = output_directory / 'utilization_heatmap.png'
    if SAVE_CHARTS:
        plt.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close()
    print(f"Created chart: {out_path.name}")
    return out_path

def create_inventory_wait_chart(scenario_summaries: List[ScenarioSummary], output_directory: Path) -> Path:
    scenarios = [s.scenario.name.split(' — ')[0] for s in scenario_summaries]
    waits = [s.inventory_wait_events_mean for s in scenario_summaries]
    
    plt.figure(figsize=(10, 5.5))
    bars = plt.bar(scenarios, waits, color='#64B5CD', alpha=0.9)
    plt.title('Inventory Availability Wait Events by Scenario')
    plt.xlabel('Scenario')
    plt.ylabel('Average count of orders delayed by inventory')
    plt.grid(axis='y', linestyle='--', alpha=0.7)
    
    for bar, value in zip(bars, waits):
        plt.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{value:.1f}",
                 ha='center', va='bottom')
        
    out_path = output_directory / 'inventory_wait_events.png'
    if SAVE_CHARTS:
        plt.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close()
    print(f"Created chart: {out_path.name}")
    return out_path

def create_operations_dashboard(scenario_summaries: List[ScenarioSummary], output_directory: Path) -> Path:
    scenarios = [s.scenario.name.split(' — ')[0] for s in scenario_summaries]
    throughput = [s.throughput_per_hr_mean for s in scenario_summaries]
    lead_time = [s.avg_lead_time_mean for s in scenario_summaries]
    backlog = [s.backlog_mean for s in scenario_summaries]
    bottleneck_util = [s.utilization_mean[s.bottleneck] * 100 for s in scenario_summaries]
    
    fig, axes = plt.subplots(2, 2, figsize=(13, 8))
    fig.suptitle('Warehouse Operations KPI Dashboard', fontsize=14)
    
    chart_specs = [
        (axes[0, 0], throughput, 'Throughput', 'Orders per hour', '#4C72B0'),
        (axes[0, 1], lead_time, 'Average Lead Time', 'Minutes', '#55A868'),
        (axes[1, 0], backlog, 'End-of-Shift Backlog', 'Orders', '#DD8452'),
        (axes[1, 1], bottleneck_util, 'Bottleneck Utilization', 'Percent', '#C44E52')
    ]
    
    for ax, values, title, ylabel, color in chart_specs:
        bars = ax.bar(scenarios, values, color=color, alpha=0.9)
        ax.set_title(title)
        ax.set_ylabel(ylabel)
        ax.grid(axis='y', linestyle='--', alpha=0.5)
        ax.tick_params(axis='x', labelrotation=12)
        for bar, value in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{value:.1f}",
                    ha='center', va='bottom', fontsize=9)
    
    fig.tight_layout()
    
    out_path = output_directory / 'operations_dashboard.png'
    if SAVE_CHARTS:
        plt.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close()
    print(f"Created chart: {out_path.name}")
    return out_path

# ==============================================================================
# REPORT GENERATION
# ==============================================================================

def percent_change(new_value: float, old_value: float) -> float:
    if old_value == 0:
        return 0.0
    return ((new_value - old_value) / old_value) * 100

def format_distribution(label: str, values: tuple) -> str:
    return f"{label}: triangular min={values[0]:.1f}, mode={values[1]:.1f}, max={values[2]:.1f} minutes"

def build_management_insights(summaries: List[ScenarioSummary]) -> List[str]:
    baseline = summaries[0]
    best_throughput = max(summaries, key=lambda x: x.throughput_per_hr_mean)
    best_lead_time = min(summaries, key=lambda x: x.avg_lead_time_mean)
    best_backlog = min(summaries, key=lambda x: x.backlog_mean)
    
    insights = [
        f"Highest throughput was produced by {best_throughput.scenario.name} at "
        f"{best_throughput.throughput_per_hr_mean:.2f} completed orders per hour.",
        f"Lowest average lead time was produced by {best_lead_time.scenario.name} at "
        f"{best_lead_time.avg_lead_time_mean:.2f} minutes.",
        f"Lowest end-of-shift backlog was produced by {best_backlog.scenario.name} at "
        f"{best_backlog.backlog_mean:.1f} orders.",
        f"The baseline bottleneck was {baseline.bottleneck}, with average utilization of "
        f"{baseline.utilization_mean[baseline.bottleneck] * 100:.1f}%."
    ]
    
    for summary in summaries[1:]:
        throughput_delta = percent_change(summary.throughput_per_hr_mean, baseline.throughput_per_hr_mean)
        lead_time_delta = percent_change(summary.avg_lead_time_mean, baseline.avg_lead_time_mean)
        backlog_delta = percent_change(summary.backlog_mean, baseline.backlog_mean)
        insights.append(
            f"{summary.scenario.name} changed throughput by {throughput_delta:+.1f}%, "
            f"average lead time by {lead_time_delta:+.1f}%, and backlog by {backlog_delta:+.1f}% versus baseline."
        )
        
    return insights

def build_next_steps(summaries: List[ScenarioSummary]) -> List[str]:
    baseline = summaries[0]
    best_throughput = max(summaries, key=lambda x: x.throughput_per_hr_mean)
    
    return [
        "Replace illustrative processing-time assumptions with time-study observations from the actual site.",
        "Add labor calendars, breaks, overtime rules, and shift handoff logic so capacity reflects real staffing availability.",
        "Model warehouse layout, travel distance, slotting, batch picking, wave planning, and zone picking to improve picking realism.",
        "Run sensitivity analysis across arrival rates, service-time ranges, starting inventory, and staffing levels.",
        "Add cost inputs for labor, equipment, overtime, and backlog penalties so the best operating policy can be selected by ROI, not throughput alone.",
        "Test additional scenarios around shipping cutoff times, carrier departure schedules, receiving appointment variability, and inventory stockout risk.",
        f"Use {best_throughput.scenario.name} as the current performance benchmark, while watching whether the bottleneck shifts away from {baseline.bottleneck}."
    ]

def build_markdown_report(
    summaries: List[ScenarioSummary],
    sim_config: SimulationConfig,
    params: ProcessTimeParameters,
    chart_paths: List[Path]
) -> str:
    generated_at = datetime.now().strftime('%Y-%m-%d %H:%M')
    insights = build_management_insights(summaries)
    next_steps = build_next_steps(summaries)
    
    lines = [
        "# Warehouse Throughput Simulation Report",
        "",
        f"Generated: {generated_at}",
        "",
        "## Executive Summary",
        "",
        "This project uses discrete-event simulation to evaluate how warehouse resource decisions affect outbound order throughput, customer lead time, operational backlog, and resource utilization. The model compares a baseline operating design against two improvement scenarios: adding packing capacity and improving picking speed.",
        "",
        "Key findings:",
    ]
    lines.extend([f"- {insight}" for insight in insights])
    
    lines.extend([
        "",
        "## Project Planning",
        "",
        "The project objective is to create a repeatable decision-support model for warehouse operations. The model focuses on the main inbound and outbound flows that determine whether customer orders can be completed during an 8-hour operating window.",
        "",
        "Modeled workflows:",
        "- Inbound: shipment arrival, receiving, putaway, and inventory replenishment.",
        "- Outbound: customer order arrival, inventory allocation, picking, packing, and shipping.",
        "",
        "Planning assumptions:",
        f"- Operating period: {sim_config.operating_minutes:.0f} minutes.",
        f"- Customer-order arrival cutoff: {sim_config.arrival_cutoff:.0f} minutes.",
        f"- Replications per scenario: {sim_config.replications}.",
        f"- Starting inventory: {params.starting_inventory} units.",
        f"- Inbound arrival mean: {params.inbound_arrival_mean:.1f} minutes.",
        f"- Outbound order arrival mean: {params.outbound_arrival_mean:.1f} minutes.",
        f"- {format_distribution('Receiving time', params.receiving_times)}.",
        f"- {format_distribution('Putaway time', params.putaway_times)}.",
        f"- {format_distribution('Picking time', params.picking_times)}.",
        f"- {format_distribution('Packing time', params.packing_times)}.",
        f"- {format_distribution('Shipping time', params.shipping_times)}.",
        "",
        "## Execution Method",
        "",
        "The simulation is implemented in Python using SimPy. Each replication creates a fresh simulation environment, generates inbound shipments and outbound customer orders using exponential inter-arrival times, processes work through constrained resources, and records order completion and queueing outcomes. Multiple replications are averaged to reduce the effect of random variation.",
        "",
        "KPIs captured:",
        "- Completed orders and throughput per hour.",
        "- Average, median, and 95th-percentile order lead time.",
        "- End-of-shift backlog.",
        "- Resource utilization by receiving, putaway, picking, packing, and shipping.",
        "- Average and maximum queue time by activity.",
        "- Inventory wait events caused by unavailable stock.",
        "",
        "## Scenario Results",
        "",
        "| Scenario | Orders/Hr | Avg Lead Time | P95 Lead Time | Backlog | Bottleneck | Bottleneck Util | Inventory Wait Events |",
        "|---|---:|---:|---:|---:|---|---:|---:|"
    ])
    
    for s in summaries:
        lines.append(
            f"| {s.scenario.name} | {s.throughput_per_hr_mean:.2f} | "
            f"{s.avg_lead_time_mean:.2f} | {s.p95_lead_time_mean:.2f} | "
            f"{s.backlog_mean:.1f} | {s.bottleneck.capitalize()} | "
            f"{s.utilization_mean[s.bottleneck] * 100:.1f}% | {s.inventory_wait_events_mean:.1f} |"
        )
        
    lines.extend([
        "",
        "## Visualization Outputs",
        "",
        "The following charts are generated by the script:"
    ])
    lines.extend([f"- {path.name}" for path in chart_paths])
    
    lines.extend([
        "",
        "## Interpretation",
        "",
        "The bottleneck is identified as the resource with the highest average utilization. High utilization is not automatically bad, but values near saturation often create nonlinear queue growth. The queue-time heatmap should be read together with the utilization charts: a heavily utilized resource with rising queues is a stronger bottleneck signal than utilization alone.",
        "",
        "A scenario should not be selected only because it maximizes throughput. A practical decision should also consider lead time, backlog, cost, labor availability, service-level goals, and whether the new bottleneck is easier to manage than the old one.",
        "",
        "## What Else Can Be Done",
        ""
    ])
    lines.extend([f"- {step}" for step in next_steps])
    
    lines.extend([
        "",
        "## Limitations",
        "",
        "This model is intentionally simplified. It does not include detailed warehouse geometry, travel paths, SKU-level slotting, worker learning curves, absenteeism, equipment downtime, order batching, returns, carrier appointment windows, or WMS integration. Those additions would improve realism but require site-specific data.",
        ""
    ])
    
    return "\n".join(lines)

def write_html_report(markdown_text: str, chart_paths: List[Path], output_directory: Path) -> Path:
    out_path = output_directory / 'warehouse_simulation_report.html'
    body = []
    in_list = False
    in_table = False
    
    for line in markdown_text.splitlines():
        if line.startswith("# "):
            if in_list:
                body.append("</ul>")
                in_list = False
            if in_table:
                body.append("</tbody></table>")
                in_table = False
            body.append(f"<h1>{escape(line[2:])}</h1>")
        elif line.startswith("## "):
            if in_list:
                body.append("</ul>")
                in_list = False
            if in_table:
                body.append("</tbody></table>")
                in_table = False
            body.append(f"<h2>{escape(line[3:])}</h2>")
        elif line.startswith("- "):
            if not in_list:
                body.append("<ul>")
                in_list = True
            body.append(f"<li>{escape(line[2:])}</li>")
        elif line.startswith("|") and not line.startswith("|---"):
            cells = [escape(cell.strip()) for cell in line.strip("|").split("|")]
            if not in_table:
                body.append("<table><thead><tr>")
                body.extend([f"<th>{cell}</th>" for cell in cells])
                body.append("</tr></thead><tbody>")
                in_table = True
            else:
                body.append("<tr>")
                body.extend([f"<td>{cell}</td>" for cell in cells])
                body.append("</tr>")
        elif line.startswith("|---"):
            continue
        elif line.strip() == "":
            if in_list:
                body.append("</ul>")
                in_list = False
            if in_table:
                body.append("</tbody></table>")
                in_table = False
        else:
            if in_list:
                body.append("</ul>")
                in_list = False
            if in_table:
                body.append("</tbody></table>")
                in_table = False
            body.append(f"<p>{escape(line)}</p>")
            
    if in_list:
        body.append("</ul>")
    if in_table:
        body.append("</tbody></table>")
        
    chart_html = "\n".join(
        f'<figure><img src="{escape(path.name)}" alt="{escape(path.stem.replace("_", " ").title())}"><figcaption>{escape(path.name)}</figcaption></figure>'
        for path in chart_paths
    )
    
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Warehouse Throughput Simulation Report</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 40px; color: #222; line-height: 1.5; }}
    h1, h2 {{ color: #17324d; }}
    table {{ border-collapse: collapse; width: 100%; margin: 16px 0 24px; font-size: 14px; }}
    th, td {{ border: 1px solid #d8dee4; padding: 8px; text-align: left; }}
    th {{ background: #eef3f8; }}
    figure {{ margin: 28px 0; }}
    img {{ max-width: 100%; border: 1px solid #d8dee4; }}
    figcaption {{ color: #59636e; font-size: 13px; margin-top: 6px; }}
  </style>
</head>
<body>
{chr(10).join(body)}
<h2>Chart Appendix</h2>
{chart_html}
</body>
</html>
"""
    out_path.write_text(html, encoding='utf-8')
    print(f"Created HTML report: {out_path.name}")
    return out_path

def save_project_report(
    summaries: List[ScenarioSummary],
    sim_config: SimulationConfig,
    params: ProcessTimeParameters,
    chart_paths: List[Path],
    output_directory: Path
) -> Dict[str, Path]:
    markdown_text = build_markdown_report(summaries, sim_config, params, chart_paths)
    
    md_path = output_directory / 'warehouse_simulation_report.md'
    txt_path = output_directory / 'warehouse_simulation_report.txt'
    md_path.write_text(markdown_text, encoding='utf-8')
    txt_path.write_text(markdown_text, encoding='utf-8')
    html_path = write_html_report(markdown_text, chart_paths, output_directory)
    
    print(f"Created Markdown report: {md_path.name}")
    print(f"Created text report: {txt_path.name}")
    return {'markdown': md_path, 'html': html_path, 'text': txt_path}

# ==============================================================================
# MAIN
# ==============================================================================

def main():
    SCRIPT_DIRECTORY = Path(__file__).resolve().parent
    
    try:
        import simpy
        import matplotlib
    except ImportError:
        print("SimPy and Matplotlib are required. Install them with:")
        print("python -m pip install simpy matplotlib")
        return

    sim_config = SimulationConfig()
    params = ProcessTimeParameters()
    validate_configuration(sim_config, params)
    
    scenarios = [
        ScenarioConfig(
            name="Scenario A — Baseline",
            receiving_capacity=1, putaway_capacity=1, picking_capacity=2,
            packing_capacity=1, shipping_capacity=1,
            picking_multiplier=1.00, packing_multiplier=1.00
        ),
        ScenarioConfig(
            name="Scenario B — Additional packing capacity",
            receiving_capacity=1, putaway_capacity=1, picking_capacity=2,
            packing_capacity=2, shipping_capacity=1,
            picking_multiplier=1.00, packing_multiplier=1.00
        ),
        ScenarioConfig(
            name="Scenario C — Faster picking equipment",
            receiving_capacity=1, putaway_capacity=1, picking_capacity=2,
            packing_capacity=1, shipping_capacity=1,
            picking_multiplier=0.80, packing_multiplier=1.00
        )
    ]
    
    summaries = []
    print("Running warehouse simulation scenarios...")
    for scen in scenarios:
        print(f"Processing {scen.name}...")
        summary = run_scenario(scen, sim_config, params)
        summaries.append(summary)
        
    compare_scenarios(summaries)
    save_results_to_csv(summaries, SCRIPT_DIRECTORY)
    
    chart_paths = [
        create_operations_dashboard(summaries, SCRIPT_DIRECTORY),
        create_throughput_chart(summaries, SCRIPT_DIRECTORY),
        create_lead_time_chart(summaries, SCRIPT_DIRECTORY),
        create_backlog_chart(summaries, SCRIPT_DIRECTORY),
        create_resource_utilization_chart(summaries, SCRIPT_DIRECTORY),
        create_queue_time_heatmap(summaries, SCRIPT_DIRECTORY),
        create_utilization_heatmap(summaries, SCRIPT_DIRECTORY),
        create_inventory_wait_chart(summaries, SCRIPT_DIRECTORY)
    ]
    save_project_report(summaries, sim_config, params, chart_paths, SCRIPT_DIRECTORY)

if __name__ == "__main__":
    main()
