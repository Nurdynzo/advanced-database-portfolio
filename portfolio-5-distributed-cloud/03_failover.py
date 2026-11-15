"""
Portfolio 5, step 3: availability under failure.

A writer records a payment every 100 ms (w:"majority", retryable writes) and a
reader reads the latest payment (readPreference primaryPreferred) while:

  F1  the primary STEPS DOWN (planned maintenance, e.g. upgrading a node)
  F2  the primary CRASHES (container/process killed with --kill-cmd)

For each, the script measures how long writes were blocked, whether any
write failed, whether reads continued, and whether every acknowledged write
survived on the new primary. After F2 it waits for the crashed node to come
back (--start-cmd) and rejoin as a secondary.

    python 03_failover.py > evidence/03_failover.txt
    # defaults: --kill-cmd "docker kill {service}"  --start-cmd "docker start {service}"
    # {service} is the host name (mongo1...), {host} is host:port, {port} the port
"""
import subprocess
import threading
import time
from datetime import datetime, timezone

from bson.decimal128 import Decimal128
from pymongo import ReadPreference, WriteConcern
from pymongo.errors import AutoReconnect, ConnectionFailure, PyMongoError

from common import (DB_NAME, args, cluster_client, find_primary, heading, log,
                    members, node_client, primary_and_secondaries)


class Load:
    """Background writer + reader that record what the application experiences."""

    def __init__(self, client, label):
        self.label = label
        self.payments = client[DB_NAME].get_collection(
            "payments", write_concern=WriteConcern(w="majority", wtimeout=30000))
        self.reader = client[DB_NAME].get_collection(
            "payments", read_preference=ReadPreference.PRIMARY_PREFERRED)
        self.stop = threading.Event()
        self.acked, self.failed, self.write_latencies = [], [], []
        self.reads_ok = self.reads_failed = 0
        self.seq = 0

    def write_loop(self):
        while not self.stop.is_set():
            self.seq += 1
            ref = f"FAILOVER-{self.label}-{self.seq:05d}"
            t = time.perf_counter()
            try:
                self.payments.insert_one({"reference": ref, "tenancyId": "ten_2",
                                          "amount": Decimal128("200000"), "status": "succeeded",
                                          "confirmedAt": datetime.now(timezone.utc)})
                self.acked.append(ref)
                self.write_latencies.append(time.perf_counter() - t)
                if time.perf_counter() - t > 1:
                    log(f"write {ref} acknowledged after {time.perf_counter() - t:.2f} s "
                        "(was waiting for a new primary)", "writer")
            except PyMongoError as e:
                self.failed.append(ref)
                log(f"write {ref} FAILED: {type(e).__name__}", "writer")
            time.sleep(0.1)

    def read_loop(self):
        while not self.stop.is_set():
            try:
                self.reader.find_one(sort=[("_id", -1)])
                self.reads_ok += 1
            except PyMongoError:
                self.reads_failed += 1
            time.sleep(0.1)

    def run(self):
        self.threads = [threading.Thread(target=self.write_loop),
                        threading.Thread(target=self.read_loop)]
        for t in self.threads:
            t.start()

    def finish(self):
        self.stop.set()
        for t in self.threads:
            t.join()


def wait_for_new_primary(hosts, old):
    t = time.perf_counter()
    while True:
        p = find_primary(hosts, exclude=old)
        if p:
            return p, time.perf_counter() - t
        time.sleep(0.1)


def run_cmd(template, host):
    service, port = host.split(":")
    cmd = template.format(service=service, host=host, port=port)
    log(f"$ {cmd}")
    subprocess.run(cmd, shell=True, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def report(load, client, label):
    survived = client[DB_NAME].payments.count_documents(
        {"reference": {"$in": load.acked}})
    print(f"\n  {label} RESULT")
    print(f"    writes acknowledged : {len(load.acked)}   failed: {len(load.failed)}")
    print(f"    longest write wait  : {max(load.write_latencies):.2f} s")
    print(f"    reads ok / failed   : {load.reads_ok} / {load.reads_failed}")
    print(f"    acknowledged writes present on new primary: {survived} of {len(load.acked)}"
          f"  -> {'NO DATA LOSS' if survived == len(load.acked) else 'DATA LOST'}")


def main():
    def extra(p):
        p.add_argument("--kill-cmd", default="docker kill {service}")
        p.add_argument("--start-cmd", default="docker start {service}")
    a = args(__doc__, extra)
    client = cluster_client(a, serverSelectionTimeoutMS=30000, retryWrites=True)
    client[DB_NAME].payments.delete_many({"reference": {"$regex": "^FAILOVER-"}})

    # ------------------------------------------------------------------
    heading("F1 Planned failover: replSetStepDown on the primary")
    old, _ = primary_and_secondaries(client)
    log(f"members: {members(client)}")
    load = Load(client, "F1"); load.run()
    time.sleep(3)
    log(f"stepping down primary {old}")
    try:
        node_client(old).admin.command("replSetStepDown", 30)   # not electable for 30 s
    except (AutoReconnect, ConnectionFailure):
        pass                                                      # connections are closed on step-down
    new, took = wait_for_new_primary(a.hosts, old)
    log(f"new primary {new} elected {took:.2f} s after the step-down")
    time.sleep(3)
    load.finish()
    report(load, client, "F1")

    # ------------------------------------------------------------------
    heading("F2 Unplanned failover: the primary process is killed")
    time.sleep(2)
    old, _ = primary_and_secondaries(client)
    load = Load(client, "F2"); load.run()
    time.sleep(3)
    log(f"killing primary {old}")
    run_cmd(a.kill_cmd, old)
    new, took = wait_for_new_primary(a.hosts, old)
    log(f"new primary {new} elected {took:.2f} s after the crash "
        "(secondaries first wait electionTimeoutMillis = 10 s for missed heartbeats)")
    time.sleep(3)
    load.finish()
    report(load, client, "F2")

    # ------------------------------------------------------------------
    heading("F3 Recovery: the crashed node rejoins")
    run_cmd(a.start_cmd, old)
    log(f"waiting for {old} to rejoin ...")
    deadline = time.time() + 120
    while time.time() < deadline:
        state = dict(members(client)).get(old)
        if state == "SECONDARY":
            log(f"{old} is back as SECONDARY and has caught up via the oplog")
            break
        time.sleep(1)
    else:
        log(f"{old} did not come back; start it manually (e.g. docker start <service>)")
    log(f"final members: {members(client)}")
    print("\n  Note: the old primary did not take its role back. All members have equal\n"
          "  priority, so the cluster keeps the current primary to avoid a second election.")


if __name__ == "__main__":
    main()
