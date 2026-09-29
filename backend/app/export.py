"""Adım 3 — Pandas tabanlı outbound arama listesi üretimi.

API'deki /api/export/csv ucu ve scripts/generate_outbound_list.py CLI aracı
bu modülü kullanır. Kurallar rule_engine.py'den okunur (tek kaynak).
"""
from datetime import date

import pandas as pd

from .rule_engine import (
    CONVERTED_RULE,
    LEAD_RULES,
    PRIORITY_NAMES,
    RULE_DESCRIPTIONS,
    TRIGGER_CONVERTED,
    TRIGGER_LEAD,
    next_sept_1,
)

OUT_COLUMNS = [
    "misafir_id", "ad", "soyad", "telefon", "email", "segment", "kural",
    "sonuc_kodu", "tekrar_aranma_tarihi", "hedef_donem", "oncelik",
    "oncelik_adi", "deneme_no", "son_temas_tarihi", "son_rezervasyon_tarihi",
    "notlar",
]

SEGMENT_NAMES = {"converted": "Satın Alan", "non_converted": "Potansiyel (Lead)"}

_PIPE_COLS = ["guest_id", "first_name", "last_name", "phone", "email", "segment",
              "trigger_rule", "result_code", "scheduled_date", "target_period",
              "priority", "attempt_no", "last_contact", "last_booking", "notes"]


def _read_contacts(engine):
    df = pd.read_sql(
        "SELECT c.guest_id, c.contact_date, c.result_code, "
        "g.first_name, g.last_name, g.phone, g.email "
        "FROM contacts c JOIN guests g ON g.guest_id = c.guest_id", engine)
    if not df.empty:
        df["contact_date"] = pd.to_datetime(df["contact_date"])
    return df


def _read_reservations(engine):
    df = pd.read_sql(
        "SELECT guest_id, booking_date, check_in, status FROM reservations "
        "WHERE status IN ('confirmed', 'completed')", engine)
    if not df.empty:
        df["booking_date"] = pd.to_datetime(df["booking_date"])
        df["check_in"] = pd.to_datetime(df["check_in"])
    return df


def _read_done_attempts(engine):
    return pd.read_sql(
        "SELECT guest_id, segment, COUNT(*) AS attempts FROM outbound_tasks "
        "WHERE status = 'completed' GROUP BY guest_id, segment", engine)


def _last_by(df, sort_col):
    if df.empty:
        return df
    return df.sort_values(sort_col).groupby("guest_id", as_index=False).tail(1)


def _attempts_for(tasks_done, segment):
    if tasks_done.empty:
        return pd.DataFrame(columns=["guest_id", "attempts"])
    return tasks_done[tasks_done["segment"] == segment][["guest_id", "attempts"]].copy()


def _lead_pipeline(engine, contacts, reservations, tasks_done, as_of):
    if contacts.empty:
        return pd.DataFrame(columns=_PIPE_COLS)

    m = _last_by(contacts, "contact_date").copy()
    rules = pd.DataFrame([
        {"code": k, "delay_days": v.get("delay_days"), "seasonal": v.get("seasonal"),
         "priority": v["priority"], "max_attempts": v["max_attempts"],
         "notes": RULE_DESCRIPTIONS[k]}
        for k, v in LEAD_RULES.items()])
    m = m.merge(rules, left_on="result_code", right_on="code", how="inner")
    if m.empty:
        return pd.DataFrame(columns=_PIPE_COLS)

    # Converted akışına ait olanlar (son temas satın almadan önce) lead listesine girmez
    if reservations.empty:
        m = m.copy()
        m["booking_date"] = pd.NaT
    else:
        m = m.merge(_last_by(reservations, "booking_date")[["guest_id", "booking_date"]],
                    on="guest_id", how="left")
    m = m[m["booking_date"].isna() | (m["contact_date"] >= m["booking_date"])]

    # Deneme limiti
    m = m.merge(_attempts_for(tasks_done, "non_converted"), on="guest_id", how="left")
    m["attempts"] = m["attempts"].fillna(0).astype(int)
    m = m[m["attempts"] < m["max_attempts"]]
    if m.empty:
        return pd.DataFrame(columns=_PIPE_COLS)
    m = m.reset_index(drop=True)

    # Tekrar aranma zamanı: sabit gecikme veya sezonluk (1 Eylül)
    m["scheduled_date"] = pd.NaT
    fixed = m["delay_days"].notna()
    if fixed.any():
        m.loc[fixed, "scheduled_date"] = (
            m.loc[fixed, "contact_date"]
            + pd.to_timedelta(m.loc[fixed, "delay_days"], unit="D"))
    seasonal = m["seasonal"].eq("sept") & m["scheduled_date"].isna()
    if seasonal.any():
        m.loc[seasonal, "scheduled_date"] = m.loc[seasonal, "contact_date"].map(
            lambda d: pd.Timestamp(next_sept_1(d.date())))

    # Hedef dönem: varsa son teklifin dönemi
    offers = pd.read_sql("SELECT guest_id, offer_date, target_period FROM offers", engine)
    if offers.empty:
        m["target_period"] = None
    else:
        m = m.merge(_last_by(offers, "offer_date")[["guest_id", "target_period"]],
                    on="guest_id", how="left")

    return pd.DataFrame({
        "guest_id": m["guest_id"],
        "first_name": m["first_name"],
        "last_name": m["last_name"],
        "phone": m["phone"],
        "email": m["email"],
        "segment": "non_converted",
        "trigger_rule": TRIGGER_LEAD,
        "result_code": m["result_code"],
        "scheduled_date": pd.to_datetime(m["scheduled_date"]),
        "target_period": m["target_period"],
        "priority": m["priority"],
        "attempt_no": m["attempts"] + 1,
        "last_contact": pd.to_datetime(m["contact_date"]),
        "last_booking": pd.to_datetime(m["booking_date"]),
        "notes": m["notes"],
    })


