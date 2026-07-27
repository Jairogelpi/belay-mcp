"""`belay dashboard`: a self-contained HTML snapshot of one ledger (spec §9).

Reads every event + approval item from a `belay.db` and embeds them as JSON
in a static HTML file -- sessions, their steps, pending/resolved approvals,
and (for rewound sessions) the compensation outcome per step. No server, no
live DB access from the page: a snapshot, refreshed by re-running the
command. Approve/reject actions are shown as the exact `belay approvals`
command to run, not executed from the page -- the spec's no-self-approval
rule (§7, §12) means approval is a CLI-only, human-typed action by design;
a button here would just be re-implementing that surface with weaker
guarantees.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from belay.approvals.queue import ApprovalQueue
from belay.ledger.model import Event
from belay.ledger.store import LedgerStore


def _session_summary(events: list[Event]) -> dict[str, Any]:
    by_session: dict[str, list[Event]] = {}
    for event in events:
        by_session.setdefault(event.session_id, []).append(event)
    sessions = {}
    for session_id, session_events in by_session.items():
        session_events.sort(key=lambda e: (e.step_seq is None, e.step_seq, e.at))
        sessions[session_id] = {
            "initiated_by": session_events[0].initiated_by if session_events else None,
            "event_count": len(session_events),
            "events": [
                {
                    "type": e.type,
                    "step_seq": e.step_seq,
                    "at": e.at,
                    "payload": e.payload,
                }
                for e in session_events
            ],
        }
    return sessions


def build_dashboard_data(db_path: str) -> dict[str, Any]:
    ledger = LedgerStore(f"sqlite:///{Path(db_path).resolve().as_posix()}")
    events = ledger.read_all()
    approvals_queue = ApprovalQueue(f"sqlite:///{Path(db_path).resolve().as_posix()}")
    approvals = [
        {**asdict(item), "requested_at": item.requested_at.isoformat(),
         "expires_at": item.expires_at.isoformat(), "state": item.state}
        for item in approvals_queue.list()
    ]
    return {
        "db_path": str(Path(db_path).resolve()),
        "sessions": _session_summary(events),
        "approvals": approvals,
    }


_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Belay dashboard</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, Segoe UI, sans-serif; margin: 0; padding: 2rem;
         background: Canvas; color: CanvasText; }
  h1 { font-size: 1.1rem; opacity: .7; margin-bottom: .25rem; }
  .db-path { font-size: .8rem; opacity: .5; margin-bottom: 1.5rem; font-family: monospace; }
  .session { border: 1px solid color-mix(in srgb, CanvasText 15%, transparent);
             border-radius: 8px; margin-bottom: 1rem; overflow: hidden; }
  .session-head { padding: .6rem 1rem; background: color-mix(in srgb, CanvasText 6%, transparent);
                  display: flex; justify-content: space-between; font-family: monospace; }
  .step { padding: .5rem 1rem; border-top: 1px solid color-mix(in srgb, CanvasText 10%, transparent);
          display: flex; gap: .75rem; align-items: baseline; font-size: .85rem; }
  .step-seq { opacity: .4; width: 2.5rem; flex-shrink: 0; }
  .step-type { font-family: monospace; font-weight: 600; }
  .tag { display: inline-block; padding: .1rem .5rem; border-radius: 999px; font-size: .7rem;
         font-weight: 600; margin-left: .5rem; }
  .tag.pause, .tag.pending { background: #7a5c00; color: #fff; }
  .tag.deny, .tag.rejected, .tag.step_failed { background: #7a1f1f; color: #fff; }
  .tag.allow, .tag.approved, .tag.step_committed { background: #1f5c2e; color: #fff; }
  .tag.compensated { background: #1f4c7a; color: #fff; }
  .approvals { margin-top: 2rem; }
  .approval { padding: .6rem 1rem; border: 1px solid color-mix(in srgb, CanvasText 15%, transparent);
              border-radius: 8px; margin-bottom: .5rem; font-size: .85rem; }
  .cmd { font-family: monospace; background: color-mix(in srgb, CanvasText 8%, transparent);
         padding: .3rem .5rem; border-radius: 4px; display: inline-block; margin-top: .3rem; }
  pre { white-space: pre-wrap; word-break: break-word; }
</style>
</head>
<body>
<h1>Belay dashboard (snapshot)</h1>
<div class="db-path">DATA_DB_PATH</div>
<div id="root"></div>
<script id="belay-data" type="application/json">DATA_JSON</script>
<script>
const data = JSON.parse(document.getElementById("belay-data").textContent);
const root = document.getElementById("root");

function tagClass(t) {
  return (t || "").toLowerCase();
}

for (const [sessionId, session] of Object.entries(data.sessions)) {
  const div = document.createElement("div");
  div.className = "session";
  const head = document.createElement("div");
  head.className = "session-head";
  head.innerHTML = `<span>${sessionId}</span><span>${session.initiated_by || "unknown"} · ${session.event_count} events</span>`;
  div.appendChild(head);
  for (const ev of session.events) {
    const step = document.createElement("div");
    step.className = "step";
    const verdict = ev.payload && (ev.payload.verdict || ev.payload.status || ev.payload.result);
    step.innerHTML = `<span class="step-seq">${ev.step_seq ?? ""}</span>` +
      `<span class="step-type">${ev.type}</span>` +
      (verdict ? `<span class="tag ${tagClass(verdict)}">${verdict}</span>` : "");
    div.appendChild(step);
  }
  root.appendChild(div);
}

const approvalsSection = document.createElement("div");
approvalsSection.className = "approvals";
approvalsSection.innerHTML = "<h1>Approvals</h1>";
for (const a of data.approvals) {
  const div = document.createElement("div");
  div.className = "approval";
  div.innerHTML = `<div>${a.approval_id} <span class="tag ${tagClass(a.state)}">${a.state}</span> ` +
    `tool=${a.plan.tool || ""} session=${a.session_id}</div>` +
    (a.state === "pending"
      ? `<div class="cmd">belay approvals approve ${a.approval_id} --by &lt;you&gt; --db ${data.db_path}</div>`
      : "");
  approvalsSection.appendChild(div);
}
root.appendChild(approvalsSection);
</script>
</body>
</html>
"""


def render_dashboard(db_path: str) -> str:
    data = build_dashboard_data(db_path)
    html = _TEMPLATE.replace("DATA_DB_PATH", data["db_path"])
    html = html.replace("DATA_JSON", json.dumps(data).replace("</script>", "<\\/script>"))
    return html
