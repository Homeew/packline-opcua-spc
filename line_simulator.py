"""Simulated slicing/packaging line exposed as an OPC UA server.

It plays one 8-hour shift (compressed to a few minutes) of a line that slices bacon,
packs it in a thermoformer and weighs every pack on a checkweigher. The OPC UA address
space looks like this:

    Objects/
      Line1/
        Checkweigher/
          LastWeight_g   Double   measured weight of the pack that just passed
          PackCount      UInt32   total packs weighed this shift
          RejectCount    UInt32   packs rejected as underweight
          LowerLimit_g   Double
        LineStatus/
          State          String   RUNNING | STOPPED | SHIFT_END
          StopReason     String
          PlannedStop    Boolean  True for breaks (excluded from OEE)
          ShiftComplete  Boolean

Every value is written with a SourceTimestamp equal to the simulated time the event
happened, the way a PLC stamps its own data.

The shift contains realistic problems for the collector and analysis to find:
  * a loader jam, a thermoformer film break, a changeover and several micro-stops
  * a slow slicer drift (blade wear) that pushes weights down until rejects climb
  * reduced speed after the film break
  * a +0.8 g calibration offset in the checkweigher (only visible against manual checks)

Run:  python line_simulator.py
"""
import asyncio
import csv
import logging
import random
from datetime import timedelta
from pathlib import Path

import numpy as np
from asyncua import Server, ua

import config as C

log = logging.getLogger("simulator")

# (start_min, duration_min, reason, planned) relative to shift start
SCHEDULED_STOPS = [
    (100, 12, "Loader jam", False),
    (180, 15, "Break", True),
    (270, 30, "Lunch", True),
    (300, 20, "Changeover", False),
    (398, 4, "Slicer adjustment", False),
    (425, 7, "Film break - thermoformer", False),
]
DRIFT_START, DRIFT_END, DRIFT_DEPTH_G = 350, 398, -10.0   # blade wear, fixed at 398
SLOW_START, SLOW_END, SLOW_RATE_PPM = 432, 460, 24.0       # cautious restart after film break
MICRO_STOP_REASONS = ["Minor stop - tray misfeed", "Minor stop - product jam at slicer"]


def build_stop_list(rng):
    """Scheduled stops plus a handful of random 1-3 minute micro-stops."""
    stops = list(SCHEDULED_STOPS)
    busy = [(s, s + d) for s, d, _, _ in stops] + [(DRIFT_START - 5, DRIFT_END + 5)]
    added = 0
    while added < 7:
        start = rng.uniform(15, C.SHIFT_MINUTES - 10)
        dur = rng.choice([1.0, 1.5, 2.0, 3.0])
        if any(start < b + 3 and start + dur > a - 3 for a, b in busy):
            continue
        stops.append((start, dur, rng.choice(MICRO_STOP_REASONS), False))
        busy.append((start, start + dur))
        added += 1
    return sorted(stops)


def slicer_offset(t_min):
    """Mean weight shift from blade wear: zero, then a linear slide, then corrected."""
    if DRIFT_START <= t_min < DRIFT_END:
        return DRIFT_DEPTH_G * (t_min - DRIFT_START) / (DRIFT_END - DRIFT_START)
    return 0.0


def line_rate(t_min, rng):
    base = SLOW_RATE_PPM if SLOW_START <= t_min < SLOW_END else C.NORMAL_RATE_PPM
    return base * rng.uniform(0.97, 1.03)


def at(t_min):
    """Simulated wall-clock time, rounded to the millisecond like a PLC timestamp."""
    return C.SHIFT_START + timedelta(milliseconds=round(t_min * 60_000))


def dv(value, vtype, when):
    return ua.DataValue(Value=ua.Variant(value, vtype), SourceTimestamp=when, ServerTimestamp=when)


