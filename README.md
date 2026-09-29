# Whtur — Outbound Müşteri Geri Kazanım & Satış Algoritması

Otel grubu / çağrı merkezi outbound operasyonları için **kural tabanlı ve dinamik çağrı listesi motoru**. Geçmiş temas, teklif ve konaklama verisini analiz ederek *"hangi müşteri, ne zaman, hangi gerekçeyle aranacak"* sorusunu yanıtlar.

```
[Panel (statik UI)]  ⇄  [FastAPI + Kural Motoru]  ⇄  [PostgreSQL]
                              ↑
[Pandas CLI / CSV Export] ───┘   (kuralların tek kaynağı: app/rule_engine.py)
```

- **Backend:** Python / FastAPI / SQLAlchemy / Pandas
- **Veritabanı:** PostgreSQL 16 (docker compose)
- **Panel:** `http://localhost:3000` — KPI'lar, arama listesi, kural tablosu, veri modeli, CSV indirme

---

## Adım 1 — Veri Modeli ve Mimarisi

| Tablo | Açıklama | Anahtarlar |
|---|---|---|
| `guests` | Misafirler — ana müşteri kaydı | PK `guest_id` |
| `agents` | Çağrı merkezi temsilcileri | PK `agent_id` |
| `contacts` | Temas kayıtları; **İlk Temas / Son Temas** bu tablodan türetilir (`contact_date`, yıl/ay/gün döngüsü) | PK `contact_id`, FK `guest_id→guests`, `agent_id→agents`, `result_code→result_codes` |
| `result_codes` | Sonuç kodları referans tablosu (dönüşüm/kapanış kodları) | PK `code` |
| `offers` | Teklifler — `target_period ('YYYY-MM')` hedef konaklama dönemini taşır | PK `offer_id`, FK `guest_id→guests`, `contact_id→contacts` |
| `reservations` | Rezervasyonlar — satın alma kaydı (`booking_date` = satın alma, `check_in/check_out` = konaklama zamanı) | PK `reservation_id`, FK `guest_id→guests`, `offer_id→offers` |
| `outbound_tasks` | Kural motorunun çıktısı: `segment`, `trigger_rule`, `scheduled_date` (tekrar aranma), `target_period`, `priority`, `attempt_no`, `status` | PK `task_id`, FK `guest_id→guests` |

Kolon tipleri: `Integer` (PK/FK), `Date` (temas/rezervasyon/aranma tarihleri), `String` (kodlar, dönem `'YYYY-MM'`), `Numeric(12,2)` (tutarlar), `Text` (notlar). Canlı şema panelin **Veri Modeli** sekmesinde, API'de `GET /api/schema` ucunda yansıtılır (PK/FK rozetleriyle).

## Adım 2 — Outbound Algoritması ve Kural Motoru

Kuralların **tek kaynağı** `backend/app/rule_engine.py` içindeki `LEAD_RULES` ve `CONVERTED_RULE`; API, Pandas export ve seed hepsi buradan okur.

### Satın Alanlar (Converted) — Yıl Dönümü Stratejisi

1. Misafirin geçerli (confirmed/completed) **en yeni rezervasyonu** alınır.
2. Son temas, rezervasyon tarihinden sonraysa misafir Lead akışındadır → converted kuralı uygulanmaz.
3. Konaklama gelecekteyse (yaklaşan tatil) → görev oluşturulmaz.
4. Tamamlanmış yıl dönümü denemesi ≥ 2 ise → kapanmış hesap.
5. Aksi halde **görev oluşturulur**: `tekrar_aranma = booking_date + 11 ay`, `hedef_dönem = önceki konaklama ayının bir sonraki yıl karşılığı ('YYYY-MM')`, öncelik Yüksek.

> Örnek: 25.09.2025'te aranıp Ekim 2025 için rezervasyon yapan misafirin 2026'da aktivitesi yoksa → **Ağustos/Eylül 2026'da aranma görevi**, hedef dönem **2026-10**.

### Satın Almayanlar (Non-Converted / Lead) — Sonuç Kodu Kural Tablosu

