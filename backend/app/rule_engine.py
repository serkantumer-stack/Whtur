"""Adım 2 — Outbound Kural Motoru.

İki segment için mantıksal karar ağaçları ve tekrar-aranma kural tablosu.
Bu modül kuraların TEK KAYNAĞIDIR: API, Pandas export scripti ve seed
verisi hepsi buradan okur.

Segmentler
----------
converted     : Geçerli (confirmed/completed) rezervasyonu olan misafirler.
non_converted : Teklif almış / temas edilmiş ama satın alma gerçekleşmemiş lead'ler.
"""
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta

from .models import Contact, Guest, Offer, OutboundTask, Reservation

# ---------------------------------------------------------------------------
# KURAL TABLOSU — Satın Almayanlar (Non-Converted / Lead)
#   delay_days   : son temastan kaç gün sonra tekrar aranacak (None -> sezonlik)
#   seasonal     : "sept" = Erken Rezervasyon dönemi (1 Eylül) stratejisi
#   priority     : 1 Yüksek | 2 Orta | 3 Düşük
#   max_attempts : kural başına maksimum deneme sayısı
# ---------------------------------------------------------------------------
LEAD_RULES = {
    "FIYAT_YUKSEK":       {"delay_days": 30,  "priority": 2, "max_attempts": 3},
    "ERKEN_REZ_BEKLIYOR": {"delay_days": None, "seasonal": "sept", "priority": 1, "max_attempts": 2},
    "TARIH_UYGUN_DEGIL":  {"delay_days": 60,  "priority": 2, "max_attempts": 2},
    "DUSUNECEK":          {"delay_days": 14,  "priority": 2, "max_attempts": 3},
    "TEKRAR_ARANACAK":    {"delay_days": 21,  "priority": 1, "max_attempts": 3},
    "ULASILAMADI":        {"delay_days": 7,   "priority": 1, "max_attempts": 5},
    "ILGI_GOSTERMEDI":    {"delay_days": 365, "priority": 3, "max_attempts": 1},
    "PLANIM_YOK":         {"delay_days": 180, "priority": 3, "max_attempts": 1},
}

RULE_DESCRIPTIONS = {
    "FIYAT_YUKSEK": "Fiyat yüksek — kampanya/indirim haber verilerek tekrar yaklaş",
    "ERKEN_REZ_BEKLIYOR": "Erken rezervasyon bekliyor — 1 Eylül'de erken rezervasyon teklifiyle ara",
    "TARIH_UYGUN_DEGIL": "Tarihler uygun değil — alternatif dönem öner",
    "DUSUNECEK": "Düşünecek — karar sorulmak üzere kısa vadede ara",
    "TEKRAR_ARANACAK": "Müşteri aranmamızı istedi — söz verilen tarihte ara",
    "ULASILAMADI": "Ulaşılamadı — kısa aralıklarla tekrar dene",
    "ILGI_GOSTERMEDI": "İlgilenmiyor — yıllık tekrar satış kontrolü",
    "PLANIM_YOK": "Bu dönem planı yok — 6 ay sonra dönem kontrolü",
}

# ---------------------------------------------------------------------------
# KURAL — Satın Alanlar (Converted): Yıl Dönümü / Anniversary stratejisi
# Misafir X tarihinde rezervasyon yaptıysa ve o tarihten sonra yeni bir
# temas/rezervasyon yoksa, rezervasyondan 11 ay sonra, önceki konaklama
# ayını hedefleyen bir outbound görevi tetiklenir.
# ---------------------------------------------------------------------------
CONVERTED_RULE = {
    "key": "CONVERTED_ANNIVERSARY",
    "trigger_after_months": 11,
    "target": "Önceki konaklama ayının bir sonraki yıl karşılığı (YYYY-MM)",
    "priority": 1,
    "max_attempts": 2,
}

PRIORITY_NAMES = {1: "Yüksek", 2: "Orta", 3: "Düşük"}
VALID_RESERVATION_STATUS = ("confirmed", "completed")
TRIGGER_LEAD = "RESULT_CODE_RETRY"
TRIGGER_CONVERTED = CONVERTED_RULE["key"]


def next_sept_1(d: date) -> date:
    """Verilen tarihten sonraki 1 Eylül (Erken Rezervasyon dönemi)."""
    sept = date(d.year, 9, 1)
    return sept if d <= sept else date(d.year + 1, 9, 1)


def next_period_for_month(month: int, as_of: date) -> str:
    """Konaklama ayının as_of sonrası ilk gerçekleşme yılını 'YYYY-MM' döndürür."""
    year = as_of.year + (1 if month < as_of.month else 0)
    return f"{year}-{month:02d}"


# ---------------------------------------------------------------------------
# Karar ağacı metinleri (UI + dokümantasyon)
# ---------------------------------------------------------------------------
CONVERTED_TREE = """\
SATIN ALANLAR (CONVERTED) — KARAR AĞACI
1. Misafirin geçerli (confirmed/completed) EN YENİ rezervasyonu al.
2. Misafirin son teması, rezervasyon tarihinden SONRA mı?
   EVET  -> misafir Lead akışındadır; converted kuralı uygulanmaz.
   HAYIR -> 3. adıma geç.
3. Konaklama gelecekte mi (check_in > bugün)?
   EVET  -> aktif yaklaşan konaklama var; görev OLUŞTURMA.
   HAYIR -> 4. adıma geç.
4. Tamamlanmış CONVERTED_ANNIVERSARY görev sayısı >= max_attempts (2) mi?
   EVET  -> kapanmış hesap; görev OLUŞTURMA.
   HAYIR -> 5. adıma geç.
5. GÖREV OLUŞTUR:
   - tekrar_aranma_tarihi = rezervasyon tarihi + 11 ay
   - hedef_dönem          = önceki konaklama ayının bir sonraki yıl karşılığı (YYYY-MM)
   - öncelik = 1 (Yüksek), segment = converted
Örnek: 25.09.2025'te Ekim 2025 için rezervasyon, 2026'da aktivite yok
       -> Ağustos/Eylül 2026'da aranma görevi, hedef dönem 2026-10."""

