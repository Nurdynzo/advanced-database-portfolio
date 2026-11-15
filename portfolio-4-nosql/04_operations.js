// =====================================================================
// Portfolio 4, step 4: the key Lodgely operations on the document model.
// Run after 01_collections.js and 02_seed.js:
//   mongosh "mongodb://localhost:27017/?directConnection=true" --quiet 04_operations.js
// =====================================================================
db = db.getSiblingDB("lodgely");
const D = v => NumberDecimal(String(v));
const num = d => Number(d.toString());          // demo only: money values here are whole naira
const section = t => print(`\n${"=".repeat(74)}\n${t}\n${"=".repeat(74)}`);

// ---------------------------------------------------------------------
section("O1  Read patterns: one document answers a whole screen");
// ---------------------------------------------------------------------
print("Building page (units, categories, staff) = ONE read, no joins:");
printjson(db.buildings.findOne({ _id: "bld_palm_court" },
  { name: 1, "units.label": 1, "unitCategories.name": 1, staff: 1 }));

print("\nTenant statement = ONE read; balances computed inside the query:");
printjson(db.tenancies.aggregate([
  { $match: { _id: "ten_2" } },
  { $project: {
      tenant: "$tenant.fullName", unit: "$unitLabel",
      cycles: { $map: { input: "$cycles", as: "c", in: {
        month: { $dateToString: { date: "$$c.periodStart", format: "%Y-%m" } },
        due: "$$c.amountDue", paid: "$$c.amountPaid",
        balance: { $subtract: ["$$c.amountDue", "$$c.amountPaid"] } } } },
      outstanding: { $sum: { $map: { input: "$cycles",
        in: { $subtract: ["$$this.amountDue", "$$this.amountPaid"] } } } } } }
]).toArray()[0]);

// ---------------------------------------------------------------------
section("O2  Confirm a payment: multi-document ACID transaction");
// The status change, the allocations on the payment and the amountPaid on
// the tenancy's cycles commit together or not at all. The first statement
// is an atomic conditional update, so a duplicate confirmation is a no-op.
// ---------------------------------------------------------------------
function confirmPayment(reference) {
  const session = db.getMongo().startSession();
  const payments = session.getDatabase("lodgely").payments;
  const tenancies = session.getDatabase("lodgely").tenancies;
  let outcome;
  try {
    session.withTransaction(() => {
      const pay = payments.findOneAndUpdate(
        { reference, status: "pending" },                       // check + claim in one step
        { $set: { status: "succeeded", confirmedAt: new Date() } },
        { returnDocument: "after" });
      if (pay === null) { outcome = "skipped: not pending (already processed)"; return; }

      // Arrears first: oldest unpaid cycles first.
      const ten = tenancies.findOne({ _id: pay.tenancyId });
      let remaining = num(pay.amount);
      const allocations = [];
      for (const c of ten.cycles.slice().sort((a, b) => a.dueDate - b.dueDate)) {
        const owed = num(c.amountDue) - num(c.amountPaid);
        if (remaining <= 0 || owed <= 0) continue;
        const part = Math.min(remaining, owed);
        tenancies.updateOne({ _id: ten._id, "cycles._id": c._id },
                            { $inc: { "cycles.$.amountPaid": D(part) } });
        allocations.push({ cycleId: c._id, amount: D(part) });
        remaining -= part;
      }
      payments.updateOne({ _id: pay._id }, { $set: { allocations } });
      outcome = "credited: " + allocations.map(a => `${a.cycleId}=${a.amount}`).join(", ");
    }, { readConcern: { level: "snapshot" }, writeConcern: { w: "majority" } });
  } catch (e) {
    outcome = `ABORTED (${e.code || ""} ${e.codeName || e.message.split("\n")[0]}) -> nothing was saved`;
  } finally {
    session.endSession();
  }
  return outcome;
}

print("First confirmation (webhook):    " + confirmPayment("PSK-0004-SAMPLE"));
print("Second confirmation (verify):    " + confirmPayment("PSK-0004-SAMPLE"));
const p4 = db.payments.findOne({ reference: "PSK-0004-SAMPLE" });
print(`Payment ${p4.reference}: status=${p4.status}, allocated=` +
      p4.allocations.reduce((s, a) => s + num(a.amount), 0));

// ---------------------------------------------------------------------
section("O3  Transaction rollback: a failure halfway undoes the earlier writes");
// A ₦700,000 payment for tenancy 2 (₦600,000 outstanding) would overpay the
// last cycle; the validator rejects that write, so the whole transaction,
// including the status change made before it, is rolled back.
// ---------------------------------------------------------------------
db.payments.insertOne({ _id: "pay_5", reference: "PSK-0005-SAMPLE", tenancyId: "ten_2",
  buildingId: "bld_palm_court", amount: D(700000), channel: "card", status: "pending",
  initiatedAt: new Date(), allocations: [] });