| Sonuç Kodu | Tekrar Aranma | Öncelik | Max Deneme |
|---|---|---|---|
| `FIYAT_YUKSEK` | Son temastan **+30 gün** | Orta | 3 |
| `ERKEN_REZ_BEKLIYOR` | **Sonraki 1 Eylül** (erken rezervasyon dönemi) | Yüksek | 2 |
| `TARIH_UYGUN_DEGIL` | +60 gün | Orta | 2 |
| `DUSUNECEK` | +14 gün | Orta | 3 |
| `TEKRAR_ARANACAK` | +21 gün | Yüksek | 3 |
| `ULASILAMADI` | +7 gün | Yüksek | 5 |
| `ILGI_GOSTERMEDI` | +365 gün | Düşük | 1 |
| `PLANIM_YOK` | +180 gün | Düşük | 1 |
| `MEMNUN_OLMADI` | *(kapanış kodu — hiç aranmaz)* | — | 0 |

Döngü: görev için çağrı yapıldığında yeni bir `contacts` kaydı ve sonuç kodu oluşur; motorun bir sonraki çalışması **son temasa göre** yeniden planlar (dinamik döngü). Deneme limiti dolan lead'ler listeye düşmez.

Motor idempotenttir: bekleyen (pending) görevleri silip kaynak tablolardan yeniden üretir; tamamlanmış görevler deneme sayacı için korunur. Paneldeki **Motoru Çalıştır** düğmesi veya `POST /api/engine/run` ile tetiklenir (açılışta da otomatik çalışır).

## Adım 3 — Python/Pandas İşleme & CSV Export

Standalone CLI script:

```bash
# docker içinde (önerilen)
docker compose -f docker-compose.base44.yml exec app \
    python /app/scripts/generate_outbound_list.py --output /tmp/outbound_list.csv

# filtreler
... --as-of 2026-09-29 --segment non_converted --due-only
```

Script kaynak tabloları okur, kural tablosunu pandas ile uygular (sabit gecikmeler vektörel, 1 Eylül stratejisi tarih fonksiyonuyla), deneme limitlerini kontrol eder ve CSV yazar. Panelden **CSV İndir** düğmesi aynı motoru (`GET /api/export/csv`) çağırır.

**CSV kolonları:** `misafir_id, ad, soyad, telefon, email, segment, kural, sonuc_kodu, tekrar_aranma_tarihi, hedef_donem, oncelik, oncelik_adi, deneme_no, son_temas_tarihi, son_rezervasyon_tarihi, notlar` — öncelik ve tarihe göre sıralı, Excel uyumlu (UTF-8 BOM).

## Google Sheets Bağlantısı

Mevcut müşteri verisi Google Sheets'te duruyorsa paneldeki **📥 Sheets İçe Aktar** düğmesiyle bağlanılır:

1. Google'da sheet'i **Dosya → Web'de yayınla** (veya "Herkesi görüntüleyebilir" paylaşım linki) ile erişilebilir yapın.
2. Panelden **Sheets İçe Aktar** → linki yapıştırın → **İçe Aktar**.
3. İçe aktarma **tüm mevcut veriyi siler** (demo dahil), sheet'i ana veri kaynağı yapar ve kural motorunu otomatik çalıştırır.

Kolon eşlemesi esnektir (Türkçe/İngilizce başlıklar otomatik tanınır): `Ad/Soyad` veya tek `Ad Soyad` kolonu, `Telefon`, `E-posta`, `İlk Temas`, `Son Temas`, `Sonuç Kodu`, `Rezervasyon Tarihi`, `Konaklama Tarihi`, `Çıkış Tarihi`, `Tutar`, `Durum`. Sonuç kodları normalize edilir ("Fiyat Yüksek", "Meşgul", "Satın Aldı" vb. → kanonik kodlar); tanınmayan kodlar içe alınır ama kural tablosunda karşılığı yoksa görev üretmez ve panelde raporlanır. API: `POST /api/import/sheet {"url": "..."}`.

## Çalıştırma

```bash
docker compose -f docker-compose.base44.yml up -d
# panel: http://localhost:3000
```

Boş veritabanı açılışta **bugüne göre tarihli demo verisiyle** otomatik doldurulur (yıldönümü vadesi gelenler, fiyat itirazı +2. deneme, erken rezervasyon bekleyenler, deneme limiti dolanlar vb. ~30 misafir).

| Uç | İşlev |
|---|---|
| `GET /api/stats` | KPI'lar (segment sayıları, bekleyen/vadesi gelen görev) |
| `GET /api/tasks?segment=&due=` | Arama listesi (filtreli) |
| `POST /api/engine/run` | Kural motorunu yeniden çalıştır |
| `GET /api/rules` | Kural tablosu + karar ağaçları |
| `GET /api/schema` | Veri modeli (tablo/kolon/PK/FK) |
| `GET /api/export/csv` | Pandas üretimli arama listesi CSV |
