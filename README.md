# Automation Ticketing System

Zero-dependency helpdesk (Python 3.9+, SQLite, vanilla JS).

## Run
    python3 server.py            # http://localhost:8000

Env vars: `PORT`, `TICKET_DB`, `AUTOMATION_INTERVAL` (s, default 30), `AUTO_CLOSE_HOURS` (default 72),
`SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS/SMTP_FROM` (omit SMTP_HOST to just print emails to the console).

## What is automated
| Trigger | Automation |
|---|---|
| Ticket created | keyword classification -> category; urgent-word detection -> priority; your rules; skill-match + least-loaded assignment; SLA due date; acknowledgement email to requester, alert to agent |
| Every 30s | assign orphan tickets; escalate SLA-breached tickets (priority +1, new SLA, notify); auto-close tickets resolved > 72h |
| Agent public reply | open -> in_progress, email to requester |
| Customer reply | waiting/resolved/closed -> reopened, SLA restarted |
| Status = resolved | notification email to requester |

SLA: urgent 1h, high 4h, medium 8h, low 24h. Max 3 escalations per ticket.

## API (JSON)
    GET/POST   /api/tickets            (filters: status, priority, category, assignee_id, q, overdue=1)
    GET/PATCH/DELETE /api/tickets/{id}
    POST       /api/tickets/{id}/comments   {author, role: agent|requester, body, internal}
    POST       /api/inbound-email      {from, subject, text}   <- point your mail/form webhook here
    GET/POST   /api/agents  ·  PATCH /api/agents/{id}
    GET/POST   /api/rules   ·  PATCH/DELETE /api/rules/{id}
    GET        /api/stats  /api/events  /api/outbox
    POST       /api/automation/run

Example:
    curl -XPOST localhost:8000/api/tickets -H 'Content-Type: application/json' \
      -d '{"requester_email":"a@x.com","subject":"Refund please","body":"charged twice"}'

## Production notes
No authentication is built in - put it behind a reverse proxy with auth (or add login) before exposing it.
Swap SQLite for Postgres if you need multiple server processes.
