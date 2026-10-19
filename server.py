"""
Automation Ticketing System  (zero dependencies: Python 3.9+ standard library only)

Features
  * REST API + web UI (static/index.html)
  * Auto-classification (category + priority) from ticket text
  * Rules engine (IF field matches THEN set priority / category / assignee / status)
  * Auto-assignment (skill match + least-loaded agent)
  * SLA deadlines per priority, auto-escalation of overdue tickets
  * Auto-reopen on customer reply, auto-close of long-resolved tickets
  * Email outbox (prints to console, or sends via SMTP if SMTP_HOST is set)
  * Inbound webhook (/api/inbound-email) to create tickets from emails/forms

Run:  python3 server.py        ->  http://localhost:8000
"""
import json, os, re, smtplib, sqlite3, threading, time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


DB_PATH = os.environ.get("TICKET_DB", "tickets.db")
PORT = int(os.environ.get("PORT", "8000"))
AUTOMATION_INTERVAL = int(os.environ.get("AUTOMATION_INTERVAL", "30"))  
AUTO_CLOSE_HOURS = int(os.environ.get("AUTO_CLOSE_HOURS", "72"))
MAX_ESCALATIONS = 3
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

PRIORITIES = ["low", "medium", "high", "urgent"]
STATUSES = ["open", "in_progress", "waiting", "resolved", "closed"]
ACTIVE = ("open", "in_progress", "waiting")
SLA_HOURS = {"urgent": 1, "high": 4, "medium": 8, "low": 24}

CATEGORY_KEYWORDS = {
    "billing": ["invoice", "refund", "charge", "payment", "billing", "subscription", "receipt"],
    "technical": ["error", "bug", "crash", "broken", "not working", "timeout", "500", "login", "password", "cannot access"],
    "account": ["account", "profile", "email change", "delete my", "username", "2fa", "verification"],
    "sales": ["pricing", "quote", "demo", "upgrade", "plan", "enterprise", "trial"],
}
URGENT_WORDS = ["urgent", "asap", "outage", "down", "critical", "emergency", "data loss", "security breach"]

RULE_FIELDS = ["subject", "body", "requester_email", "category"]
RULE_OPS = ["contains", "equals", "startswith", "endswith"]
RULE_ACTIONS = ["set_priority", "set_category", "assign_agent", "set_status"]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def parse(ts):
    return datetime.fromisoformat(ts)

def due_from(base_iso, priority):
    return (parse(base_iso) + timedelta(hours=SLA_HOURS[priority])).isoformat(timespec="seconds")

def db():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
  skills TEXT DEFAULT '', active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS tickets(
  id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL, body TEXT DEFAULT '',
  requester_email TEXT NOT NULL, category TEXT DEFAULT 'general', priority TEXT DEFAULT 'medium',
  status TEXT DEFAULT 'open', assignee_id INTEGER REFERENCES agents(id),
  created_at TEXT, updated_at TEXT, due_at TEXT, resolved_at TEXT, escalations INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS comments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER REFERENCES tickets(id) ON DELETE CASCADE,
  author TEXT, role TEXT DEFAULT 'agent', body TEXT, internal INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER, kind TEXT, detail TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS rules(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, enabled INTEGER DEFAULT 1,
  field TEXT, op TEXT, value TEXT, action TEXT, action_value TEXT);
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT, to_addr TEXT, subject TEXT, body TEXT,
  created_at TEXT, sent INTEGER DEFAULT 0);
