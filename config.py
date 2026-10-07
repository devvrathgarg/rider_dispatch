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


# --- Group 2: the machine ----------------------------------------------------

REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")

# 6380, not the default 6379. Port 6379 on this machine belongs to a different
# project, and a connection to it succeeds while talking to the wrong database.
# See DECISIONS.md entry 10.
REDIS_PORT = int(os.getenv("REDIS_PORT", "6380"))
