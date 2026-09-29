"""Google Sheets içe aktarma — herkese açık link üzerinden tam senkronizasyon.

Sheet, verinin ANA KAYNAĞIDIR: her içe aktarma uygulama tablolarını
(outbound_tasks, contacts, offers, reservations, guests) temizler, sheet
satırlarını yeniden yazar ve kural motorunu çalıştırır.

Erişim: sheet'in "Dosya → Web'de yayınla" linki veya "Herkesi görüntüleyebilir"
paylaşım linki (motor, linkten Google'ın CSV export ucuyla veriyi çeker).
"""
import re
import urllib.request
from datetime import date, timedelta
from io import StringIO

import pandas as pd

from .database import SessionLocal
from .models import Contact, Guest, Offer, OutboundTask, Reservation, ResultCode
from .rule_engine import LEAD_RULES, run_engine

# ---------------------------------------------------------------------------
# Google Sheets linki -> CSV indirme URL'i
# ---------------------------------------------------------------------------
_SHEET_ID = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")
_PUB_ID = re.compile(r"/spreadsheets/d/e/([a-zA-Z0-9_-]+)")
_GID = re.compile(r"[?&#]gid=([0-9]+)")

_UTILITY_CODES = {
    "ILK_TEMAS": ("İlk temas kaydı (Sheets içe aktarma)", "non_converted", 1),
    "BILINMIYOR": ("Sonuç kodu belirtilmemiş (Sheets içe aktarma)", "non_converted", 1),
}
_KNOWN_CODES = set(LEAD_RULES) | {"SATIN_ALDI", "MEMNUN_OLMADI"} | set(_UTILITY_CODES)


def to_csv_url(url: str) -> str:
    url = url.strip()
    pub = _PUB_ID.search(url)
    if pub:
        return f"https://docs.google.com/spreadsheets/d/e/{pub.group(1)}/pub?output=csv"
    m = _SHEET_ID.search(url)
    if not m:
        raise ValueError(
            "Geçersiz Google Sheets linki. Örnek: "
            "https://docs.google.com/spreadsheets/d/SAYFA_ID/edit"
        )
    out = f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv"
    g = _GID.search(url)
    if g:
        out += f"&gid={g.group(1)}"
    return out


