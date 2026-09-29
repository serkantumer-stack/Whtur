"""Adım 1 — Veri Modeli (PostgreSQL / SQLAlchemy).

Tablolar:
    guests          Misafirler (ana müşteri kaydı)
    agents          Çağrı merkezi temsilcileri
    contacts        Temas kayıtları (ilk/son temas bu tablodan türetilir)
    result_codes    Sonuç kodları referans tablosu
    offers          Teklifler (hedef konaklama dönemi ile)
    reservations    Rezervasyonlar (satın alma kaydı)
    outbound_tasks  Outbound aranma görevleri (kural motoru çıktısı)
"""
from datetime import datetime

from sqlalchemy import (Column, Date, DateTime, ForeignKey, Integer,
                        Numeric, String, Text)
from sqlalchemy.orm import relationship

from .database import Base


class ResultCode(Base):
    """Sonuç kodları — referans tablo (Adım 2 kural tablosunun kaynağı)."""
    __tablename__ = "result_codes"

    code = Column(String(40), primary_key=True)  # FIYAT_YUKSEK, SATIN_ALDI, ...
    description = Column(String(200), nullable=False)
    category = Column(String(20), nullable=False)  # converted | non_converted
    is_final = Column(Integer, nullable=False, default=0)  # 1 = kapanış kodu


class Agent(Base):
    """Çağrı merkezi temsilcileri."""
    __tablename__ = "agents"

    agent_id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    team = Column(String(60))  # inbound | outbound | rezervasyon


class Guest(Base):
    """Misafirler — ana müşteri kaydı."""
    __tablename__ = "guests"

    guest_id = Column(Integer, primary_key=True)
    first_name = Column(String(80), nullable=False)
    last_name = Column(String(80), nullable=False)
    phone = Column(String(24), nullable=False, index=True)
    email = Column(String(160))
    city = Column(String(60))
    created_at = Column(DateTime, default=datetime.utcnow)

    contacts = relationship("Contact")
    offers = relationship("Offer")
    reservations = relationship("Reservation")
    tasks = relationship("OutboundTask")


class Contact(Base):
    """Temas kayıtları — 'İlk Temas' ve 'Son Temas' bu tablodan türetilir."""
    __tablename__ = "contacts"

    contact_id = Column(Integer, primary_key=True)
    guest_id = Column(Integer, ForeignKey("guests.guest_id"), nullable=False, index=True)
    agent_id = Column(Integer, ForeignKey("agents.agent_id"))
    contact_date = Column(Date, nullable=False, index=True)  # yıl/ay/gün döngüsü
    contact_type = Column(String(16), nullable=False)  # call_in | call_out | email
    result_code = Column(String(40), ForeignKey("result_codes.code"), nullable=False)
    notes = Column(Text)


class Offer(Base):
    """Müşteriye verilen teklifler — hedef konaklama dönemi ile."""
    __tablename__ = "offers"

    offer_id = Column(Integer, primary_key=True)
    guest_id = Column(Integer, ForeignKey("guests.guest_id"), nullable=False, index=True)
    contact_id = Column(Integer, ForeignKey("contacts.contact_id"))
    offer_date = Column(Date, nullable=False)
    target_period = Column(String(7), nullable=False)  # hedef konaklama dönemi 'YYYY-MM'
    hotel = Column(String(120))
    room_type = Column(String(60))
    board = Column(String(20))  # OB | BB | UP | AI
    price_amount = Column(Numeric(12, 2))
    currency = Column(String(3), default="EUR")
    status = Column(String(16), default="sent")  # sent | accepted | rejected | expired


class Reservation(Base):
    """Rezervasyonlar — satın alma kaydı."""
    __tablename__ = "reservations"

    reservation_id = Column(Integer, primary_key=True)
    guest_id = Column(Integer, ForeignKey("guests.guest_id"), nullable=False, index=True)
    offer_id = Column(Integer, ForeignKey("offers.offer_id"))
    booking_date = Column(Date, nullable=False, index=True)  # satın alma tarihi
    check_in = Column(Date, nullable=False)
    check_out = Column(Date, nullable=False)
    hotel = Column(String(120))
    total_amount = Column(Numeric(12, 2))
    currency = Column(String(3), default="EUR")
    status = Column(String(16), nullable=False, default="confirmed")
    # confirmed | completed | cancelled | no_show


class OutboundTask(Base):
    """Outbound aranma görevleri — kural motorunun ürettiği çağrı listesi kalemi."""
    __tablename__ = "outbound_tasks"

    task_id = Column(Integer, primary_key=True)
    guest_id = Column(Integer, ForeignKey("guests.guest_id"), nullable=False, index=True)
    segment = Column(String(16), nullable=False, index=True)  # converted | non_converted
    trigger_rule = Column(String(40), nullable=False)  # CONVERTED_ANNIVERSARY | RESULT_CODE_RETRY
    result_code = Column(String(40))  # lead: son temasın kodu; converted: SATIN_ALDI
    scheduled_date = Column(Date, nullable=False, index=True)  # tekrar aranma zamanı
    target_period = Column(String(7))  # hedef konaklama dönemi 'YYYY-MM'
    priority = Column(Integer, nullable=False, default=2)  # 1 Yüksek | 2 Orta | 3 Düşük
    attempt_no = Column(Integer, nullable=False, default=1)
    status = Column(String(16), nullable=False, default="pending", index=True)
    # pending | completed | cancelled
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    guest = relationship("Guest")
