// =====================================================================
// MIT 8103 Portfolio 4: NoSQL (MongoDB document model)
// Step 1: collections, schema validation and indexes.
//   mongosh "mongodb://localhost:27017/?directConnection=true" --quiet 01_collections.js
// Tested on MongoDB 8.0 (single-node replica set, needed for transactions).
// =====================================================================
db = db.getSiblingDB("lodgely");
db.dropDatabase();

// Helpers reused in validators
const money = { bsonType: "decimal", minimum: NumberDecimal("0") };
const positiveMoney = { bsonType: "decimal", minimum: NumberDecimal("0.01") };

// ---------------------------------------------------------------------
// users: referenced from many places, so kept as its own collection
// ---------------------------------------------------------------------
db.createCollection("users", {
  validator: { $jsonSchema: {
    bsonType: "object",
    required: ["_id", "email", "fullName", "createdAt"],
    properties: {
      email:     { bsonType: "string", pattern: "^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$" },
      fullName:  { bsonType: "string", minLength: 2, maxLength: 150 },
      phone:     { bsonType: "string" },
      createdAt: { bsonType: "date" },
      deletedAt: { bsonType: ["date", "null"] }
    }
  } }
});
// Case-insensitive unique email (collation strength 2 ignores case).
db.users.createIndex({ email: 1 }, { unique: true, name: "ux_email_ci",
                                     collation: { locale: "en", strength: 2 } });
// Word-based full-text search over names and emails.
db.users.createIndex({ fullName: "text", email: "text" }, { name: "tx_user_search" });

// ---------------------------------------------------------------------
// organisations: members EMBEDDED (bounded, always read with the org)
// ---------------------------------------------------------------------
db.createCollection("organisations", {
  validator: { $jsonSchema: {
    bsonType: "object",
    required: ["_id", "name", "members"],
    properties: {
      name:    { bsonType: "string" },
      handle:  { bsonType: "string", pattern: "^[a-z0-9-]{3,60}$" },
      members: { bsonType: "array", items: {
        bsonType: "object", required: ["userId", "role"],
        properties: { userId: { bsonType: "string" },
                      role:   { enum: ["owner", "admin", "member"] } } } }
    }
  } }
});
db.organisations.createIndex({ handle: 1 }, { unique: true, name: "ux_handle",
  partialFilterExpression: { handle: { $exists: true } } });
db.organisations.createIndex({ "members.userId": 1 }, { name: "ix_member_user" });

// ---------------------------------------------------------------------
// buildings: unit categories, units and staff EMBEDDED.
// $expr rules enforce WITHIN-document integrity that an index cannot:
//   - unit labels unique inside a building
//   - every unit's categoryId exists in this building's unitCategories
//   - at most one active facility manager
// ---------------------------------------------------------------------
db.createCollection("buildings", {
  validator: { $and: [
    { $jsonSchema: {
      bsonType: "object",
      required: ["_id", "organisationId", "name", "address", "unitCategories", "units", "staff"],
      properties: {
        organisationId: { bsonType: "string" },
        name: { bsonType: "string" },
        address: { bsonType: "object", required: ["line", "city", "state"] },
        unitCategories: { bsonType: "array", items: {
          bsonType: "object", required: ["_id", "name", "bedrooms"],
          properties: { bedrooms: { bsonType: "int", minimum: 0, maximum: 10 } } } },
        units: { bsonType: "array", items: {
          bsonType: "object", required: ["_id", "label", "categoryId"] } },
        staff: { bsonType: "array", items: {
          bsonType: "object", required: ["userId", "role", "status"],
          properties: { role:   { enum: ["facility_manager", "finance", "viewer"] },
                        status: { enum: ["active", "revoked"] } } } }
      }
    } },
    { $expr: { $eq: [ { $size: "$units.label" },
                      { $size: { $setUnion: ["$units.label"] } } ] } },
    { $expr: { $setIsSubset: ["$units.categoryId", "$unitCategories._id"] } },
    { $expr: { $lte: [ { $size: { $filter: { input: "$staff", cond: { $and: [
        { $eq: ["$$this.role", "facility_manager"] },
        { $eq: ["$$this.status", "active"] } ] } } } }, 1 ] } }
  ] }
});
db.buildings.createIndex({ organisationId: 1, name: 1 }, { unique: true, name: "ux_org_building_name" });
db.buildings.createIndex({ "units._id": 1 }, { name: "ix_unit_id" });

// ---------------------------------------------------------------------
// tenancies: billing cycles EMBEDDED (bounded: one lease = at most 12
// monthly cycles), tenant and unit details SNAPSHOTTED for display.
// ---------------------------------------------------------------------
db.createCollection("tenancies", {
  validator: { $and: [
    { $jsonSchema: {
      bsonType: "object",
      required: ["_id", "buildingId", "unitId", "tenant", "leaseStart", "leaseEnd",
                 "rent", "status", "cycles"],
      properties: {
        buildingId: { bsonType: "string" },
        unitId:     { bsonType: "string" },
        tenant: { bsonType: "object", required: ["userId", "fullName"] },
        leaseStart: { bsonType: "date" },
        leaseEnd:   { bsonType: "date" },
        rent: { bsonType: "object", required: ["amount", "cycle"], properties: {
          amount: positiveMoney, cycle: { enum: ["monthly", "quarterly", "annual"] } } },
        status: { enum: ["pending", "active", "ended", "cancelled"] },
        cycles: { bsonType: "array", maxItems: 24, items: {
          bsonType: "object",
          required: ["_id", "chargeType", "periodStart", "periodEnd", "amountDue", "amountPaid"],
          properties: { chargeType: { enum: ["rent", "service_charge"] },
                        amountDue: positiveMoney, amountPaid: money } } }
      }
    } },
    { $expr: { $lt: ["$leaseStart", "$leaseEnd"] } },
    // No cycle may be paid beyond what is due.
    { $expr: { $allElementsTrue: [ { $map: { input: "$cycles",
        in: { $lte: ["$$this.amountPaid", "$$this.amountDue"] } } } ] } }
  ] }
});
// At most one live tenancy per unit (same idea as the original system).
db.tenancies.createIndex({ unitId: 1 }, { unique: true, name: "ux_one_live_tenancy_per_unit",
  partialFilterExpression: { status: { $in: ["pending", "active"] } } });
