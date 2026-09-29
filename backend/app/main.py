"""Whtur Outbound CRM — API + Panel (FastAPI).

Uçlar:
    GET  /api/stats        KPI'lar (misafir, segment, görev, vadesi gelen)
    GET  /api/tasks        Outbound arama listesi (segment / zaman filtreli)
    POST /api/engine/run   Kural motorunu çalıştır (pending görevleri yeniden üret)
    GET  /api/rules        Kural tablosu + karar ağaçları
    GET  /api/schema       Veri modeli (reflected: tablo, kolon, PK/FK)
    GET  /api/export/csv   Pandas ile üretilmiş arama listesi (CSV indirme)
"""
from datetime import date
from io import StringIO
from pathlib import Path

from fastapi import Depends, FastAPI, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, inspect as sa_inspect
from sqlalchemy.orm import Session

from .database import Base, SessionLocal, engine, get_db
from .models import Contact, Guest, OutboundTask, Reservation
from .rule_engine import (
    CONVERTED_RULE,
    CONVERTED_TREE,
    LEAD_RULES,
    LEAD_TREE,
    PRIORITY_NAMES,
    RULE_DESCRIPTIONS,
    TRIGGER_CONVERTED,
    run_engine,
)
from .seed_data import seed_if_empty

app = FastAPI(title="Whtur Outbound CRM", version="1.0.0")

TABLE_INFO = {
    "guests": "Misafirler — ana müşteri kaydı",
    "agents": "Çağrı merkezi temsilcileri",
    "contacts": "Temas kayıtları (ilk/son temas bu tablodan türetilir)",
    "result_codes": "Sonuç kodları referans tablosu (kural kaynağı)",
    "offers": "Teklifler — hedef konaklama dönemi ile",
    "reservations": "Rezervasyonlar (satın alma kaydı)",
    "outbound_tasks": "Outbound aranma görevleri (kural motoru çıktısı)",
}


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    seed_if_empty()
    db = SessionLocal()
    try:
        run_engine(db)
    finally:
        db.close()


@app.get("/api/stats")
def stats(db: Session = Depends(get_db)):
    today = date.today()
    guests = db.query(Guest).count()
    converted = (
        db.query(Guest.guest_id)
        .join(Reservation, Reservation.guest_id == Guest.guest_id)
        .filter(Reservation.status.in_(["confirmed", "completed"]))
        .distinct()
        .count()
    )
    pending_q = db.query(OutboundTask).filter(OutboundTask.status == "pending")
    by_priority = dict(
        db.query(OutboundTask.priority, func.count(OutboundTask.task_id))
        .filter(OutboundTask.status == "pending")
        .group_by(OutboundTask.priority)
        .all()
    )
    by_segment = dict(
        db.query(OutboundTask.segment, func.count(OutboundTask.task_id))
        .filter(OutboundTask.status == "pending")
        .group_by(OutboundTask.segment)
        .all()
    )
    return {
        "as_of": today.isoformat(),
        "guests": guests,
        "converted": converted,
        "leads": guests - converted,
        "pending_tasks": pending_q.count(),
        "due_tasks": pending_q.filter(OutboundTask.scheduled_date <= today).count(),
        "by_priority": {PRIORITY_NAMES.get(k, str(k)): v for k, v in by_priority.items()},
        "by_segment": by_segment,
    }


@app.get("/api/tasks")
def list_tasks(
    segment: str = Query("all"),
    due: str = Query("all"),  # all | due | upcoming
    db: Session = Depends(get_db),
):
    today = date.today()
    q = db.query(OutboundTask)
    if segment in ("converted", "non_converted"):
        q = q.filter(OutboundTask.segment == segment)
    tasks = (
        q.filter(OutboundTask.status == "pending")
        .order_by(OutboundTask.priority.asc(), OutboundTask.scheduled_date.asc())
        .all()
    )

    last_contacts = {}
    for gid, cdate in (
        db.query(Contact.guest_id, Contact.contact_date)
        .order_by(Contact.contact_date.desc())
        .all()
    ):
        last_contacts.setdefault(gid, cdate)

    items = []
    for t in tasks:
        overdue = t.scheduled_date <= today
        if due == "due" and not overdue:
            continue
        if due == "upcoming" and overdue:
            continue
        items.append({
            "task_id": t.task_id,
            "guest_id": t.guest_id,
            "name": f"{t.guest.first_name} {t.guest.last_name}",
            "phone": t.guest.phone,
            "segment": t.segment,
            "trigger_rule": t.trigger_rule,
            "result_code": t.result_code,
            "scheduled_date": t.scheduled_date.isoformat(),
            "target_period": t.target_period,
            "priority": t.priority,
            "priority_name": PRIORITY_NAMES.get(t.priority, "-"),
            "attempt_no": t.attempt_no,
            "notes": t.notes,
            "last_contact": last_contacts[t.guest_id].isoformat() if t.guest_id in last_contacts else None,
            "overdue": overdue,
        })
    return {"as_of": today.isoformat(), "count": len(items), "items": items}


@app.post("/api/engine/run")
def engine_run(db: Session = Depends(get_db)):
    created = run_engine(db)
    return {"ok": True, "created": created}


@app.get("/api/rules")
def rules():
    lead = []
    for code, r in LEAD_RULES.items():
        lead.append({
            "code": code,
            "description": RULE_DESCRIPTIONS.get(code, ""),
            "strategy": (
                "Sonraki 1 Eylül (Erken Rezervasyon dönemi)"
                if r.get("seasonal") == "sept"
                else f"Son temastan +{r['delay_days']} gün"
            ),
            "priority": r["priority"],
            "priority_name": PRIORITY_NAMES[r["priority"]],
            "max_attempts": r["max_attempts"],
        })
    return {
        "lead_rules": lead,
        "converted_rule": {
            "key": TRIGGER_CONVERTED,
            "trigger_after_months": CONVERTED_RULE["trigger_after_months"],
            "target": CONVERTED_RULE["target"],
            "priority_name": PRIORITY_NAMES[CONVERTED_RULE["priority"]],
            "max_attempts": CONVERTED_RULE["max_attempts"],
        },
        "lead_tree": LEAD_TREE,
        "converted_tree": CONVERTED_TREE,
    }


@app.get("/api/schema")
def schema():
    insp = sa_inspect(engine)
    tables = []
    for name in insp.get_table_names():
        pk = set(insp.get_pk_constraint(name)["constrained_columns"] or [])
        fk_map = {}
        for fk in insp.get_foreign_keys(name):
            for col, ref in zip(fk["constrained_columns"], fk["referred_columns"]):
                fk_map[col] = f"{fk['referred_table']}.{ref}"
        columns = [
            {
                "name": c["name"],
                "type": str(c["type"]),
                "nullable": c["nullable"],
                "pk": c["name"] in pk,
                "fk": fk_map.get(c["name"]),
            }
            for c in insp.get_columns(name)
        ]
        tables.append({
            "name": name,
            "description": TABLE_INFO.get(name, ""),
            "columns": columns,
        })
    return {"tables": tables}


@app.get("/api/export/csv")
def export_csv(segment: str = Query("all"), due_only: bool = Query(False)):
    from .export import build_outbound_list

    df = build_outbound_list(engine, date.today(), segment=segment, due_only=due_only)
    buf = StringIO()
    df.to_csv(buf, index=False, encoding="utf-8-sig")
    filename = f"outbound_list_{date.today():%Y%m%d}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
