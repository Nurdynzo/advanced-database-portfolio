// =====================================================================
// Portfolio 4, step 2: seed the SAME fictional data as Portfolios 1 and 3,
// reshaped as documents. Readable string _ids are used so the demos are
// easy to follow (the production system uses ObjectIds).
//   mongosh "mongodb://localhost:27017/?directConnection=true" --quiet 02_seed.js
// =====================================================================
db = db.getSiblingDB("lodgely");
const D = v => NumberDecimal(String(v));
const day = s => ISODate(s + "T00:00:00Z");

db.users.insertMany([
  { _id: "usr_amaka",  email: "amaka.obi@example.com",     fullName: "Amaka Obi",     phone: "+2348010000001", createdAt: new Date() },
  { _id: "usr_tunde",  email: "tunde.bello@example.com",   fullName: "Tunde Bello",   phone: "+2348010000002", createdAt: new Date() },
  { _id: "usr_ngozi",  email: "ngozi.eze@example.com",     fullName: "Ngozi Eze",     phone: "+2348010000003", createdAt: new Date() },
  { _id: "usr_musa",   email: "musa.ibrahim@example.com",  fullName: "Musa Ibrahim",  phone: "+2348010000004", createdAt: new Date() },
  { _id: "usr_chioma", email: "chioma.okafor@example.com", fullName: "Chioma Okafor", phone: "+2348010000005", createdAt: new Date() },
  { _id: "usr_femi",   email: "femi.adeyemi@example.com",  fullName: "Femi Adeyemi",  phone: "+2348010000006", createdAt: new Date() }
]);

db.organisations.insertMany([
  { _id: "org_greenfield", name: "Greenfield Estates Ltd", handle: "greenfield-estates",
    members: [ { userId: "usr_amaka", role: "owner",  joinedAt: new Date() },
               { userId: "usr_tunde", role: "member", joinedAt: new Date() },
               { userId: "usr_ngozi", role: "member", joinedAt: new Date() } ] },
  { _id: "org_harbour", name: "Harbour Point Properties", handle: "harbour-point", members: [] }
]);

db.buildings.insertMany([
  { _id: "bld_palm_court", organisationId: "org_greenfield", name: "Palm Court",
    address: { line: "12 Example Crescent", city: "Abuja", state: "FCT" },
    unitCategories: [ { _id: "cat_pc_2bed", name: "2-Bedroom Flat", bedrooms: NumberInt(2) },
                      { _id: "cat_pc_3bed", name: "3-Bedroom Flat", bedrooms: NumberInt(3) } ],
    units: [ { _id: "u_pc_a1", label: "A1", categoryId: "cat_pc_2bed", floor: 1 },
             { _id: "u_pc_a2", label: "A2", categoryId: "cat_pc_2bed", floor: 1 },
             { _id: "u_pc_b1", label: "B1", categoryId: "cat_pc_3bed", floor: 2 } ],
    staff: [ { userId: "usr_tunde", role: "facility_manager", status: "active" },
             { userId: "usr_ngozi", role: "finance",          status: "active" } ] },
  { _id: "bld_cedar", organisationId: "org_greenfield", name: "Cedar Heights",
    address: { line: "4 Sample Close", city: "Abuja", state: "FCT" },
    unitCategories: [ { _id: "cat_ch_studio", name: "Studio", bedrooms: NumberInt(0) } ],
    units: [ { _id: "u_ch_s1", label: "S1", categoryId: "cat_ch_studio", floor: 0 },
             { _id: "u_ch_s2", label: "S2", categoryId: "cat_ch_studio", floor: 0 } ],
    staff: [ { userId: "usr_tunde", role: "facility_manager", status: "active" } ] }
]);

// Monthly cycles for tenancy 2, March - September 2026.
const monthly = [];
for (let m = 3; m <= 9; m++) {
  const mm = String(m).padStart(2, "0"), next = String(m + 1).padStart(2, "0");
  monthly.push({ _id: `t2_2026_${mm}`, chargeType: "rent",
                 periodStart: day(`2026-${mm}-01`), periodEnd: day(`2026-${next}-01`),
                 dueDate: day(`2026-${mm}-01`), amountDue: D(200000),
                 amountPaid: D(m <= 5 ? 200000 : 0) });     // March-May paid
}

db.tenancies.insertMany([
  { _id: "ten_1", buildingId: "bld_palm_court", unitId: "u_pc_a1", unitLabel: "A1",
    tenant: { userId: "usr_musa", fullName: "Musa Ibrahim", email: "musa.ibrahim@example.com" },
    leaseStart: day("2026-01-01"), leaseEnd: day("2027-01-01"),
    rent: { amount: D(1800000), cycle: "annual" }, serviceCharge: D(250000), status: "active",
    cycles: [
      { _id: "t1_2026_rent", chargeType: "rent", periodStart: day("2026-01-01"), periodEnd: day("2027-01-01"),
        dueDate: day("2026-01-01"), amountDue: D(1800000), amountPaid: D(1800000) },
      { _id: "t1_2026_sc", chargeType: "service_charge", periodStart: day("2026-01-01"), periodEnd: day("2027-01-01"),
        dueDate: day("2026-01-01"), amountDue: D(250000), amountPaid: D(250000) } ] },
  { _id: "ten_2", buildingId: "bld_palm_court", unitId: "u_pc_b1", unitLabel: "B1",
    tenant: { userId: "usr_chioma", fullName: "Chioma Okafor", email: "chioma.okafor@example.com" },
    leaseStart: day("2026-03-01"), leaseEnd: day("2027-03-01"),
    rent: { amount: D(200000), cycle: "monthly" }, serviceCharge: D(20000), status: "active",
    cycles: monthly },
  { _id: "ten_3", buildingId: "bld_cedar", unitId: "u_ch_s1", unitLabel: "S1",
    tenant: { userId: "usr_femi", fullName: "Femi Adeyemi", email: "femi.adeyemi@example.com" },
    leaseStart: day("2025-06-01"), leaseEnd: day("2026-06-01"),
    rent: { amount: D(450000), cycle: "quarterly" }, serviceCharge: D(0), status: "ended",
    cycles: [] }
]);

