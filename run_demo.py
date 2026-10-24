"""One command for the whole pipeline: start the simulated line, collect one shift into
PostgreSQL, then run the analysis.

    python run_demo.py

You can also run the three pieces yourself in separate terminals:
    python line_simulator.py
    python collector.py
    python analysis.py
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent

sim = subprocess.Popen([sys.executable, str(ROOT / "line_simulator.py")])
try:
    time.sleep(1)
    subprocess.run([sys.executable, str(ROOT / "collector.py")], check=True)
    sim.wait(timeout=60)
finally:
    if sim.poll() is None:
        sim.terminate()
subprocess.run([sys.executable, str(ROOT / "analysis.py")], check=True)
