"""
Portfolio 5, step 1: inspect the replica set.

Shows each member's role, health, votes/priority, how far behind the primary
it is, and how much history the oplog can hold (how long a member can be
offline and still catch up without a full resync).

    python 01_cluster_status.py > evidence/01_cluster_status.txt
"""
from datetime import datetime, timezone

from pymongo import WriteConcern

from common import DB_NAME, args, cluster_client, heading, node_client


def main():
    a = args(__doc__)
    client = cluster_client(a)
    admin = client.admin

    heading("1. Topology as the driver discovers it (hello command)")
    hello = admin.command("hello")
    print(f"  replica set : {hello['setName']}")
    print(f"  primary     : {hello['primary']}")
    print(f"  all hosts   : {', '.join(hello['hosts'])}")

    heading("2. Member configuration (rs.conf)")
    conf = admin.command("replSetGetConfig")["config"]
    print(f"  {'member':<22}{'votes':>6}{'priority':>10}{'hidden':>8}")
    for m in conf["members"]:
        print(f"  {m['host']:<22}{m['votes']:>6}{m['priority']:>10}{str(m.get('hidden', False)):>8}")
    print(f"  election timeout: {conf['settings']['electionTimeoutMillis']} ms; "
          f"majority = {len(conf['members']) // 2 + 1} of {len(conf['members'])} voting members")

    heading("3. Member state and replication lag (rs.status)")
    # On an idle set, optimes only advance with periodic no-op writes, which makes
    # lag look like ~10 s. A probe write acknowledged by all members first gives
    # every member a fresh, comparable optime.
    client[DB_NAME].get_collection("probe", write_concern=WriteConcern(w=len(hello["hosts"]),
                                   wtimeout=5000)).insert_one({"at": datetime.now(timezone.utc)})
    st = admin.command("replSetGetStatus")

    def own_optime(host):
        """Ask the member itself how far it has applied (more precise than the
        primary's heartbeat-based view of the other members)."""
        with node_client(host) as c:
            me = next(m for m in c.admin.command("replSetGetStatus")["members"] if m.get("self"))
            return me["optimeDate"]

    optimes = {m["name"]: own_optime(m["name"]) for m in st["members"] if m["health"] == 1}
    primary_optime = next(optimes[m["name"]] for m in st["members"] if m["stateStr"] == "PRIMARY")
    print(f"  {'member':<22}{'state':<12}{'health':>7}{'lag (s)':>10}  last heartbeat")
    for m in st["members"]:
        lag = (primary_optime - optimes[m["name"]]).total_seconds() if m["name"] in optimes else float("nan")
        hb = m.get("lastHeartbeat")
        hb = hb.strftime("%H:%M:%S") if isinstance(hb, datetime) else "(self)"
        print(f"  {m['name']:<22}{m['stateStr']:<12}{m['health']:>7.0f}{lag:>10.1f}  {hb}")

    heading("4. Oplog window (how long a member may be offline and still catch up)")
    local = client.get_database("local")
    oplog = local["oplog.rs"]
    first = oplog.find().sort("$natural", 1).limit(1).next()["ts"].as_datetime()
    last = oplog.find().sort("$natural", -1).limit(1).next()["ts"].as_datetime()
    size = local.command("collStats", "oplog.rs")
    print(f"  oplog max size : {size['maxSize'] / 1024 / 1024:,.0f} MB, "
          f"used {size['size'] / 1024 / 1024:,.1f} MB")
    print(f"  oldest entry   : {first:%Y-%m-%d %H:%M:%S}  newest: {last:%Y-%m-%d %H:%M:%S}")
    print("  (the window grows until the oplog is full; size it to cover the longest\n"
          "   expected outage or maintenance of a member)")


if __name__ == "__main__":
    main()
