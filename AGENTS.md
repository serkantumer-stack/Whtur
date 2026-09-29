# AGENTS.md — Whtur Outbound CRM (Base44 geliştirme ortamı)

## Çalıştırma
- `docker compose -f docker-compose.base44.yml up -d` — web panel host **3000** → konteyner 8000 (uvicorn `--reload`, canlı yeniden yükleme; kaynak repo kökü `/app`'e bind-mount).
- Bağımlılıklar her konteyner açılışında `pip install -r backend/requirements.txt` ile kurulur (`pipcache` volume sayesinde 2. açılış hızlıdır). `requirements.txt` değişince: `docker compose -f docker-compose.base44.yml restart app`.
- Ortamda **harici secret yok**; PostgreSQL yerel compose servisidir (kimlikler compose `environment:` içinde).

## Mimari özet
- Kuralların TEK KAYNAĞI `backend/app/rule_engine.py` (`LEAD_RULES`, `CONVERTED_RULE`, karar ağaçları, `run_engine`). API, Pandas export ve seed hepsi buradan okur — kural değişikliği yalnızca bu dosyada yapılır.
- `backend/app/export.py` Pandas ile aynı kuralları uygular; hem `/api/export/csv` hem `scripts/generate_outbound_list.py` CLI bunu kullanır.
- `backend/static/` vanilla JS panel; FastAPI `/` altından servis edilir.

## Veri akışı
- Boş DB açılışta otomatik demo verisiyle dolar (`seed_data.py`, tarihler `date.today()`'ye göre relatif — senaryoların vade durumları her ortamda korunur).
- Açılışta ve `POST /api/engine/run`'da motor **pending** görevleri silip yeniden üretir (idempotent); **completed** görevler deneme sayacı için korunur.
- Veriyi sıfırlamak: `docker compose -f docker-compose.base44.yml down -v` (pgdata volume silinir).

## Doğrulama
- `curl localhost:3000/api/stats` → KPI dönüyorsa app + db ayakta.
- CSV CLI: `docker compose -f docker-compose.base44.yml exec app python /app/scripts/generate_outbound_list.py --output /tmp/liste.csv`.
- Panelden "Motoru Çalıştır" → toast'ta üretilen görev sayıları döner.