async def main(seed=7):
    rng = random.Random(seed)
    nrng = np.random.default_rng(seed)
    stops = build_stop_list(rng)

    server = Server()
    await server.init()
    server.set_endpoint(C.OPCUA_ENDPOINT)
    server.set_server_name("Packaging Line Simulator")
    idx = await server.register_namespace(C.OPCUA_NAMESPACE)

    line = await server.nodes.objects.add_object(idx, "Line1")
    cw = await line.add_object(idx, "Checkweigher")
    st = await line.add_object(idx, "LineStatus")
    weight = await cw.add_variable(idx, "LastWeight_g", 0.0, ua.VariantType.Double)
    pack_count = await cw.add_variable(idx, "PackCount", 0, ua.VariantType.UInt32)
    reject_count = await cw.add_variable(idx, "RejectCount", 0, ua.VariantType.UInt32)
    await cw.add_variable(idx, "LowerLimit_g", C.LOWER_REJECT_G, ua.VariantType.Double)
    state = await st.add_variable(idx, "State", "STOPPED", ua.VariantType.String)
    reason = await st.add_variable(idx, "StopReason", "Not started", ua.VariantType.String)
    planned = await st.add_variable(idx, "PlannedStop", True, ua.VariantType.Boolean)
    done = await st.add_variable(idx, "ShiftComplete", False, ua.VariantType.Boolean)

    manual_rows, packs_since_check, next_check = [], 0, C.MANUAL_CHECK_EVERY_MIN
    n_packs = n_rejects = 0

    async with server:
        log.info("OPC UA server listening on %s (namespace index %d)", C.OPCUA_ENDPOINT, idx)
        log.info("Waiting 5 s for the collector to connect...")
        await asyncio.sleep(5)
        loop = asyncio.get_running_loop()
        real_t0 = loop.time()

        async def set_state(t_min, s, why, is_planned):
            when = at(t_min)
            await reason.write_value(dv(why, ua.VariantType.String, when))
            await planned.write_value(dv(is_planned, ua.VariantType.Boolean, when))
            await state.write_value(dv(s, ua.VariantType.String, when))
            log.info("%s  %-9s %s", when.strftime("%H:%M"), s, why)

        async def pace(t_min):
            target = real_t0 + t_min * C.REAL_SECONDS_PER_SIM_MINUTE
            delay = target - loop.time()
            if delay > 0.005:
                await asyncio.sleep(delay)

        t = 0.0
        await set_state(t, "RUNNING", "", False)
        stop_iter = iter(stops)
        next_stop = next(stop_iter, None)

        while t < C.SHIFT_MINUTES:
            if next_stop and t >= next_stop[0]:
                s_start, s_dur, s_reason, s_planned = next_stop
                await set_state(s_start, "STOPPED", s_reason, s_planned)
                t = s_start + s_dur
                await pace(t)
                next_stop = next(stop_iter, None)
                if not (next_stop and next_stop[0] <= t):   # back-to-back stops: stay stopped
                    await set_state(t, "RUNNING", "", False)
                continue

            true_w = C.SETPOINT_G + slicer_offset(t) + nrng.normal(0, C.PRODUCT_SIGMA_G)
            measured = round(true_w + C.CHECKWEIGHER_BIAS_G + nrng.normal(0, C.CHECKWEIGHER_NOISE_G), 1)
            when = at(t)
            n_packs += 1
            rejected = measured < C.LOWER_REJECT_G
            n_rejects += rejected
            await weight.write_value(dv(measured, ua.VariantType.Double, when))
            await pack_count.write_value(dv(n_packs, ua.VariantType.UInt32, when))
            if rejected:
                await reject_count.write_value(dv(n_rejects, ua.VariantType.UInt32, when))

            # QA technician's manual check: 5 consecutive packs on a reference scale
            if t >= next_check and packs_since_check < 5:
                manual_rows.append((when.isoformat(sep=" ", timespec="milliseconds"),
                                    round(true_w + nrng.normal(0, C.MANUAL_SCALE_NOISE_G), 1), "QA-1"))
                packs_since_check += 1
                if packs_since_check == 5:
                    next_check += C.MANUAL_CHECK_EVERY_MIN
                    packs_since_check = 0

            t += 1.0 / line_rate(t, rng)
            await pace(t)

        await set_state(C.SHIFT_MINUTES, "SHIFT_END", "End of shift", True)
        await done.write_value(dv(True, ua.VariantType.Boolean,
                                  at(C.SHIFT_MINUTES)))
        log.info("Shift complete: %d packs, %d rejected. Keeping server up 15 s for the collector.",
                 n_packs, n_rejects)

        out = Path(__file__).parent / "data" / "manual_checks.csv"
        out.parent.mkdir(exist_ok=True)
        with out.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["pack_time", "manual_weight_g", "technician"])
            w.writerows(manual_rows)
        log.info("Wrote %d manual check rows to %s", len(manual_rows), out)
        await asyncio.sleep(15)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    asyncio.run(main())