db.payments.insertMany([
  { _id: "pay_1", reference: "PSK-0001-SAMPLE", tenancyId: "ten_1", buildingId: "bld_palm_court",
    amount: D(2050000), channel: "bank_transfer", status: "succeeded",
    initiatedAt: ISODate("2026-01-02T09:10:00Z"), confirmedAt: ISODate("2026-01-02T09:15:00Z"),
    allocations: [ { cycleId: "t1_2026_rent", amount: D(1800000) },
                   { cycleId: "t1_2026_sc",   amount: D(250000) } ] },
  { _id: "pay_2", reference: "PSK-0002-SAMPLE", tenancyId: "ten_2", buildingId: "bld_palm_court",
    amount: D(200000), channel: "card", status: "succeeded",
    initiatedAt: ISODate("2026-03-01T08:00:00Z"), confirmedAt: ISODate("2026-03-01T08:00:30Z"),
    allocations: [ { cycleId: "t2_2026_03", amount: D(200000) } ] },
  { _id: "pay_3", reference: "PSK-0003-SAMPLE", tenancyId: "ten_2", buildingId: "bld_palm_court",
    amount: D(400000), channel: "card", status: "succeeded",
    initiatedAt: ISODate("2026-05-03T17:40:00Z"), confirmedAt: ISODate("2026-05-03T17:40:40Z"),
    allocations: [ { cycleId: "t2_2026_04", amount: D(200000) },
                   { cycleId: "t2_2026_05", amount: D(200000) } ] },
  { _id: "pay_4", reference: "PSK-0004-SAMPLE", tenancyId: "ten_2", buildingId: "bld_palm_court",
    amount: D(200000), channel: "ussd", status: "pending",
    initiatedAt: ISODate("2026-06-02T10:00:00Z"), allocations: [] }
]);

db.payouts.insertMany([
  { _id: "po_1", reference: "PO-0001-SAMPLE", buildingId: "bld_palm_court", requestedBy: "usr_tunde",
    amount: D(1500000), platformFee: D(15000), status: "pending", requestedAt: new Date() },
  { _id: "po_2", reference: "PO-0002-SAMPLE", buildingId: "bld_palm_court", requestedBy: "usr_tunde",
    amount: D(200000), platformFee: D(2000), status: "pending", requestedAt: new Date() }
]);

db.budgets.insertOne({
  _id: "bud_pc_2026", buildingId: "bld_palm_court", fiscalYear: NumberInt(2026),
  lines: [
    { _id: "bl_generator", itemName: "Generator maintenance",
      expectedAmount: D(600000), committedAmount: D(215000), remaining: D(385000) },
    { _id: "bl_plumbing", itemName: "Plumbing repairs",
      expectedAmount: D(300000), committedAmount: D(0), remaining: D(300000) } ]
});

// mongosh's Decimal128 has no arithmetic, so totals are computed with plain numbers.
const reqDoc = (id, line, purpose, status, items) => {
  const total = items.reduce((s, i) => s + i.q * i.p, 0);
  return { _id: id, buildingId: "bld_palm_court", budgetId: "bud_pc_2026", budgetLineId: line,
           raisedBy: "usr_tunde", purpose, status,
           items: items.map((i, n) => ({ lineNo: n + 1, description: i.d,
                                         quantity: D(i.q), unitPrice: D(i.p) })),
           total: D(total), version: NumberInt(1), createdAt: new Date() };
};
db.requisitions.insertMany([
  reqDoc("req_1", "bl_generator", "Quarterly generator servicing", "approved",
         [ { d: "Engine oil (litres)", q: 20, p: 6500 }, { d: "Technician labour", q: 1, p: 85000 } ]),
  reqDoc("req_2", "bl_plumbing", "Fix leaking riser in Block B", "rejected",
         [ { d: "PVC pipe 2-inch", q: 6, p: 12000 } ]),
  reqDoc("req_3", "bl_plumbing", "Replace water pump", "submitted",
         [ { d: "Submersible pump, installed", q: 1, p: 180000 } ]),
  reqDoc("req_4", "bl_plumbing", "Re-pipe Block A bathrooms", "submitted",
         [ { d: "Bathroom re-piping (per flat)", q: 3, p: 60000 } ])
]);

print("Seeded document counts:");
["users", "organisations", "buildings", "tenancies", "payments", "payouts", "budgets", "requisitions"]
  .forEach(c => print(`  ${c.padEnd(14)} ${db.getCollection(c).countDocuments()}`));

print("\nExample document: tenancy ten_2 (cycles embedded, tenant snapshotted)");
printjson(db.tenancies.findOne({ _id: "ten_2" }, { cycles: { $slice: 2 } }));
