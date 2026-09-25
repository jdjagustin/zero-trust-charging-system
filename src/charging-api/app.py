"""charging-api: simplified online-charging function (synthetic data only).

Endpoints:
  GET  /healthz
  GET  /balance/<subscriber>
  POST /reserve  {"subscriber": "sub-0001", "amount": 50, "session": "s-123"}
  POST /commit   {"session": "s-123", "used": 30}
  POST /refund   {"session": "s-123"}
"""
import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

client = MongoClient(
    host=os.environ.get("DB_HOST", "balance-db"),
    port=int(os.environ.get("DB_PORT", "27017")),
    username=os.environ["DB_USER"],
    password=os.environ["DB_PASSWORD"],
    authSource=os.environ.get("DB_NAME", "billing"),
    serverSelectionTimeoutMS=3000,
)
db = client[os.environ.get("DB_NAME", "billing")]
balances = db.balances
ledger = db.ledger

# One "final" ledger entry per session (commit or refund). The unique sparse
# index makes a second finalization fail instead of crediting twice.
ledger.create_index("finalSession", unique=True, sparse=True)
ledger.create_index("session")


def now():
    return datetime.now(timezone.utc)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def log_message(self, fmt, *args):
        print("%s %s" % (self.address_string(), fmt % args), flush=True)

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, {"status": "ok"})
        if self.path.startswith("/balance/"):
            doc = balances.find_one({"_id": self.path.split("/", 2)[2]})
            if not doc:
                return self._send(404, {"error": "unknown subscriber"})
            return self._send(200, {"subscriber": doc["_id"], "balance": int(doc["balance"])})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            body = self._json()
            if self.path == "/reserve":
                return self.reserve(body)
            if self.path == "/commit":
                return self.finalize(body, commit=True)
            if self.path == "/refund":
                return self.finalize(body, commit=False)
            self._send(404, {"error": "not found"})
        except (ValueError, KeyError, TypeError):
            self._send(400, {"error": "bad request"})

    def reserve(self, body):
        sub, amount, session = body["subscriber"], int(body["amount"]), body["session"]
        if amount <= 0:
            return self._send(400, {"error": "amount must be positive"})
        if ledger.find_one({"session": session, "type": "reserve"}):
            return self._send(409, {"error": "session already reserved"})
        # Atomic debit: matches only if enough balance, so no double-spend.
        doc = balances.find_one_and_update(
            {"_id": sub, "balance": {"$gte": amount}},
            {"$inc": {"balance": -amount}, "$set": {"updatedAt": now()}},
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            return self._send(402, {"error": "insufficient balance or unknown subscriber"})
        ledger.insert_one({"subscriber": sub, "type": "reserve", "session": session,
                           "amount": amount, "ts": now()})
        self._send(200, {"session": session, "reserved": amount, "balance": int(doc["balance"])})

    def finalize(self, body, commit):
        session = body["session"]
        res = ledger.find_one({"session": session, "type": "reserve"})
        if not res:
            return self._send(404, {"error": "unknown session"})
        reserved = int(res["amount"])
        used = int(body["used"]) if commit else 0
        if used < 0 or used > reserved:
            return self._send(400, {"error": "used must be between 0 and reserved"})
        release = reserved - used
        try:
            # Record the finalization first: a duplicate fails here, before any credit.
            ledger.insert_one({"subscriber": res["subscriber"],
                               "type": "commit" if commit else "refund",
                               "session": session, "amount": used, "released": release,
                               "finalSession": session, "ts": now()})
        except DuplicateKeyError:
            return self._send(409, {"error": "session already finalized"})
        # Not atomic with the insert above (no multi-document transactions without a
        # replica set): a crash between the two leaves the released amount uncredited.
        doc = balances.find_one_and_update(
            {"_id": res["subscriber"]},
            {"$inc": {"balance": release}, "$set": {"updatedAt": now()}},
            return_document=ReturnDocument.AFTER,
        )
        self._send(200, {"session": session, "used": used, "released": release,
                         "balance": int(doc["balance"])})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print("charging-api listening on :%d" % port, flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
