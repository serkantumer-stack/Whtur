"""Demo verisi — boş veritabanına bir kez yüklenir.

Tarihler bugüne (as_of) göre üretilir; böylece senaryolar her ortamda
doğru vade durumlarını (vadesi gelmiş / yaklaşan) üretir.
"""
import random
from datetime import date, timedelta

from .database import SessionLocal
from .models import Agent, Contact, Guest, Offer, OutboundTask, Reservation, ResultCode
from .rule_engine import TRIGGER_LEAD

FIRST = ["Ahmet", "Ayşe", "Mehmet", "Fatma", "Mustafa", "Zeynep", "Emre", "Elif",
         "Burak", "Ceren", "Deniz", "Gizem", "Hakan", "İrem", "Kaan", "Leyla",
         "Murat", "Nil", "Ozan", "Pelin", "Selim", "Tuğçe"]
LAST = ["Yılmaz", "Kaya", "Demir", "Şahin", "Çelik", "Yıldız", "Aydın", "Öztürk",
        "Arslan", "Doğan", "Kılıç", "Aslan", "Çetin", "Kara", "Koç", "Kurt",
        "Özdemir", "Şimşek"]
CITIES = ["İstanbul", "Ankara", "İzmir", "Bursa", "Antalya", "Adana"]

RESULT_CODES = [
    ("SATIN_ALDI", "Rezervasyon yapıldı (satın alma)", "converted", 0),
    ("FIYAT_YUKSEK", "Fiyat yüksek bulundu", "non_converted", 0),
    ("ERKEN_REZ_BEKLIYOR", "Erken rezervasyon dönemini bekliyor", "non_converted", 0),
    ("TARIH_UYGUN_DEGIL", "Tarihler uygun değil", "non_converted", 0),
    ("DUSUNECEK", "Düşünüp dönüş yapacak", "non_converted", 0),
    ("TEKRAR_ARANACAK", "Müşteri daha sonra aranmamızı istedi", "non_converted", 0),
    ("ULASILAMADI", "Ulaşılamadı / meşgul", "non_converted", 0),
    ("ILGI_GOSTERMEDI", "İlgilenmiyor", "non_converted", 1),
    ("PLANIM_YOK", "Bu dönem planı yok", "non_converted", 0),
    ("MEMNUN_OLMADI", "Memnun kalmadı (kapanış kodu)", "non_converted", 1),
]


def seed_if_empty() -> bool:
    db = SessionLocal()
    try:
        if db.query(Guest).count() > 0:
            return False
        _seed(db)
        db.commit()
        return True
    finally:
        db.close()