function confirmOverpayment(reference) {
  const session = db.getMongo().startSession();
  const s = session.getDatabase("lodgely");
  try {
    session.withTransaction(() => {
      s.payments.updateOne({ reference, status: "pending" },
                           { $set: { status: "succeeded", confirmedAt: new Date() } });
      print("  inside transaction: payment marked succeeded");
      // Naive allocation: put everything on the oldest unpaid cycle.
      s.tenancies.updateOne({ _id: "ten_2", "cycles._id": "t2_2026_08" },
                            { $inc: { "cycles.$.amountPaid": D(700000) } });   // > amountDue
    });
    return "committed";
  } catch (e) {
    return `aborted: error ${e.code} (document failed validation)`;
  } finally { session.endSession(); }
}
print("Result: " + confirmOverpayment("PSK-0005-SAMPLE"));
print("Status after abort: " + db.payments.findOne({ _id: "pay_5" }).status +
      "   (the earlier status change was rolled back)");
db.payments.deleteOne({ _id: "pay_5" });

// ---------------------------------------------------------------------
section("O4  Budget reservation: ONE atomic conditional update (no read-then-write)");
// The guard `remaining >= total` and the $inc happen in a single
// document operation, so two approvals cannot both pass the check.
// ---------------------------------------------------------------------
function approveRequisition(reqId) {
  const session = db.getMongo().startSession();
  const s = session.getDatabase("lodgely");
  let outcome;
  try {
    session.withTransaction(() => {
      const req = s.requisitions.findOne({ _id: reqId, status: "submitted" });
      if (!req) { outcome = "skipped: not submitted"; return; }
      const reserved = s.budgets.updateOne(
        { _id: req.budgetId,
          lines: { $elemMatch: { _id: req.budgetLineId, remaining: { $gte: req.total } } } },
        { $inc: { "lines.$.remaining": D(-num(req.total)),
                  "lines.$.committedAmount": req.total } });
      const status = reserved.modifiedCount === 1 ? "approved" : "rejected";
      s.requisitions.updateOne({ _id: reqId, version: req.version },
                               { $set: { status }, $inc: { version: 1 } });
      outcome = `${status} (requested ${req.total})`;
    });
  } finally { session.endSession(); }
  return outcome;
}
print("req_3: " + approveRequisition("req_3"));
print("req_4: " + approveRequisition("req_4"));
printjson(db.budgets.findOne({ _id: "bud_pc_2026" }, { _id: 0, lines: { $elemMatch: { _id: "bl_plumbing" } } }));

// ---------------------------------------------------------------------
section("O5  Optimistic concurrency: version field prevents a lost update");
// ---------------------------------------------------------------------
const seenByA = db.requisitions.findOne({ _id: "req_1" });
const seenByB = db.requisitions.findOne({ _id: "req_1" });
const editA = db.requisitions.updateOne({ _id: "req_1", version: seenByA.version },
  { $set: { purpose: "Quarterly generator servicing (Q3)" }, $inc: { version: 1 } });
const editB = db.requisitions.updateOne({ _id: "req_1", version: seenByB.version },
  { $set: { purpose: "Generator servicing - urgent" }, $inc: { version: 1 } });
print(`A's edit: matched ${editA.matchedCount}`);
print(`B's edit (based on stale version ${seenByB.version}): matched ${editB.matchedCount} -> B must reload and retry`);
print("Stored purpose: " + db.requisitions.findOne({ _id: "req_1" }).purpose + "   (A's edit was not overwritten)");

// ---------------------------------------------------------------------
section("O6  Aggregation pipelines");
// ---------------------------------------------------------------------
print("a) Building wallet: succeeded payments minus payouts ($unionWith):");
printjson(db.payments.aggregate([
  { $match: { buildingId: "bld_palm_court", status: "succeeded" } },
  { $project: { _id: 0, kind: "income", amount: 1 } },
  { $unionWith: { coll: "payouts", pipeline: [
      { $match: { buildingId: "bld_palm_court", status: { $in: ["pending", "paid"] } } },
      { $project: { _id: 0, kind: "withdrawn", amount: { $multiply: ["$amount", -1] } } } ] } },
  { $group: { _id: "$kind", total: { $sum: "$amount" } } },
  { $group: { _id: null, lines: { $push: { k: "$_id", v: "$total" } },
              availableBalance: { $sum: "$total" } } },
  { $project: { _id: 0, breakdown: { $arrayToObject: "$lines" }, availableBalance: 1 } }
]).toArray());

