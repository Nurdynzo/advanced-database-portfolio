"""
Portfolio 5, step 4: consistency BETWEEN services (database-per-service).

When payment-service confirms a payment, other services (notifications,
facility, investor read models) learn about it through a message broker.
The database write and the publish are two different systems, so one can
succeed while the other fails.

  E1  Fire-and-forget (pattern found in the original system): write, then
      publish; if the broker is down the error is logged and the event is lost.
  E2  Transactional outbox: the payment update and an `outbox` event are
      written in ONE MongoDB transaction. A relay, driven by a CHANGE STREAM
      on the outbox, publishes pending events and retries until the broker
      is back. The consumer is idempotent (unique eventId), so a relay crash
      between "publish" and "mark sent" causes a harmless duplicate.

    python 04_outbox_change_stream.py > evidence/04_outbox.txt
"""
import threading
import time
import uuid
from datetime import datetime, timezone

from bson.decimal128 import Decimal128
from pymongo.errors import DuplicateKeyError

from common import DB_NAME, args, cluster_client, heading, log


class Broker:
    """Stand-in for RabbitMQ: delivers to the consumer's inbox when up."""

    def __init__(self, inbox):
        self.up = True
        self.inbox = inbox

    def publish(self, event):
        if not self.up:
            raise ConnectionError("broker unavailable")
        try:                                   # idempotent consumer: unique eventId
            self.inbox.insert_one({"eventId": event["eventId"], "type": event["type"],
                                   "reference": event["payload"]["reference"],
                                   "receivedAt": datetime.now(timezone.utc)})
            return "delivered"
        except DuplicateKeyError:
            return "duplicate ignored by consumer"


def new_payment(db, ref):
    db.payments.insert_one({"reference": ref, "tenancyId": "ten_2",
                            "amount": Decimal128("200000"), "status": "pending"})


# ---------------------------------------------------------------------------
def e1_fire_and_forget(db, broker):
    heading("E1 Fire-and-forget: update the payment, then publish (broker DOWN)")
    broker.up = False
    refs = [f"FF-{i}" for i in range(1, 4)]
    for ref in refs:
        new_payment(db, ref)
        db.payments.update_one({"reference": ref}, {"$set": {"status": "succeeded"}})
        try:
            broker.publish({"eventId": str(uuid.uuid4()), "type": "payment.succeeded",
                            "payload": {"reference": ref}})
        except ConnectionError as e:
            log(f"{ref}: payment saved, publish failed ({e}) -> logged, event dropped")
    broker.up = True
    log("broker is back up")
    saved = db.payments.count_documents({"reference": {"$in": refs}, "status": "succeeded"})
    delivered = db.inbox.count_documents({"reference": {"$in": refs}})
    print(f"\n  E1 RESULT: payments succeeded = {saved}, events received by consumer = {delivered}"
          f"\n             -> {saved - delivered} tenants never get a receipt; other services"
          " never learn of these payments.")


# ---------------------------------------------------------------------------
class Relay:
    """Publishes pending outbox events. Woken by a change stream; also sweeps
    for anything still pending (e.g. after the broker was down)."""

    def __init__(self, db, broker, crash_after_publish=None):
        self.db, self.broker = db, broker
        self.crash_after_publish = crash_after_publish
        self.stop = threading.Event()
        self.crashed = False

    def deliver_pending(self):
        for ev in self.db.outbox.find({"status": "pending"}).sort("createdAt", 1):
            try:
                result = self.broker.publish(ev)
            except ConnectionError:
                log(f"publish {ev['payload']['reference']} failed: broker down, will retry", "relay")
                return
            log(f"published {ev['payload']['reference']}: {result}", "relay")
            if ev["payload"]["reference"] == self.crash_after_publish:
                self.crash_after_publish = None
                self.crashed = True
                log("RELAY CRASHED after publishing, before marking the event sent", "relay")
                raise SystemExit
            self.db.outbox.update_one({"_id": ev["_id"]},
                                      {"$set": {"status": "sent", "sentAt": datetime.now(timezone.utc)}})

    def run(self):
        try:
            self.deliver_pending()                     # catch up on start
            with self.db.outbox.watch([{"$match": {"operationType": "insert"}}],
                                      max_await_time_ms=500) as stream:
                last_sweep = time.time()
                while not self.stop.is_set():
                    change = stream.try_next()
                    if change is not None:
                        log(f"change stream: new outbox event "
                            f"{change['fullDocument']['payload']['reference']}", "relay")
                        self.deliver_pending()
                    elif time.time() - last_sweep > 1:
                        self.deliver_pending()       # periodic retry of pending events
                        last_sweep = time.time()
        except SystemExit:
            pass

    def start(self):
        self.thread = threading.Thread(target=self.run)
        self.thread.start()
        return self


def confirm_with_outbox(client, ref):
    db = client[DB_NAME]
    with client.start_session() as s:
        def body(session):
            db.payments.update_one({"reference": ref, "status": "pending"},
                                   {"$set": {"status": "succeeded"}}, session=session)
            db.outbox.insert_one({"eventId": str(uuid.uuid4()), "type": "payment.succeeded",
                                  "payload": {"reference": ref}, "status": "pending",
                                  "createdAt": datetime.now(timezone.utc)}, session=session)
        s.with_transaction(body)
    log(f"{ref}: payment + outbox event committed in one transaction")


def e2_outbox(client, broker):
    db = client[DB_NAME]
    heading("E2 Transactional outbox + change-stream relay")
    refs = [f"OB-{i}" for i in range(1, 5)]
    for ref in refs:
        new_payment(db, ref)

    relay = Relay(db, broker, crash_after_publish="OB-4").start()
    time.sleep(1)                                  # let the change stream open

    broker.up = False
    log("broker DOWN")
    for ref in refs[:3]:
        confirm_with_outbox(client, ref)
    time.sleep(2.5)
    broker.up = True
    log("broker UP again")
    time.sleep(2)

    log("--- relay crash test ---")
    confirm_with_outbox(client, "OB-4")
    relay.thread.join(timeout=5)
    log("relay restarted (new process); it finds OB-4 still 'pending'")
    relay2 = Relay(db, broker).start()
    time.sleep(2)
    relay2.stop.set(); relay2.thread.join()

    pending = db.outbox.count_documents({"status": "pending"})
    delivered = db.inbox.count_documents({"reference": {"$in": refs}})
    print(f"\n  E2 RESULT: payments succeeded = {len(refs)}, events received = {delivered}, "
          f"outbox still pending = {pending}"
          "\n             -> every event delivered exactly once in effect (at-least-once"
          "\n                delivery + idempotent consumer), despite the outage and the crash.")


def main():
    a = args(__doc__)
    client = cluster_client(a)
    db = client[DB_NAME]
    for c in ("payments", "outbox", "inbox"):
        db[c].drop()
    db.inbox.create_index("eventId", unique=True)
    db.outbox.create_index([("status", 1), ("createdAt", 1)])
    broker = Broker(db.inbox)

    e1_fire_and_forget(db, broker)
    e2_outbox(client, broker)


if __name__ == "__main__":
    main()
