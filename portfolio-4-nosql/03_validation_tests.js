// =====================================================================
// Portfolio 4, step 3: integrity tests, the document-model counterpart of
// Portfolio 1's 03_constraint_tests.sql. Each test states what we expect;
// the output shows what MongoDB actually did.
//   mongosh "mongodb://localhost:27017/?directConnection=true" --quiet 03_validation_tests.js
// =====================================================================
db = db.getSiblingDB("lodgely");
const D = v => NumberDecimal(String(v));
const day = s => ISODate(s + "T00:00:00Z");

function test(label, expectRejected, fn) {
  let outcome, detail = "";
  try {
    const r = fn();
    outcome = "ACCEPTED";
    if (r && r.modifiedCount === 0 && r.matchedCount !== undefined) detail = "(matched 0 docs)";
  } catch (e) {
    outcome = "REJECTED";
    detail = e.code === 11000 ? "E11000 duplicate key: " + Object.keys(e.keyPattern || {}).join(",")
           : e.code === 121   ? "121 Document failed validation"
           : `${e.code} ${e.message.split("\n")[0]}`;
  }
  const ok = (outcome === "REJECTED") === expectRejected;
  print(`${ok ? "as expected  " : "UNEXPECTED   "} ${label}\n      -> ${outcome} ${detail}`);
}

const newTenancy = (over) => Object.assign({
  _id: "ten_test", buildingId: "bld_palm_court", unitId: "u_pc_a1", unitLabel: "A1",
  tenant: { userId: "usr_chioma", fullName: "Chioma Okafor" },
  leaseStart: day("2026-06-01"), leaseEnd: day("2027-06-01"),
  rent: { amount: D(1800000), cycle: "annual" }, status: "pending", cycles: [] }, over);

print("=== Enforced by unique indexes ===");
test("V1  duplicate email differing only by case", true, () =>
  db.users.insertOne({ _id: "usr_dup", email: "AMAKA.OBI@example.com", fullName: "Dup", createdAt: new Date() }));
test("V2  second live tenancy on unit A1 (partial unique index)", true, () =>
  db.tenancies.insertOne(newTenancy({})));
test("V3  NEXT tenant's lease on A1 starting after the current one ends", true, () =>
  db.tenancies.insertOne(newTenancy({ leaseStart: day("2027-01-01"), leaseEnd: day("2028-01-01") })));
print("      (V3 shows a LIMITATION: this lease does NOT overlap and is legitimate, but the index\n" +
      "       allows only one live tenancy per unit at all. PostgreSQL's range exclusion\n" +
      "       constraint accepted it in Portfolio 1.)");

print("\n=== Enforced by $jsonSchema / $expr validators ===");
test("V4  two units labelled A1 in the same building", true, () =>
  db.buildings.updateOne({ _id: "bld_palm_court" },
    { $push: { units: { _id: "u_pc_dup", label: "A1", categoryId: "cat_pc_2bed" } } }));
test("V5  unit using another building's category", true, () =>
  db.buildings.updateOne({ _id: "bld_palm_court" },
    { $push: { units: { _id: "u_pc_x9", label: "X9", categoryId: "cat_ch_studio" } } }));
test("V6  second ACTIVE facility manager", true, () =>
  db.buildings.updateOne({ _id: "bld_palm_court" },
    { $push: { staff: { userId: "usr_amaka", role: "facility_manager", status: "active" } } }));
test("V7  lease that ends before it starts", true, () =>
  db.tenancies.insertOne(newTenancy({ unitId: "u_pc_a2", leaseStart: day("2027-01-01"), leaseEnd: day("2026-01-01") })));
test("V8  succeeded payment without confirmedAt", true, () =>
  db.payments.updateOne({ _id: "pay_4" }, { $set: { status: "succeeded" } }));
test("V9  paying a cycle beyond its amount due", true, () =>
  db.tenancies.updateOne({ _id: "ten_2", "cycles._id": "t2_2026_06" },
    { $set: { "cycles.$.amountPaid": D(250000) } }));
test("V10 budget line pushed below zero remaining", true, () =>
  db.budgets.updateOne({ _id: "bud_pc_2026", "lines._id": "bl_plumbing" },
    { $inc: { "lines.$.remaining": D(-400000), "lines.$.committedAmount": D(400000) } }));
test("V11 invalid tenancy status", true, () =>
  db.tenancies.insertOne(newTenancy({ unitId: "u_pc_a2", status: "squatting" })));
test("V12 negative rent", true, () =>
  db.tenancies.insertOne(newTenancy({ unitId: "u_pc_a2", rent: { amount: D(-5), cycle: "annual" } })));

print("\n=== NOT enforced: references between documents ===");
test("V13 tenancy pointing at a building and unit that do not exist", false, () =>
  db.tenancies.insertOne(newTenancy({ _id: "ten_ghost", buildingId: "bld_nowhere", unitId: "u_nowhere" })));
test("V14 deleting a user who is still a tenant", false, () =>
  db.users.deleteOne({ _id: "usr_musa" }));
print("      (V13-V14 succeed: MongoDB has no foreign keys. PostgreSQL rejected both in\n" +
      "       Portfolio 1. Here, the application or a scheduled consistency check must catch them.)");

// Undo the changes the accepted tests made, so later scripts start from the seed state.
db.tenancies.deleteMany({ _id: { $in: ["ten_test", "ten_ghost"] } });
db.users.insertOne({ _id: "usr_musa", email: "musa.ibrahim@example.com", fullName: "Musa Ibrahim",
                     phone: "+2348010000004", createdAt: new Date() });
print("\n(cleanup done: accepted test documents removed, deleted user restored)");

print("\nWhy a document was rejected (example: V9), from errInfo:");
try {
  db.tenancies.updateOne({ _id: "ten_2", "cycles._id": "t2_2026_06" },
                         { $set: { "cycles.$.amountPaid": D(250000) } });
} catch (e) { printjson(e.errInfo.details); }
