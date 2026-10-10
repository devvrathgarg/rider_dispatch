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
RANDOM_SEED = 42


# --- Group 2: the machine ----------------------------------------------------

REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")

# 6380, not the default 6379. Port 6379 on this machine belongs to a different
# project, and a connection to it succeeds while talking to the wrong database.
# See DECISIONS.md entry 10.
REDIS_PORT = int(os.getenv("REDIS_PORT", "6380"))