def _seed(db):
    today = date.today()
    rng = random.Random(42)
    next_year = today.year + 1

    db.add_all([ResultCode(code=c, description=d, category=cat, is_final=f)
                for c, d, cat, f in RESULT_CODES])
    a_in = Agent(name="Ceren Aksoy", team="inbound")
    a_out1 = Agent(name="Elif Yıldız", team="outbound")
    a_out2 = Agent(name="Burak Demir", team="outbound")
    db.add_all([a_in, a_out1, a_out2])
    db.flush()

    counter = {"i": 0}

    def guest():
        i = counter["i"]
        counter["i"] += 1
        g = Guest(
            first_name=FIRST[i % len(FIRST)],
            last_name=LAST[(i * 7 + 3) % len(LAST)],
            phone=f"+90 5{rng.randint(10, 55)} {rng.randint(100, 999)} {rng.randint(10, 99)} {rng.randint(10, 99)}",
            email=f"misafir{i + 1}@ornek.com" if i % 2 == 0 else None,
            city=rng.choice(CITIES),
        )
        db.add(g)
        db.flush()
        return g

    def contact(g, days_ago, code, agent, ctype="call_in", notes=None):
        c = Contact(guest_id=g.guest_id, agent_id=agent.agent_id,
                    contact_date=today - timedelta(days=days_ago),
                    contact_type=ctype, result_code=code, notes=notes)
        db.add(c)
        db.flush()
        return c

    def offer(g, c, days_ago, period, price):
        o = Offer(guest_id=g.guest_id, contact_id=c.contact_id,
                  offer_date=today - timedelta(days=days_ago),
                  target_period=period, hotel="Whtur Beach Resort Antalya",
                  room_type=rng.choice(["Standart", "Superior", "Deluxe", "Suit"]),
                  board=rng.choice(["OB", "BB", "UP", "AI"]),
                  price_amount=price, currency="EUR", status="sent")
        db.add(o)
        db.flush()
        return o

    def converted_booking(g, booking_days_ago, stay_month, stay_day=5,
                           nights=7, status="completed"):
        """İnbound arama -> satın alma -> konaklama senaryosu."""
        c = contact(g, booking_days_ago + 1, "SATIN_ALDI", a_in, "call_in",
                    "Inbound arama — teklif kabul edildi")
        offer(g, c, booking_days_ago + 1, f"{today.year if stay_month >= (today - timedelta(days=booking_days_ago)).month else today.year + 1}-{stay_month:02d}", rng.randint(1200, 2600))
        booking = today - timedelta(days=booking_days_ago)
        check_in = date(booking.year, stay_month, stay_day)
        r = Reservation(guest_id=g.guest_id,
                        booking_date=booking, check_in=check_in,
                        check_out=check_in + timedelta(days=nights),
                        hotel="Whtur Beach Resort Antalya",
                        total_amount=rng.randint(1100, 2800), currency="EUR",
                        status=status)
        db.add(r)
        return r

    def lead(g, days_ago, code, period=None, price=1450, ctype="call_in",
             agent=None, notes=None):
        c = contact(g, days_ago, code, agent or a_in, ctype, notes)
        if period:
            offer(g, c, days_ago, period, price)
        return c

    def completed_task(g, scheduled_days_ago, code, period, attempt=1):
        db.add(OutboundTask(
            guest_id=g.guest_id, segment="non_converted", trigger_rule=TRIGGER_LEAD,
            result_code=code, scheduled_date=today - timedelta(days=scheduled_days_ago),
            target_period=period, priority=2, attempt_no=attempt,
            status="completed", notes=f"{attempt}. deneme tamamlandı"))

    # ------------------------------------------------------------------
    # SEGMENT 1 — SATIN ALANLAR (Converted)
    # ------------------------------------------------------------------
    # 1a) Yıldönümü VADESİ GELMİŞ: Eylül/Ekim 2025 alımı, 2026'da aktivite yok
    #     -> booking + 11 ay ≈ Ağustos 2026, hedef dönem Ekim 2026
    for _ in range(6):
        converted_booking(guest(), booking_days_ago=370 + rng.randint(0, 15),
                          stay_month=10, nights=rng.randint(5, 10))
    # 1b) Yıldönümü YAKLAŞAN: geçen yılsonu alımı -> vade bu hafta
    converted_booking(guest(), booking_days_ago=330, stay_month=12, nights=7)
    # 1c) Yeni alım: henüz 11 ay dolmadı -> vade gelecek yıl
    converted_booking(guest(), booking_days_ago=200, stay_month=5, nights=7)
    # 1d) Yenilenen misafir: 2025 alımı + 2026'da yeni rezervasyon (yaklaşan konaklama) -> görev YOK
    for _ in range(2):
        g = guest()
        converted_booking(g, booking_days_ago=400, stay_month=8, nights=7)
        c = contact(g, 61, "SATIN_ALDI", a_in, "call_in", "Tekrar satış — kabul edildi")
        offer(g, c, 61, f"{today.year}-12", rng.randint(1200, 2600))
        booking = today - timedelta(days=60)
        db.add(Reservation(guest_id=g.guest_id, booking_date=booking,
                           check_in=date(booking.year, 12, 10),
                           check_out=date(booking.year, 12, 17),
                           hotel="Whtur Ski Resort Erzurum",
                           total_amount=rng.randint(1100, 2800), currency="EUR",
                           status="confirmed"))

    # ------------------------------------------------------------------
    # SEGMENT 2 — SATIN ALMAYANLAR (Lead)
    # ------------------------------------------------------------------
    # 2a) FIYAT_YUKSEK — 1. deneme, vadesi geldi (+30 gün)
    for _ in range(3):
        lead(guest(), 40, "FIYAT_YUKSEK", period=f"{next_year}-07", price=1850)
    # 2b) FIYAT_YUKSEK — 2. deneme (1 outbound çağrı yapılmış, yine fiyat itirazı)
    for _ in range(2):
        g = guest()
        lead(g, 70, "FIYAT_YUKSEK", period=f"{next_year}-07", price=1900)
        completed_task(g, 40, "FIYAT_YUKSEK", f"{next_year}-07", attempt=1)
        lead(g, 40, "FIYAT_YUKSEK", period=f"{next_year}-07", price=1780,
             ctype="call_out", agent=a_out1, notes="Outbound — fiyat yine yüksek")
    # 2c) ERKEN_REZ_BEKLIYOR — yazın temas, 1 Eylül'de erken rezervasyon teklifi
    for _ in range(3):
        lead(guest(), 120, "ERKEN_REZ_BEKLIYOR", period=f"{next_year}-07", price=1400)
    # 2d) ULASILAMADI — +7 gün
    for _ in range(2):
        lead(guest(), 9, "ULASILAMADI", ctype="call_out", agent=a_out1, notes="Meşgul")
    # 2e) DUSUNECEK — +14 gün (henüz vadesi gelmedi)
    for _ in range(2):
        lead(guest(), 5, "DUSUNECEK", period=f"{next_year}-06", price=1650)
    # 2f) TEKRAR_ARANACAK — +21 gün
    for _ in range(2):
        lead(guest(), 25, "TEKRAR_ARANACAK", period=f"{next_year}-05", price=1290)
    # 2g) TARIH_UYGUN_DEGIL — +60 gün
    for _ in range(2):
        lead(guest(), 70, "TARIH_UYGUN_DEGIL", period=f"{next_year}-07", price=1550)
    # 2h) PLANIM_YOK — +180 gün
    lead(guest(), 200, "PLANIM_YOK")
    # 2i) ILGI_GOSTERMEDI — 1. yıllık kontrol görevi
    lead(guest(), 400, "ILGI_GOSTERMEDI")
    # 2j) ILGI_GOSTERMEDI — deneme limiti doldu, YENİ görev OLMAMALI
    g = guest()
    lead(g, 400, "ILGI_GOSTERMEDI")
    completed_task(g, 35, "ILGI_GOSTERMEDI", None, attempt=1)
    # 2k) MEMNUN_OLMADI — kapanış kodu, hiçbir zaman aranmamalı
    lead(guest(), 100, "MEMNUN_OLMADI", notes="Kapanış kodu — tekrar aranmaz")