db.tenancies.createIndex({ buildingId: 1, status: 1 }, { name: "ix_building_status" });
db.tenancies.createIndex({ "tenant.userId": 1 }, { name: "ix_tenant" });

// ---------------------------------------------------------------------
// payments: own collection (unbounded, written by the gateway).
// buildingId is copied in so wallet totals need no join.
// ---------------------------------------------------------------------
db.createCollection("payments", {
  validator: { $and: [
    { $jsonSchema: {
      bsonType: "object",
      required: ["_id", "reference", "tenancyId", "buildingId", "amount", "channel",
                 "status", "initiatedAt", "allocations"],
      properties: {
        reference: { bsonType: "string" },
        amount:    positiveMoney,
        channel:   { enum: ["card", "bank_transfer", "ussd"] },
        status:    { enum: ["pending", "succeeded", "failed"] },
        allocations: { bsonType: "array", items: {
          bsonType: "object", required: ["cycleId", "amount"],
          properties: { amount: positiveMoney } } }
      }
    } },
    // confirmedAt present if and only if succeeded
    { $expr: { $eq: [ { $eq: ["$status", "succeeded"] },
                      { $ne: [ { $type: "$confirmedAt" }, "missing" ] } ] } }
  ] }
});
db.payments.createIndex({ reference: 1 }, { unique: true, name: "ux_reference" });
db.payments.createIndex({ tenancyId: 1 }, { name: "ix_tenancy" });
db.payments.createIndex({ buildingId: 1, status: 1 }, { name: "ix_building_status" });
db.payments.createIndex({ initiatedAt: 1 }, { name: "ix_pending_by_age",
  partialFilterExpression: { status: "pending" } });

// ---------------------------------------------------------------------
// payouts
// ---------------------------------------------------------------------
db.createCollection("payouts", {
  validator: { $and: [
    { $jsonSchema: {
      bsonType: "object",
      required: ["_id", "reference", "buildingId", "requestedBy", "amount", "platformFee", "status"],
      properties: { amount: positiveMoney, platformFee: money,
                    status: { enum: ["pending", "paid", "cancelled", "failed"] } }
    } },
    { $expr: { $lte: ["$platformFee", "$amount"] } }
  ] }
});
db.payouts.createIndex({ reference: 1 }, { unique: true, name: "ux_reference" });
db.payouts.createIndex({ buildingId: 1, status: 1 }, { name: "ix_building_status" });

// ---------------------------------------------------------------------
// budgets: lines EMBEDDED with a running `remaining` amount, so a budget
// reservation is ONE atomic conditional update (see 04_operations.js).
// ---------------------------------------------------------------------
db.createCollection("budgets", {
  validator: { $and: [
    { $jsonSchema: {
      bsonType: "object",
      required: ["_id", "buildingId", "fiscalYear", "lines"],
      properties: {
        fiscalYear: { bsonType: "int", minimum: 2000, maximum: 2100 },
        lines: { bsonType: "array", items: {
          bsonType: "object", required: ["_id", "itemName", "expectedAmount", "committedAmount", "remaining"],
          properties: { expectedAmount: positiveMoney, committedAmount: money,
                        remaining: money } } }   // minimum 0: overspend is impossible
      }
    } },
    // remaining must always equal expected - committed
    { $expr: { $allElementsTrue: [ { $map: { input: "$lines", in: { $eq: [
        "$$this.remaining", { $subtract: ["$$this.expectedAmount", "$$this.committedAmount"] } ] } } } ] } }
  ] }
});
db.budgets.createIndex({ buildingId: 1, fiscalYear: 1 }, { unique: true, name: "ux_building_year" });

// ---------------------------------------------------------------------
// requisitions: items EMBEDDED (always created and read together)
// ---------------------------------------------------------------------
db.createCollection("requisitions", {
  validator: { $jsonSchema: {
    bsonType: "object",
    required: ["_id", "buildingId", "raisedBy", "purpose", "status", "items", "total", "version"],
    properties: {
      status: { enum: ["draft", "submitted", "approved", "rejected", "paid"] },
      items: { bsonType: "array", minItems: 1, items: {
        bsonType: "object", required: ["lineNo", "description", "quantity", "unitPrice"] } },
      total: money,
      version: { bsonType: "int", minimum: 1 }
    }
  } }
});
db.requisitions.createIndex({ buildingId: 1, status: 1 }, { name: "ix_building_status" });

// ---------------------------------------------------------------------
// accessPasses: visitor passes deleted automatically when they expire
// (TTL index: MongoDB's background monitor removes expired documents).
// ---------------------------------------------------------------------
db.createCollection("accessPasses");
db.accessPasses.createIndex({ code: 1 }, { unique: true, name: "ux_code" });
db.accessPasses.createIndex({ expiresOn: 1 }, { expireAfterSeconds: 0, name: "ttl_expires_on" });

// ---------------------------------------------------------------------
print("Collections and indexes:");
db.getCollectionNames().sort().forEach(c => {
  const idx = db.getCollection(c).getIndexes().map(i => i.name).filter(n => n !== "_id_");
  print(`  ${c.padEnd(14)} ${idx.join(", ")}`);
});