print("\nb) Overdue report for Palm Court ($unwind the embedded cycles):");
printjson(db.tenancies.aggregate([
  { $match: { buildingId: "bld_palm_court", status: "active" } },
  { $unwind: "$cycles" },
  { $match: { "cycles.dueDate": { $lt: new Date() },
              $expr: { $lt: ["$cycles.amountPaid", "$cycles.amountDue"] } } },
  { $project: { _id: 0, unit: "$unitLabel", tenant: "$tenant.fullName",
                due: { $dateToString: { date: "$cycles.dueDate", format: "%Y-%m-%d" } },
                balance: { $subtract: ["$cycles.amountDue", "$cycles.amountPaid"] } } },
  { $sort: { due: 1 } }
]).toArray());

print("\nc) $lookup (a join) when a snapshot is not stored: requisitions + requester name");
printjson(db.requisitions.aggregate([
  { $match: { buildingId: "bld_palm_court" } },
  { $lookup: { from: "users", localField: "raisedBy", foreignField: "_id", as: "by" } },
  { $project: { _id: 1, purpose: 1, status: 1, total: 1,
                raisedBy: { $first: "$by.fullName" } } }
]).toArray());

// ---------------------------------------------------------------------
section("O7  Full-text search ($text index)");
// ---------------------------------------------------------------------
print("search 'okafor' (whole word):  " +
  JSON.stringify(db.users.find({ $text: { $search: "okafor" } }, { _id: 0, fullName: 1 }).toArray()));
print("search 'kafo'   (fragment):    " +
  JSON.stringify(db.users.find({ $text: { $search: "kafo" } }, { _id: 0, fullName: 1 }).toArray()) +
  "   <- $text matches words, not substrings (contrast: pg_trgm in Portfolio 2)");

// ---------------------------------------------------------------------
section("O8  TTL index: expired visitor passes delete themselves");
// The TTL monitor normally runs every 60 s; shortened here for the demo only.
// ---------------------------------------------------------------------
db.adminCommand({ setParameter: 1, ttlMonitorSleepSecs: 1 });
db.accessPasses.insertMany([
  { code: "PASS-EXPIRING", buildingId: "bld_palm_court", tenantUserId: "usr_chioma",
    visitor: "Plumber", expiresOn: new Date(Date.now() + 2000) },
  { code: "PASS-VALID", buildingId: "bld_palm_court", tenantUserId: "usr_musa",
    visitor: "Family guest", expiresOn: new Date(Date.now() + 7 * 24 * 3600 * 1000) } ]);
print("passes right after insert: " + db.accessPasses.distinct("code").join(", "));
sleep(5000);
print("passes 5 seconds later:   " + db.accessPasses.distinct("code").join(", "));
db.adminCommand({ setParameter: 1, ttlMonitorSleepSecs: 60 });

// ---------------------------------------------------------------------
section("O9  Index evidence: explain() on 100,000 synthetic payments");
// ---------------------------------------------------------------------
const bulk = [];
for (let i = 0; i < 100000; i++) {
  bulk.push({ reference: `SYN-${i}`, tenancyId: `ten_syn_${i % 5000}`,
              buildingId: `bld_syn_${i % 500}`, amount: D(150000),
              channel: "card", status: i % 20 === 0 ? "failed" : "succeeded",
              initiatedAt: new Date(2025, 0, 1 + (i % 600)), confirmedAt: new Date(),
              allocations: [] });
}
// Failed payments must not carry confirmedAt (validator rule), so fix those up.
bulk.forEach(p => { if (p.status !== "succeeded") delete p.confirmedAt; });
db.payments.insertMany(bulk, { ordered: false });

const walletFilter = { buildingId: "bld_syn_250", status: "succeeded" };
function explainSummary(label, hint) {
  const cur = db.payments.find(walletFilter);
  if (hint) cur.hint(hint);
  const e = cur.explain("executionStats");
  const plan = e.queryPlanner.winningPlan;
  const stage = JSON.stringify(plan).match(/"stage":"(COLLSCAN|IXSCAN)"/)[1];
  const s = e.executionStats;
  print(`${label.padEnd(36)} stage=${stage.padEnd(8)} docsExamined=${String(s.totalDocsExamined).padStart(6)}` +
        `  keysExamined=${String(s.totalKeysExamined).padStart(5)}  returned=${s.nReturned}  time=${s.executionTimeMillis} ms`);
}
explainSummary("forced collection scan ($natural)", { $natural: 1 });
explainSummary("with index ix_building_status", "ix_building_status");
db.payments.deleteMany({ reference: /^SYN-/ });
print("(synthetic payments removed)");
