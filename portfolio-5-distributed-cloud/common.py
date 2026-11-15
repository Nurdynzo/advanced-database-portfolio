"""Shared helpers for the Portfolio 5 scripts."""
import argparse
import threading
import time

from pymongo import MongoClient

# Default: the three containers from docker-compose.yml. Each listens on its own
# port, and the host maps mongo1/mongo2/mongo3 to 127.0.0.1 (see README), so the
# same names work inside the Docker network and from your machine.
DEFAULT_HOSTS = "mongo1:27017,mongo2:27018,mongo3:27019"
DEFAULT_SET = "rs0"
DB_NAME = "lodgely_dist"

_T0 = time.perf_counter()
_LOCK = threading.Lock()


def args(description, extra=None):
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--hosts", default=DEFAULT_HOSTS, help="comma-separated host:port list")
    p.add_argument("--replica-set", default=DEFAULT_SET)
    if extra:
        extra(p)
    return p.parse_args()


def cluster_client(a, **kw):
    """Replica-set-aware client: follows the primary through failovers."""
    kw.setdefault("serverSelectionTimeoutMS", 15000)
    return MongoClient(f"mongodb://{a.hosts}/?replicaSet={a.replica_set}", **kw)


def node_client(host, **kw):
    """Connection to ONE specific member, whatever its role."""
    kw.setdefault("serverSelectionTimeoutMS", 5000)
    return MongoClient(f"mongodb://{host}/?directConnection=true", **kw)


def reset_clock():
    global _T0
    _T0 = time.perf_counter()


def log(msg, who=""):
    with _LOCK:
        prefix = f"{who}: " if who else ""
        print(f"  [+{time.perf_counter() - _T0:6.2f}s] {prefix}{msg}", flush=True)


def heading(title):
    print("\n" + "=" * 76 + f"\n{title}\n" + "=" * 76, flush=True)
    reset_clock()


def members(client):
    """[(name, state)] from replSetGetStatus."""
    st = client.admin.command("replSetGetStatus")
    return [(m["name"], m["stateStr"]) for m in st["members"]]


def primary_and_secondaries(client):
    ms = members(client)
    primary = next(n for n, s in ms if s == "PRIMARY")
    return primary, [n for n, s in ms if s == "SECONDARY"]


def find_primary(hosts, exclude=None):
    """Ask each member directly; return the one that reports itself primary."""
    for h in hosts.split(","):
        if h == exclude:
            continue
        try:
            with node_client(h, serverSelectionTimeoutMS=1000) as c:
                if c.admin.command("hello").get("isWritablePrimary"):
                    return h
        except Exception:
            pass
    return None
