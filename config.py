"""Settings shared by both processes.

There are two kinds of value here, and the distinction decides where each one
belongs (DECISIONS.md entry 14):

  Group 1 - the experiment.  Facts about the world being simulated. Changing one
  changes what is being measured, so the change must be visible in git. These
  are plain constants, deliberately NOT overridable from the environment.

  Group 2 - the machine.  Where the code happens to be running. Nothing to do
  with dispatch. These come from the environment, with a working default, so the
  same code runs anywhere without being edited.

Values join this file when their first consumer appears, not in advance.
"""

import os

# --- Group 1: the experiment -------------------------------------------------

# H3 cells are ~0.53 km edge and ~0.74 km2 at this resolution: a few city
# blocks. Finer means less wasted ranking but more index churn as riders cross
# boundaries; coarser means the opposite. See DECISIONS.md entry 2.
H3_RESOLUTION = 8

# The simulated city: a square box this many km on a side, centred here.
# Converted to degrees by travel.km_to_degrees, because a given distance in km
# is a different number of degrees east-west than north-south at this latitude.
CITY_CENTRE = (12.9716, 77.5946)  # Bangalore
CITY_SIZE_KM = 7.0

# How many rings of H3 cells the dispatch service searches around a restaurant.
# Fixed rather than expanded outward, because hex-ring number is not distance
# order: a rider at the far edge of ring 1 can be further away than one at the
# near edge of ring 2. See DECISIONS.md entry 2.
#
# k=2 is 19 cells. Cost grows as 3k^2+3k+1, so k=3 would more than double the
# candidate count.
#
# NOT a radius, and this is easy to get wrong: the disk is a hexagonal region
# whose furthest corner is ~2.37 km out but whose flat sides are nearer, and it
# is centred on the restaurant's CELL, not the restaurant, so it is offset by up
# to ~0.53 km. Two riders at the same 1.84 km from two restaurants were measured
# one inside the disk and one outside.
K_RING = 2

# Ceiling for the fallback search. The service widens past K_RING ONLY when the
# disk holds no riders at all - which is permanent starvation, not saturation:
# a fixed K cannot reach further, riders stay where they dropped off (entry 4),
# and orders never give up, so such an order retries forever. See entry 20.
#
# In this 7 km city a res-8 cell is 0.737 km2, so the whole city is only ~66
# cells: k=3 is 56% of it and k=5 is more than all of it. The ceiling exists
# because past a point there is nothing further to find.
MAX_K_RING = 5

# Rider speed. Needed by BOTH processes - the simulator to move riders, the
# service to estimate arrival - which is the exact case entry 14 exists for. If
# the two ever disagreed, dispatch would rank using a wrong model of the world
# and nothing would report an error.
#
# A guess about the physical world, so expect to tune it: an urban two-wheeler
# average including traffic lights, turns and parking.
RIDER_SPEED_KMH = 25.0

# Fleet size. Sized so a k=2 ring usually holds enough candidates for ranking
# to mean something: a k=2 disk is ~14.4 km2 of a 49 km2 city, so about 29% of
# the fleet. seed.py prints the real figure for the current seed.
#
# This is the knob that controls CONTENTION, which is what Day 5 depends on.
# Too large and free riders are always plentiful, so greedy looks perfect and
# batched matching has nothing to improve. See DECISIONS.md entry 16.
FLEET_SIZE = 75

# Every random choice in the simulation descends from this one number. Same
# seed, same orders, same decisions, same output - which is the whole basis of
# comparing two policies on Day 5.
#
# sim.py derives two INDEPENDENT generators from it, one for the fleet and one
# for the orders. If a single generator produced both, changing FLEET_SIZE would
# shift every order's arrival time, and varying the fleet against a fixed order
# stream is precisely the Day 4 experiment.
RANDOM_SEED = 42

# One tick of simulated time. Small enough that a rider's 69 m of movement is
# fine-grained against a ~530 m cell; large enough that an hour is 360 steps.
TICK_SECONDS = 10

# How long orders keep being created. The run then DRAINS - it keeps ticking
# past this until every order is delivered. Truncating instead would discard
# exactly the slowest orders, flattering the measured delivery times.
RUN_SECONDS = 3600

# Order arrival rate. The fleet's ceiling is roughly 215 orders/hour (each order
# occupies a rider for about 1250 s), so this is ~56% load: real competition
# during bursts, no permanent queue.
#
# THIS IS THE CONTENTION KNOB alongside FLEET_SIZE. If Day 5 finds no gap
# between greedy and batched matching, raise this before suspecting the
# matching code - matching only wins when orders actually compete.
ORDERS_PER_HOUR = 120

# Orders pick a restaurant from a fixed set rather than inventing a new location
# each time. Repeated locations make demand cluster geographically, which is
# when two orders compete for one rider.
RESTAURANT_COUNT = 20

# Fixed prep time per restaurant, starting when the order is created (entry 8).
# A rider arriving before this has elapsed waits at the restaurant.
PREP_SECONDS = 12 * 60

# Where the per-order log is written. One row per order, raw timestamps rather
# than computed durations, so a new metric never needs a new simulation run.
CSV_PATH = "orders.csv"


# --- Group 2: the machine ----------------------------------------------------

REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")

# 6380, not the default 6379. Port 6379 on this machine belongs to a different
# project, and a connection to it succeeds while talking to the wrong database.
# See DECISIONS.md entry 10.
REDIS_PORT = int(os.getenv("REDIS_PORT", "6380"))

# 8001, not the usual 8000. Port 8000 on this machine is already serving a
# different project of Devvrath's ("CivilSpace Audit API"), so uvicorn silently
# fails to bind and exits while curl still gets answers - from the wrong app.
# Second time this exact class of mistake has cost time; see DECISIONS entry 10.
DISPATCH_HOST = os.getenv("DISPATCH_HOST", "127.0.0.1")
DISPATCH_PORT = int(os.getenv("DISPATCH_PORT", "8001"))
DISPATCH_URL = f"http://{DISPATCH_HOST}:{DISPATCH_PORT}"
