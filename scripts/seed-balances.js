// Synthetic data only. No real subscriber, payment or personal data.
var conn = db.getSiblingDB("billing");
conn.balances.deleteMany({});
conn.ledger.deleteMany({});
var now = new Date();
var subs = [];
var ledger = [];
for (var i = 1; i <= 10; i++) {
  var id = "sub-" + ("0000" + i).slice(-4);
  var initial = i * 500;
  subs.push({ _id: id, label: "synthetic", balance: NumberLong(initial), updatedAt: now });
  ledger.push({ subscriber: id, type: "initial_credit", amount: NumberLong(initial), ts: now });
}
conn.balances.insertMany(subs);
conn.ledger.insertMany(ledger);
conn.ledger.createIndex({ subscriber: 1, ts: -1 });
print("balances: " + conn.balances.countDocuments({}));
print("ledger: " + conn.ledger.countDocuments({}));
