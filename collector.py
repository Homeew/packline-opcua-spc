"""OPC UA -> PostgreSQL collector.

Subscribes to the line's tags, buffers every change and writes it to PostgreSQL in
small batches. At the end of the shift it reads the equipment's own counters and
records whether every pack was captured (a data-completeness check).

Run:  python collector.py            (creates the tables, then collects one shift)
"""
import asyncio
import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from asyncua import Client, ua

import config as C

log = logging.getLogger("collector")
ROOT = Path(__file__).parent


def to_naive(ts):
    """OPC UA timestamps arrive as UTC; the simulator writes plant wall-clock time."""
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


class Handler:
    """Receives OPC UA data-change notifications and queues rows for the database."""

    def __init__(self, nodes):
        self.nodes = nodes            # node id string -> tag name
        self.packs, self.states = [], []
        # StopReason and PlannedStop are written with the same source timestamp as State,
        # but notifications for different tags can arrive in any order, so join them on time.
        self.side = {"StopReason": {}, "PlannedStop": {}}
        self.shift_complete = asyncio.Event()

    def datachange_notification(self, node, val, data):
        tag = self.nodes[node.nodeid.to_string()]
        ts = to_naive(data.monitored_item.Value.SourceTimestamp)
        if ts is None or ts < C.SHIFT_START:
            return                            # initial values from before the shift started
        if tag == "LastWeight_g":
            if val > 0:                       # initial value 0.0 is not a pack
                self.packs.append((ts, val, val < C.LOWER_REJECT_G))
        elif tag in self.side:
            self.side[tag][ts] = val
        elif tag == "State":
            self.states.append((ts, val))
        elif tag == "ShiftComplete" and val:
            self.shift_complete.set()

    def ready_events(self):
        """State changes whose reason and planned flag have both arrived."""
        ready, waiting = [], []
        for ts, s in self.states:
            if ts in self.side["StopReason"] and ts in self.side["PlannedStop"]:
                ready.append((ts, s, self.side["StopReason"].pop(ts), bool(self.side["PlannedStop"].pop(ts))))
            else:
                waiting.append((ts, s))
        self.states = waiting
        return ready


def flush(conn, h):
    packs, events = h.packs, h.ready_events()
    h.packs = []
    with conn.cursor() as cur:
        if packs:
            cur.executemany(
                "INSERT INTO packs (ts, weight_g, rejected) VALUES (%s, %s, %s) ON CONFLICT (ts) DO NOTHING",
                packs)
        if events:
            cur.executemany(
                "INSERT INTO line_events (ts, state, reason, planned) VALUES (%s, %s, %s, %s)", events)
    conn.commit()
    return len(packs)


def load_manual_checks(conn):
    path = ROOT / "data" / "manual_checks.csv"
    with path.open() as f, conn.cursor() as cur:
        rows = [(r["pack_time"], r["manual_weight_g"], r["technician"]) for r in csv.DictReader(f)]
        cur.executemany("INSERT INTO manual_checks VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", rows)
    conn.commit()
    log.info("Loaded %d manual check rows", len(rows))


async def main():
    conn = psycopg.connect(C.PG_DSN)
    conn.execute((ROOT / "sql" / "schema.sql").read_text())
    conn.commit()
    run_id = conn.execute("INSERT INTO collection_runs (started_at) VALUES (now()) RETURNING run_id").fetchone()[0]
    conn.commit()

    client = Client(C.OPCUA_ENDPOINT)
    for attempt in range(20):
        try:
            await client.connect()
            break
        except (OSError, asyncio.TimeoutError):
            log.info("Server not up yet, retrying...")
            await asyncio.sleep(1)
    else:
        raise SystemExit("Could not reach the OPC UA server")

    try:
        idx = await client.get_namespace_index(C.OPCUA_NAMESPACE)
        objects = client.nodes.objects
        paths = {
            "LastWeight_g": ["Line1", "Checkweigher", "LastWeight_g"],
            "StopReason": ["Line1", "LineStatus", "StopReason"],
            "PlannedStop": ["Line1", "LineStatus", "PlannedStop"],
            "State": ["Line1", "LineStatus", "State"],
            "ShiftComplete": ["Line1", "LineStatus", "ShiftComplete"],
        }
        nodes = {}
        for tag, p in paths.items():
            nodes[tag] = await objects.get_child([f"{idx}:{x}" for x in p])
        handler = Handler({n.nodeid.to_string(): tag for tag, n in nodes.items()})

        # Notify on every new sample, even if the value repeats (two packs can weigh the same),
        # and keep a deep queue so bursts between publish cycles are not dropped.
        sub = await client.create_subscription(100, handler)
        every_sample = ua.DataChangeFilter(Trigger=ua.DataChangeTrigger.StatusValueTimestamp)
        await sub._subscribe(list(nodes.values()), mfilter=every_sample, queuesize=1000, sampling_interval=0)
        log.info("Subscribed to %d tags; collecting...", len(nodes))

        total = 0
        while not handler.shift_complete.is_set():
            await asyncio.sleep(0.5)
            total += flush(conn, handler)
        await asyncio.sleep(1.0)              # let the last notifications arrive
        total += flush(conn, handler)

        cw = ["Line1", "Checkweigher"]
        eq_packs = await (await objects.get_child([f"{idx}:{x}" for x in cw + ["PackCount"]])).read_value()
        eq_rej = await (await objects.get_child([f"{idx}:{x}" for x in cw + ["RejectCount"]])).read_value()
    finally:
        await client.disconnect()

    rows, rej = conn.execute("SELECT count(*), count(*) FILTER (WHERE rejected) FROM packs").fetchone()
    conn.execute(
        """UPDATE collection_runs SET finished_at = now(), equipment_pack_count = %s,
           equipment_reject_count = %s, rows_collected = %s, rejects_collected = %s WHERE run_id = %s""",
        (eq_packs, eq_rej, rows, rej, run_id))
    conn.commit()
    log.info("Equipment counted %d packs (%d rejects); database has %d (%d rejects). %s",
             eq_packs, eq_rej, rows, rej, "COMPLETE" if rows == eq_packs else "MISSING DATA")

    await asyncio.sleep(1)  # simulator writes the manual check log right after the shift
    load_manual_checks(conn)
    conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    asyncio.run(main())