LEAD_TREE = """\
SATIN ALMAYANLAR (NON-CONVERTED / LEAD) — KARAR AĞACI
1. Misafirin GEÇERLİ rezervasyonu var mı?
   EVET  -> converted akışına dahildir; lead kuralı uygulanmaz.
   HAYIR -> 2. adıma geç.
2. Misafirin SON temasını ve sonuç kodunu al.
3. Sonuç kodu kural tablosunda var mı? (MEMNUN_OLMADI gibi kapanış kodları YOK)
   HAYIR -> görev OLUŞTURMA.
   EVET  -> 4. adıma geç.
4. Tamamlanmış lead görev sayısı >= kuralın max_attempts değeri mi?
   EVET  -> deneme limiti doldu; görev OLUŞTURMA.
   HAYIR -> 5. adıma geç.
5. GÖREV OLUŞTUR:
   - tekrar_aranma_tarihi = son temas + kural gecikmesi
     * FIYAT_YUKSEK -> +30 gün   | DUSUNECEK -> +14 gün | TEKRAR_ARANACAK -> +21 gün
     * ULASILAMADI   -> +7 gün    | TARIH_UYGUN_DEGIL -> +60 gün
     * ERKEN_REZ_BEKLIYOR -> sonraki 1 Eylül (erken rezervasyon dönemi)
     * ILGI_GOSTERMEDI -> +365 gün | PLANIM_YOK -> +180 gün
   - öncelik ve deneme limiti kural tablosundan
Not: Görev çağrısı yapıldığında yeni bir temas kaydı oluşur; bir sonraki
     motor çalışması son temasa göre yeniden planlar (dinamik döngü)."""


# ---------------------------------------------------------------------------
# Motor — bekleyen görevleri kural tablosuna göre (yeniden) üretir.
# ---------------------------------------------------------------------------
def _completed_count(db, guest_id: int, segment: str) -> int:
    return (
        db.query(OutboundTask)
        .filter(
            OutboundTask.guest_id == guest_id,
            OutboundTask.status == "completed",
            OutboundTask.segment == segment,
        )
        .count()
    )


def run_engine(db, as_of: date | None = None) -> dict:
    """Bekleyen (pending) görevleri silip kaynak tablolardan yeniden üretir.

    İdempotenttir; tamamlanmış görevler deneme sayacı için korunur.
    """
    as_of = as_of or date.today()
    db.query(OutboundTask).filter(OutboundTask.status == "pending").delete(
        synchronize_session=False
    )
    created = {"converted": 0, "non_converted": 0}

    for guest in db.query(Guest).all():
        res = (
            db.query(Reservation)
            .filter(
                Reservation.guest_id == guest.guest_id,
                Reservation.status.in_(VALID_RESERVATION_STATUS),
            )
            .order_by(Reservation.booking_date.desc())
            .first()
        )
        last = (
            db.query(Contact)
            .filter(Contact.guest_id == guest.guest_id)
            .order_by(Contact.contact_date.desc())
            .first()
        )

        task = None
        # --- Segment 1: Satın Alanlar (Converted) — yıl dönümü kuralı
        if res is not None and (last is None or last.contact_date < res.booking_date):
            if res.check_in <= as_of:  # yaklaşan aktif konaklama yok
                attempts = _completed_count(db, guest.guest_id, "converted")
                if attempts < CONVERTED_RULE["max_attempts"]:
                    target_period = next_period_for_month(res.check_in.month, as_of)
                    task = OutboundTask(
                        guest_id=guest.guest_id,
                        segment="converted",
                        trigger_rule=TRIGGER_CONVERTED,
                        result_code="SATIN_ALDI",
                        scheduled_date=res.booking_date
                        + relativedelta(months=CONVERTED_RULE["trigger_after_months"]),
                        target_period=target_period,
                        priority=CONVERTED_RULE["priority"],
                        attempt_no=attempts + 1,
                        status="pending",
                        notes=(
                            f"{res.check_in:%d.%m.%Y} konaklamasının yıldönümü — "
                            f"hedef dönem {target_period}"
                        ),
                    )
        # --- Segment 2: Satın Almayanlar (Lead) — sonuç kodu kuralı
        elif last is not None:
            rule = LEAD_RULES.get(last.result_code)
            if rule:
                attempts = _completed_count(db, guest.guest_id, "non_converted")
                if attempts < rule["max_attempts"]:
                    if rule.get("seasonal") == "sept":
                        scheduled = next_sept_1(last.contact_date)
                    else:
                        scheduled = last.contact_date + timedelta(days=rule["delay_days"])
                    offer = (
                        db.query(Offer)
                        .filter(Offer.guest_id == guest.guest_id)
                        .order_by(Offer.offer_date.desc())
                        .first()
                    )
                    task = OutboundTask(
                        guest_id=guest.guest_id,
                        segment="non_converted",
                        trigger_rule=TRIGGER_LEAD,
                        result_code=last.result_code,
                        scheduled_date=scheduled,
                        target_period=offer.target_period if offer else None,
                        priority=rule["priority"],
                        attempt_no=attempts + 1,
                        status="pending",
                        notes=RULE_DESCRIPTIONS.get(last.result_code, ""),
                    )

        if task:
            db.add(task)
            created[task.segment] += 1

    db.commit()
    return created