def _converted_pipeline(engine, contacts, reservations, tasks_done, as_of):
    if reservations.empty:
        return pd.DataFrame(columns=_PIPE_COLS)

    lr = _last_by(reservations, "booking_date").copy()
    if contacts.empty:
        lr = lr.copy()
        lr["contact_date"] = pd.NaT
    else:
        lr = lr.merge(_last_by(contacts, "contact_date")[["guest_id", "contact_date"]],
                      on="guest_id", how="left")
    # Son temas satın almadan önceyse misafir converted akışındadır
    lr = lr[lr["contact_date"].isna() | (lr["contact_date"] < lr["booking_date"])]
    # Yaklaşan aktif konaklama yok
    lr = lr[lr["check_in"] <= pd.Timestamp(as_of)]

    lr = lr.merge(_attempts_for(tasks_done, "converted"), on="guest_id", how="left")
    lr["attempts"] = lr["attempts"].fillna(0).astype(int)
    lr = lr[lr["attempts"] < CONVERTED_RULE["max_attempts"]]
    if lr.empty:
        return pd.DataFrame(columns=_PIPE_COLS)
    lr = lr.reset_index(drop=True)

    # Yıl dönümü: rezervasyon + 11 ay, hedef dönem önceki konaklama ayı
    lr["scheduled_date"] = lr["booking_date"] + pd.DateOffset(
        months=CONVERTED_RULE["trigger_after_months"])
    stay_month = lr["check_in"].dt.month
    target_year = as_of.year + (stay_month < as_of.month).astype(int)
    lr["target_period"] = target_year.astype(str) + "-" + stay_month.astype(str).str.zfill(2)

    guests = pd.read_sql(
        "SELECT guest_id, first_name, last_name, phone, email FROM guests", engine)
    lr = lr.merge(guests, on="guest_id", how="left")

    return pd.DataFrame({
        "guest_id": lr["guest_id"],
        "first_name": lr["first_name"],
        "last_name": lr["last_name"],
        "phone": lr["phone"],
        "email": lr["email"],
        "segment": "converted",
        "trigger_rule": TRIGGER_CONVERTED,
        "result_code": "SATIN_ALDI",
        "scheduled_date": pd.to_datetime(lr["scheduled_date"]),
        "target_period": lr["target_period"],
        "priority": CONVERTED_RULE["priority"],
        "attempt_no": lr["attempts"] + 1,
        "last_contact": pd.to_datetime(lr["contact_date"]),
        "last_booking": pd.to_datetime(lr["booking_date"]),
        "notes": "Yıldönümü — " + lr["target_period"] + " dönemi için tekrar satış",
    })


def build_outbound_list(engine, as_of: date, segment: str = "all",
                        due_only: bool = False) -> pd.DataFrame:
    """Kaynak tabloları okur, kural tablosunu pandas ile uygular ve
    operasyonel arama listesini (CSV'ye hazır) döndürür."""
    contacts = _read_contacts(engine)
    reservations = _read_reservations(engine)
    tasks_done = _read_done_attempts(engine)

    frames = []
    if segment in ("all", "non_converted"):
        frames.append(_lead_pipeline(engine, contacts, reservations, tasks_done, as_of))
    if segment in ("all", "converted"):
        frames.append(_converted_pipeline(engine, contacts, reservations, tasks_done, as_of))
    if not frames:
        return pd.DataFrame(columns=OUT_COLUMNS)
    df = pd.concat(frames, ignore_index=True)
    if df.empty:
        return pd.DataFrame(columns=OUT_COLUMNS)

    if due_only:
        df = df[df["scheduled_date"] <= pd.Timestamp(as_of)]
    df = df.sort_values(["priority", "scheduled_date"]).reset_index(drop=True)

    out = pd.DataFrame({
        "misafir_id": df["guest_id"],
        "ad": df["first_name"],
        "soyad": df["last_name"],
        "telefon": df["phone"],
        "email": df["email"],
        "segment": df["segment"].map(SEGMENT_NAMES).fillna(df["segment"]),
        "kural": df["trigger_rule"],
        "sonuc_kodu": df["result_code"],
        "tekrar_aranma_tarihi": df["scheduled_date"].dt.strftime("%Y-%m-%d"),
        "hedef_donem": df["target_period"],
        "oncelik": df["priority"],
        "oncelik_adi": df["priority"].map(PRIORITY_NAMES),
        "deneme_no": df["attempt_no"],
        "son_temas_tarihi": df["last_contact"].dt.strftime("%Y-%m-%d"),
        "son_rezervasyon_tarihi": df["last_booking"].dt.strftime("%Y-%m-%d"),
        "notlar": df["notes"],
    })
    return out[OUT_COLUMNS]