"""

def init_db():
    with db() as c:
        c.executescript(SCHEMA)
        if not c.execute("SELECT 1 FROM agents").fetchone():
            c.executemany("INSERT INTO agents(name,email,skills) VALUES(?,?,?)", [
                ("Alice Reyes", "alice@example.com", "technical,account"),
                ("Ben Santos", "ben@example.com", "billing,sales"),
                ("Carla Lim", "carla@example.com", "technical,billing,general"),
            ])
        if not c.execute("SELECT 1 FROM rules").fetchone():
            c.executemany(
                "INSERT INTO rules(name,field,op,value,action,action_value) VALUES(?,?,?,?,?,?)", [
                    ("VIP customers are high priority", "requester_email", "endswith", "@bigclient.com", "set_priority", "high"),
                    ("Security reports -> urgent", "subject", "contains", "security", "set_priority", "urgent"),
                    ("Refund requests -> billing", "body", "contains", "refund", "set_category", "billing"),
                ])

def log_event(c, ticket_id, kind, detail):
    c.execute("INSERT INTO events(ticket_id,kind,detail,created_at) VALUES(?,?,?,?)",
              (ticket_id, kind, detail, now()))

def notify(c, to_addr, subject, body):
    c.execute("INSERT INTO outbox(to_addr,subject,body,created_at) VALUES(?,?,?,?)",
              (to_addr, subject, body, now()))


def classify(text):
    text = text.lower()
    best, score = "general", 0
    for cat, words in CATEGORY_KEYWORDS.items():
        s = sum(1 for w in words if w in text)
        if s > score:
            best, score = cat, s
    return best

def detect_priority(text):
    text = text.lower()
    return "urgent" if any(w in text for w in URGENT_WORDS) else "medium"

def resolve_agent(c, ref):
    ref = str(ref).strip()
    row = c.execute("SELECT * FROM agents WHERE id=? OR lower(email)=lower(?)", (ref if ref.isdigit() else -1, ref)).fetchone()
    return row

def pick_agent(c, category):
    """Skill match first, then least open tickets."""
    agents = c.execute("SELECT * FROM agents WHERE active=1").fetchall()
    if not agents:
        return None
    skilled = [a for a in agents if category in [s.strip() for s in a["skills"].split(",")]]
    pool = skilled or agents
    def load(a):
        return c.execute("SELECT COUNT(*) FROM tickets WHERE assignee_id=? AND status IN ('open','in_progress','waiting')",
                         (a["id"],)).fetchone()[0]
    return min(pool, key=load)

def rule_matches(rule, t):
    val = str(t[rule["field"]] or "").lower()
    needle = (rule["value"] or "").lower()
    return {"contains": needle in val, "equals": val == needle,
            "startswith": val.startswith(needle), "endswith": val.endswith(needle)}.get(rule["op"], False)

def apply_rules(c, t):
    """t is a mutable dict; returns it modified."""
    for r in c.execute("SELECT * FROM rules WHERE enabled=1 ORDER BY id").fetchall():
        if not rule_matches(r, t):
            continue
        a, v = r["action"], r["action_value"]
        if a == "set_priority" and v in PRIORITIES:

            if PRIORITIES.index(v) > PRIORITIES.index(t["priority"]):
                t["priority"] = v
        elif a == "set_category":
            t["category"] = v
        elif a == "set_status" and v in STATUSES:
            t["status"] = v
        elif a == "assign_agent":
            ag = resolve_agent(c, v)
            if ag:
                t["assignee_id"] = ag["id"]
        log_event(c, t["id"], "rule", f"Rule '{r['name']}' applied: {a} = {v}")
    return t

def process_new_ticket(c, ticket_id):
    t = dict(c.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone())
    text = f"{t['subject']} {t['body']}"
    if t["category"] in (None, "", "general"):
        t["category"] = classify(text)
    detected = detect_priority(text)
    if PRIORITIES.index(detected) > PRIORITIES.index(t["priority"]):
        t["priority"] = detected
    t = apply_rules(c, t)
    if not t["assignee_id"]:
        ag = pick_agent(c, t["category"])
        if ag:
            t["assignee_id"] = ag["id"]
            log_event(c, ticket_id, "assign", f"Auto-assigned to {ag['name']} ({t['category']})")
            notify(c, ag["email"], f"[#{ticket_id}] New {t['priority']} ticket: {t['subject']}", t["body"])
    t["due_at"] = due_from(t["created_at"], t["priority"])
    c.execute("""UPDATE tickets SET category=?,priority=?,status=?,assignee_id=?,due_at=?,updated_at=? WHERE id=?""",
              (t["category"], t["priority"], t["status"], t["assignee_id"], t["due_at"], now(), ticket_id))
    log_event(c, ticket_id, "triage", f"category={t['category']} priority={t['priority']} due={t['due_at']}")
    notify(c, t["requester_email"], f"[#{ticket_id}] We received your request",
           f"Hi,\n\nYour ticket \"{t['subject']}\" was received and routed to our {t['category']} team. "
           f"Priority: {t['priority']}. We'll respond within {SLA_HOURS[t['priority']]}h.")

def run_automation():
    """Periodic job: assign orphans, escalate overdue, auto-close stale resolved tickets."""
    stats = {"assigned": 0, "escalated": 0, "closed": 0}
    with db() as c:
        ts = now()

        for t in c.execute("SELECT * FROM tickets WHERE assignee_id IS NULL AND status IN ('open','in_progress','waiting')").fetchall():
            ag = pick_agent(c, t["category"])
            if ag:
                c.execute("UPDATE tickets SET assignee_id=?, updated_at=? WHERE id=?", (ag["id"], ts, t["id"]))
                log_event(c, t["id"], "assign", f"Auto-assigned to {ag['name']}")
                notify(c, ag["email"], f"[#{t['id']}] Ticket assigned: {t['subject']}", t["body"] or "")
                stats["assigned"] += 1

        for t in c.execute("SELECT * FROM tickets WHERE status IN ('open','in_progress','waiting') AND due_at < ? AND escalations < ?",
                           (ts, MAX_ESCALATIONS)).fetchall():
            new_p = PRIORITIES[min(PRIORITIES.index(t["priority"]) + 1, len(PRIORITIES) - 1)]
            c.execute("UPDATE tickets SET priority=?, escalations=escalations+1, due_at=?, updated_at=? WHERE id=?",
                      (new_p, due_from(ts, new_p), ts, t["id"]))
            log_event(c, t["id"], "escalation", f"SLA breached: {t['priority']} -> {new_p} (escalation #{t['escalations'] + 1})")
            ag = c.execute("SELECT email FROM agents WHERE id=?", (t["assignee_id"],)).fetchone()
            notify(c, ag["email"] if ag else "manager@example.com",
                   f"[#{t['id']}] ESCALATED to {new_p}: {t['subject']}", "This ticket breached its SLA.")
            stats["escalated"] += 1

        cutoff = (parse(ts) - timedelta(hours=AUTO_CLOSE_HOURS)).isoformat(timespec="seconds")
        for t in c.execute("SELECT * FROM tickets WHERE status='resolved' AND resolved_at < ?", (cutoff,)).fetchall():
            c.execute("UPDATE tickets SET status='closed', updated_at=? WHERE id=?", (ts, t["id"]))
            log_event(c, t["id"], "auto_close", f"Closed automatically after {AUTO_CLOSE_HOURS}h resolved")
            stats["closed"] += 1
    flush_outbox()
    return stats

def flush_outbox():
    host = os.environ.get("SMTP_HOST")
    with db() as c:
        for m in c.execute("SELECT * FROM outbox WHERE sent=0 ORDER BY id LIMIT 50").fetchall():
            try:
                if host:
                    msg = EmailMessage()
                    msg["From"] = os.environ.get("SMTP_FROM", "support@example.com")
                    msg["To"], msg["Subject"] = m["to_addr"], m["subject"]
                    msg.set_content(m["body"] or "")
                    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587"))) as s:
                        s.starttls()
                        if os.environ.get("SMTP_USER"):
                            s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
                        s.send_message(msg)
                else:
                    print(f"[mail] to={m['to_addr']} subject={m['subject']}")
                c.execute("UPDATE outbox SET sent=1 WHERE id=?", (m["id"],))
            except Exception as e:  
                print("[mail] failed:", e)

def worker():
    while True:
        time.sleep(AUTOMATION_INTERVAL)
        try:
            run_automation()
        except Exception as e:
            print("[automation] error:", e)


def ticket_rows(c, where="", args=()):
    q = """SELECT t.*, a.name AS assignee_name FROM tickets t
           LEFT JOIN agents a ON a.id=t.assignee_id """ + where + " ORDER BY t.id DESC"
    rows = [dict(r) for r in c.execute(q, args).fetchall()]
    ts = now()
    for r in rows:
        r["overdue"] = bool(r["status"] in ACTIVE and r["due_at"] and r["due_at"] < ts)
    return rows

def stats(c):
    ts = now()
    by = lambda col: {r[0]: r[1] for r in c.execute(f"SELECT {col}, COUNT(*) FROM tickets GROUP BY {col}")}
    res = c.execute("SELECT AVG((julianday(resolved_at)-julianday(created_at))*24) FROM tickets WHERE resolved_at IS NOT NULL").fetchone()[0]
    return {
        "total": c.execute("SELECT COUNT(*) FROM tickets").fetchone()[0],
        "by_status": by("status"), "by_priority": by("priority"), "by_category": by("category"),
        "overdue": c.execute("SELECT COUNT(*) FROM tickets WHERE status IN ('open','in_progress','waiting') AND due_at<?", (ts,)).fetchone()[0],
        "unassigned": c.execute("SELECT COUNT(*) FROM tickets WHERE assignee_id IS NULL AND status IN ('open','in_progress','waiting')").fetchone()[0],
        "avg_resolution_hours": round(res, 2) if res is not None else None,
        "agent_load": [dict(r) for r in c.execute(
            """SELECT a.id,a.name,COUNT(t.id) AS open_tickets FROM agents a LEFT JOIN tickets t
               ON t.assignee_id=a.id AND t.status IN ('open','in_progress','waiting') GROUP BY a.id""")],
    }


class ApiError(Exception):
    def __init__(self, code, msg):
        self.code, self.msg = code, msg

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def need(data, *keys):
    for k in keys:
        if not str(data.get(k, "")).strip():
            raise ApiError(400, f"'{k}' is required")

def create_ticket(data):
    need(data, "subject", "requester_email")
    if not EMAIL_RE.match(data["requester_email"]):
        raise ApiError(400, "invalid requester_email")
    prio = data.get("priority", "medium")
    if prio not in PRIORITIES:
        raise ApiError(400, "invalid priority")
    ts = now()
    with db() as c:
        cur = c.execute("""INSERT INTO tickets(subject,body,requester_email,category,priority,status,created_at,updated_at)
                           VALUES(?,?,?,?,?,'open',?,?)""",
                        (data["subject"].strip()[:200], data.get("body", "")[:10000], data["requester_email"].strip(),
                         data.get("category") or "general", prio, ts, ts))
        tid = cur.lastrowid
        log_event(c, tid, "created", f"Ticket created by {data['requester_email']}")
        process_new_ticket(c, tid)
    flush_outbox()
    with db() as c:
        return ticket_rows(c, "WHERE t.id=?", (tid,))[0]

def update_ticket(tid, data):
    allowed = {"status", "priority", "category", "assignee_id"}
    with db() as c:
        t = c.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        if not t:
            raise ApiError(404, "ticket not found")
        sets, args, ts = [], [], now()
        for k, v in data.items():
            if k not in allowed or v == t[k]:
                continue
            if k == "status" and v not in STATUSES: raise ApiError(400, "invalid status")
            if k == "priority" and v not in PRIORITIES: raise ApiError(400, "invalid priority")
            if k == "assignee_id" and v is not None and not c.execute("SELECT 1 FROM agents WHERE id=?", (v,)).fetchone():
                raise ApiError(400, "unknown agent")
            sets.append(f"{k}=?"); args.append(v)
            log_event(c, tid, "update", f"{k}: {t[k]} -> {v}")
            if k == "priority":
                sets.append("due_at=?"); args.append(due_from(t["created_at"], v))
            if k == "status":
                sets.append("resolved_at=?"); args.append(ts if v == "resolved" else None)
                if v == "resolved":
                    notify(c, t["requester_email"], f"[#{tid}] Your ticket was resolved",
                           f"Ticket \"{t['subject']}\" is marked resolved. Reply to reopen it.")
        if sets:
            c.execute(f"UPDATE tickets SET {', '.join(sets)}, updated_at=? WHERE id=?", (*args, ts, tid))
    flush_outbox()
    with db() as c:
        return ticket_rows(c, "WHERE t.id=?", (tid,))[0]

def add_comment(tid, data):
    need(data, "author", "body")
    role = data.get("role", "agent")
    internal = 1 if data.get("internal") and role == "agent" else 0
    with db() as c:
        t = c.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        if not t:
            raise ApiError(404, "ticket not found")
        ts = now()
        c.execute("INSERT INTO comments(ticket_id,author,role,body,internal,created_at) VALUES(?,?,?,?,?,?)",
                  (tid, data["author"], role, data["body"][:10000], internal, ts))
        
        if role == "requester" and t["status"] in ("waiting", "resolved", "closed"):
            c.execute("UPDATE tickets SET status='open', resolved_at=NULL, due_at=? WHERE id=?",
                      (due_from(ts, t["priority"]), tid))
            log_event(c, tid, "reopen", "Customer replied -> ticket reopened")
        elif role == "agent" and not internal:
            if t["status"] == "open":
                c.execute("UPDATE tickets SET status='in_progress' WHERE id=?", (tid,))
            notify(c, t["requester_email"], f"[#{tid}] Re: {t['subject']}", data["body"])
            log_event(c, tid, "reply", f"Public reply by {data['author']}")
        c.execute("UPDATE tickets SET updated_at=? WHERE id=?", (ts, tid))
    flush_outbox()
    return {"ok": True}

def validate_rule(d):
    need(d, "name", "field", "op", "value", "action", "action_value")
    if d["field"] not in RULE_FIELDS: raise ApiError(400, f"field must be one of {RULE_FIELDS}")
    if d["op"] not in RULE_OPS: raise ApiError(400, f"op must be one of {RULE_OPS}")
    if d["action"] not in RULE_ACTIONS: raise ApiError(400, f"action must be one of {RULE_ACTIONS}")

def api(method, path, query, data):
    parts = [p for p in path.split("/") if p][1:]
    with db() as c:
        if parts == ["meta"]:
            return {"priorities": PRIORITIES, "statuses": STATUSES, "rule_fields": RULE_FIELDS,
                    "rule_ops": RULE_OPS, "rule_actions": RULE_ACTIONS, "sla_hours": SLA_HOURS}
        if parts == ["stats"]:
            return stats(c)
        if parts == ["tickets"]:
            if method == "GET":
                where, args = [], []
                for k in ("status", "priority", "category", "assignee_id"):
                    if query.get(k):
                        where.append(f"t.{k}=?"); args.append(query[k][0])
                if query.get("q"):
                    where.append("(t.subject LIKE ? OR t.body LIKE ? OR t.requester_email LIKE ?)")
                    args += [f"%{query['q'][0]}%"] * 3
                if query.get("overdue"):
                    where.append("t.status IN ('open','in_progress','waiting') AND t.due_at < ?"); args.append(now())
                return ticket_rows(c, ("WHERE " + " AND ".join(where)) if where else "", args)
            if method == "POST":
                return create_ticket(data)
        if parts == ["inbound-email"] and method == "POST":  
            return create_ticket({"requester_email": data.get("from", ""), "subject": data.get("subject", ""),
                                  "body": data.get("text", "")})
        if len(parts) >= 2 and parts[0] == "tickets" and parts[1].isdigit():
            tid = int(parts[1])
            if len(parts) == 2:
                if method == "GET":
                    rows = ticket_rows(c, "WHERE t.id=?", (tid,))
                    if not rows: raise ApiError(404, "ticket not found")
                    t = rows[0]
                    t["comments"] = [dict(r) for r in c.execute("SELECT * FROM comments WHERE ticket_id=? ORDER BY id", (tid,))]
                    t["events"] = [dict(r) for r in c.execute("SELECT * FROM events WHERE ticket_id=? ORDER BY id", (tid,))]
                    return t
                if method == "PATCH":
                    return update_ticket(tid, data)
                if method == "DELETE":
                    c.execute("DELETE FROM tickets WHERE id=?", (tid,)); return {"ok": True}
            if parts[2:] == ["comments"] and method == "POST":
                return add_comment(tid, data)
        if parts == ["agents"]:
            if method == "GET": return [dict(r) for r in c.execute("SELECT * FROM agents ORDER BY id")]
            if method == "POST":
                need(data, "name", "email")
                try:
                    cur = c.execute("INSERT INTO agents(name,email,skills) VALUES(?,?,?)",
                                    (data["name"], data["email"], data.get("skills", "")))
                except sqlite3.IntegrityError:
                    raise ApiError(409, "agent email already exists")
                return {"id": cur.lastrowid}
        if len(parts) == 2 and parts[0] == "agents" and parts[1].isdigit() and method == "PATCH":
            for k in ("name", "skills", "active"):
                if k in data: c.execute(f"UPDATE agents SET {k}=? WHERE id=?", (data[k], int(parts[1])))
            return {"ok": True}
        if parts == ["rules"]:
            if method == "GET": return [dict(r) for r in c.execute("SELECT * FROM rules ORDER BY id")]
            if method == "POST":
                validate_rule(data)
                cur = c.execute("INSERT INTO rules(name,field,op,value,action,action_value) VALUES(?,?,?,?,?,?)",
                                tuple(data[k] for k in ("name", "field", "op", "value", "action", "action_value")))
                return {"id": cur.lastrowid}
        if len(parts) == 2 and parts[0] == "rules" and parts[1].isdigit():
            rid = int(parts[1])
            if method == "PATCH" and "enabled" in data:
                c.execute("UPDATE rules SET enabled=? WHERE id=?", (1 if data["enabled"] else 0, rid)); return {"ok": True}
            if method == "DELETE":
                c.execute("DELETE FROM rules WHERE id=?", (rid,)); return {"ok": True}
        if parts == ["events"]:
            return [dict(r) for r in c.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100")]
        if parts == ["outbox"]:
            return [dict(r) for r in c.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT 50")]
    if parts == ["automation", "run"] and method == "POST":
        return run_automation()
    raise ApiError(404, "not found")

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_any(self, method):
        u = urlparse(self.path)
        if u.path.startswith("/api/"):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(n) or b"{}") if n else {}
                self.send_json(api(method, u.path, parse_qs(u.query), data))
            except ApiError as e:
                self.send_json({"error": e.msg}, e.code)
            except json.JSONDecodeError:
                self.send_json({"error": "invalid JSON"}, 400)
            except Exception as e:
                print("[error]", repr(e))
                self.send_json({"error": "internal error"}, 500)
        elif method == "GET":
            f = os.path.join(STATIC_DIR, "index.html")
            with open(f, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_json({"error": "not found"}, 404)

    do_GET = lambda self: self.handle_any("GET")
    do_POST = lambda self: self.handle_any("POST")
    do_PATCH = lambda self: self.handle_any("PATCH")
    do_DELETE = lambda self: self.handle_any("DELETE")

if __name__ == "__main__":
    init_db()
    threading.Thread(target=worker, daemon=True).start()
    print(f"Ticketing system running on http://localhost:{PORT}  (db: {DB_PATH})")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
