"""Shared settings for the simulated packaging line, the collector and the analysis.

Everything here is a simulation parameter. Real equipment would expose its own tag
names, units and limits; this demo only mirrors the shape of the problem.
"""
import os
from datetime import datetime

# --- Connections -------------------------------------------------------------
OPCUA_ENDPOINT = os.environ.get("OPCUA_ENDPOINT", "opc.tcp://127.0.0.1:4840/packline/")
OPCUA_NAMESPACE = "urn:packline-demo:line1"
# psycopg connection string. Example for a local install: "host=localhost dbname=packline user=postgres"
PG_DSN = os.environ.get("PG_DSN", "host=localhost dbname=packline user=postgres")

# --- Product (sliced bacon, 500 g label) -------------------------------------
NOMINAL_G = 500.0          # net weight printed on the label
SETPOINT_G = 504.0         # slicer target: a little above nominal to protect against underweight
LOWER_REJECT_G = 495.0     # checkweigher rejects any pack it measures below this
PRODUCT_SIGMA_G = 3.2      # natural pack-to-pack variation from slicing
CHECKWEIGHER_BIAS_G = 0.8  # hidden calibration offset: the checkweigher reads high
CHECKWEIGHER_NOISE_G = 0.3
MANUAL_SCALE_NOISE_G = 0.2 # QA reference scale used for manual checks

# --- Line speed --------------------------------------------------------------
IDEAL_RATE_PPM = 30.0      # nameplate packs per minute (used for OEE performance)
NORMAL_RATE_PPM = 28.0

# --- Shift and time compression ----------------------------------------------
SHIFT_START = datetime(2026, 10, 26, 6, 0)
SHIFT_MINUTES = 480
REAL_SECONDS_PER_SIM_MINUTE = float(os.environ.get("SIM_SPEED", "0.5"))  # 8 h shift in ~4 min

# --- SPC ---------------------------------------------------------------------
SUBGROUP_SIZE = 5            # first 5 packs of each running minute form one subgroup
BASELINE_END_MINUTE = 180    # limits are computed from 06:00-09:00, a known-stable period
MANUAL_CHECK_EVERY_MIN = 30  # how often QA weighs packs by hand today