def fetch_sheet_df(url: str) -> pd.DataFrame:
    csv_url = to_csv_url(url)
    req = urllib.request.Request(csv_url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        raise ValueError(
            f"Sheet'e erişilemedi ({e}). Linkin herkese açık olduğundan emin olun."
        )
    if data.lstrip()[:100].lower().startswith(("<!doctype", "<html")):
        raise ValueError(
            "Sheet herkese açık değil. Google'da 'Dosya → Web'de yayınla' ile "
            "yayınlayın ya da 'Herkesi görüntüleyebilir' paylaşım linki verin."
        )
    df = pd.read_csv(StringIO(data))
    if df.empty:
        raise ValueError("Sheet boş görünüyor.")
    return df


# ---------------------------------------------------------------------------
# Kolon eşleme (Türkçe/İngilizce başlık varyantları)
# ---------------------------------------------------------------------------
def _fold(s) -> str:
    s = str(s).strip().translate(
        str.maketrans("ıİişşğĞüÜöÖçÇ", "iiissgguuoocc")).lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


COLUMN_ALIASES = {
    "ad_soyad": ["ad_soyad", "adsoyad", "isim_soyisim", "isim_soyad", "musteri_adi",
                 "musteri_ad_soyad", "full_name", "name", "musteri"],
    "ad": ["ad", "isim", "adi", "first_name", "first"],
    "soyad": ["soyad", "soyisim", "last_name", "last", "surname"],
    "telefon": ["telefon", "tel", "telefon_no", "telefon_numarasi", "cep", "cep_telefonu",
                "gsm", "phone", "phone_number"],
    "email": ["email", "e_posta", "eposta", "e_mail", "mail"],
    "sehir": ["sehir", "city", "il"],
    "ilk_temas": ["ilk_temas", "ilk_temas_tarihi", "ilk_gorusme", "ilk_gorusme_tarihi",
                  "first_contact", "first_contact_date", "ilk_aranma", "ilk_aranma_tarihi"],
    "son_temas": ["son_temas", "son_temas_tarihi", "son_gorusme", "son_gorusme_tarihi",
                  "last_contact", "last_contact_date", "son_aranma", "son_aranma_tarihi",
                  "temas_tarihi"],
    "sonuc_kodu": ["sonuc_kodu", "sonuc", "sonuc_kod", "result_code", "result",
                   "arama_sonucu", "cagri_sonucu", "sonuc_aciklamasi"],
    "rezervasyon_tarihi": ["rezervasyon_tarihi", "rezervasyon", "booking_date", "satis_tarihi",
                           "satin_alma_tarihi", "alinan_tarih", "rez_tarihi"],
    "konaklama_baslangic": ["konaklama_tarihi", "konaklama_baslangic", "konaklama_baslangici",
                            "giris_tarihi", "giris", "check_in", "konaklama", "varis_tarihi"],
    "konaklama_bitis": ["konaklama_bitis", "cikis_tarihi", "cikis", "check_out", "donus_tarihi"],
    "tutar": ["tutar", "fiyat", "toplam", "toplam_tutar", "total", "total_amount", "amount",
              "satis_tutari", "ucret"],
    "durum": ["durum", "status", "rezervasyon_durumu", "rezervasyon_durum", "statu"],
}


def _map_columns(df: pd.DataFrame) -> dict:
    norm = {_fold(c): c for c in df.columns}
    colmap = {}
    for canon, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in norm and norm[alias] not in colmap.values():
                colmap[canon] = norm[alias]
                break
    return colmap


# ---------------------------------------------------------------------------
# Değer ayrıştırma
# ---------------------------------------------------------------------------
def _parse_date(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    # Excel serisi (Google sayı formatlı tarih kolonları)
    if isinstance(v, (int, float)) and 20000 <= float(v) <= 60000:
        return date(1899, 12, 30) + timedelta(days=int(v))
    s = str(v).strip()
    if not s:
        return None
    ts = pd.to_datetime(s, dayfirst=True, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.date()


def _parse_amount(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = re.sub(r"[^0-9,.\-]", "", str(v))
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _norm_code(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip().upper().translate(
        str.maketrans("İIıŞşĞğÜüÖöÇç", "IIISSGGUUOOCC"))
    s = re.sub(r"[^A-Z0-9]+", "_", s).strip("_")
    return s or None


# Yaygın yazım varyantları -> kanonik kodlar
CODE_FIXES = {
    "SATTI": "SATIN_ALDI", "SATIN_ALDIM": "SATIN_ALDI",
    "REZERVASYON_YAPTI": "SATIN_ALDI", "REZERVASYON": "SATIN_ALDI",
    "ONAYLANDI": "SATIN_ALDI",
    "PAHALI": "FIYAT_YUKSEK", "FIYAT": "FIYAT_YUKSEK", "FIYAT_YUKSEKDI": "FIYAT_YUKSEK",
    "ERKEN_REZERVASYON_BEKLIYOR": "ERKEN_REZ_BEKLIYOR",
    "ERKEN_REZ_BEKLIYORUM": "ERKEN_REZ_BEKLIYOR", "ERKEN_REZERVASYON": "ERKEN_REZ_BEKLIYOR",
    "DUSUNECEGIM": "DUSUNECEK", "DUSUNUYOR": "DUSUNECEK", "DUSUNECEK_DER": "DUSUNECEK",
    "ILGILENMIYOR": "ILGI_GOSTERMEDI", "ILGI_YOK": "ILGI_GOSTERMEDI",
    "ULASMADI": "ULASILAMADI", "MESGUL": "ULASILAMADI",
    "TEKRAR_ARAYIN": "TEKRAR_ARANACAK", "SONRA_ARAYIN": "TEKRAR_ARANACAK",
    "TARIH_YOK": "TARIH_UYGUN_DEGIL", "TARIHLER_UYGUN_DEGIL": "TARIH_UYGUN_DEGIL",
}


# ---------------------------------------------------------------------------
# İçe aktarma (tam senkronizasyon)
# ---------------------------------------------------------------------------
def import_sheet(url: str) -> dict:
    df = fetch_sheet_df(url)
    return import_dataframe(df)


def import_dataframe(df: pd.DataFrame) -> dict:
    colmap = _map_columns(df)
    if not any(colmap.get(k) for k in ("ad", "soyad", "ad_soyad", "telefon", "email")):
        raise ValueError(
            "Sheet'te misafir kolonları tanınmadı. Beklenen başlıklar: "
            "Ad, Soyad (veya 'Ad Soyad'), Telefon, E-posta"
        )
    if not any(colmap.get(k) for k in ("ilk_temas", "son_temas", "sonuc_kodu",
                                       "rezervasyon_tarihi", "konaklama_baslangic")):
        raise ValueError(
            "Sheet'te tarih/sonuç kolonları tanınmadı. Beklenen başlıklar: "
            "İlk Temas, Son Temas, Sonuç Kodu, Rezervasyon Tarihi, Konaklama Tarihi"
        )

    db = SessionLocal()
    try:
        # Sheet ana kaynak: eski veriyi temizle (FK sırasına dikkat)
        for model in (OutboundTask, Offer, Contact, Reservation, Guest):
            db.query(model).delete(synchronize_session=False)
        db.commit()

        for code, (desc, cat, final) in _UTILITY_CODES.items():
            if not db.get(ResultCode, code):
                db.add(ResultCode(code=code, description=desc, category=cat,
                                  is_final=final))
        db.commit()

        stats = {"rows": len(df), "guests": 0, "contacts": 0,
                 "reservations": 0, "skipped": 0, "unknown_codes": []}
        used_codes = set()

        for _, row in df.iterrows():
            def val(canon):
                orig = colmap.get(canon)
                if not orig:
                    return None
                v = row[orig]
                if v is None or (isinstance(v, float) and pd.isna(v)):
                    return None
                if isinstance(v, str) and not v.strip():
                    return None
                return v

            # --- Misafir
            ad = soyad = None
            if colmap.get("ad_soyad"):
                parts = str(val("ad_soyad")).strip().split(None, 1)
                ad = parts[0] if parts else None
                soyad = parts[1] if len(parts) > 1 else "-"
            if colmap.get("ad") and val("ad"):
                ad = str(val("ad")).strip()
            if colmap.get("soyad") and val("soyad"):
                soyad = str(val("soyad")).strip()
            phone = str(val("telefon")).strip() if val("telefon") else None
            email = str(val("email")).strip() if val("email") else None
            if not ad and not phone:
                stats["skipped"] += 1
                continue

            g = Guest(first_name=ad or "-", last_name=soyad or "-", phone=phone or "-",
                      email=email,
                      city=str(val("sehir")).strip() if val("sehir") else None)
            db.add(g)
            db.flush()
            stats["guests"] += 1

            # --- Tarihler / sonuç kodu
            ilkt = _parse_date(val("ilk_temas"))
            sont = _parse_date(val("son_temas"))
            booking = _parse_date(val("rezervasyon_tarihi"))
            checkin = _parse_date(val("konaklama_baslangic"))
            checkout = _parse_date(val("konaklama_bitis"))
            code = _norm_code(val("sonuc_kodu"))
            if code:
                code = CODE_FIXES.get(code, code)

            # --- Temas kayıtları
            new_contacts = set()
            if booking or checkin:  # satın alma satırı
                sale_day = (ilkt if (ilkt and ilkt <= (booking or checkin))
                            else (booking or checkin) - timedelta(days=1))
                new_contacts.add((sale_day, "SATIN_ALDI"))
                if sont and (not booking or sont > booking):
                    new_contacts.add((sont, code or "BILINMIYOR"))
            else:
                cdate = sont or ilkt
                if cdate:
                    if ilkt and ilkt != cdate:
                        new_contacts.add((ilkt, "ILK_TEMAS"))
                    new_contacts.add((cdate, code or "BILINMIYOR"))

            for cdate, ccode in sorted(new_contacts):
                used_codes.add(ccode)
                if not db.get(ResultCode, ccode):
                    db.add(ResultCode(code=ccode,
                                      description="(Sheets içe aktarma) tanımsız kod",
                                      category="non_converted", is_final=0))
                    db.commit()
                db.add(Contact(guest_id=g.guest_id, contact_date=cdate,
                               contact_type=("call_in" if ccode == "SATIN_ALDI"
                                             else "call_out"),
                               result_code=ccode, notes="Sheets içe aktarma"))
                stats["contacts"] += 1

            # --- Rezervasyon
            if booking or checkin:
                checkin = checkin or booking
                booking = booking or checkin - timedelta(days=30)
                checkout = checkout or (checkin + timedelta(days=7))
                durum = _fold(val("durum") or "")
                status = "confirmed"
                if "iptal" in durum:
                    status = "cancelled"
                elif "gelmedi" in durum or "no_show" in durum:
                    status = "no_show"
                elif checkin <= date.today():
                    status = "completed"
                db.add(Reservation(guest_id=g.guest_id, booking_date=booking,
                                   check_in=checkin, check_out=checkout,
                                   total_amount=_parse_amount(val("tutar")),
                                   currency="EUR", status=status))
                stats["reservations"] += 1

        db.commit()
        stats["engine"] = run_engine(db)
        stats["unknown_codes"] = sorted(used_codes - _KNOWN_CODES)
        return stats
    finally:
        db.close()
