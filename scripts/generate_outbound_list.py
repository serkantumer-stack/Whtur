#!/usr/bin/env python3
"""Adım 3 — Outbound Arama Listesi Üretici (Pandas CLI).

Kaynak tabloları (contacts / reservations / offers / outbound_tasks) okur,
rule_engine.py'deki kural tablosunu pandas ile uygular ve operasyonel
outbound arama listesini CSV olarak yazar.

Kullanım:
    python scripts/generate_outbound_list.py
    python scripts/generate_outbound_list.py --as-of 2026-09-29 \
        --segment non_converted --due-only --output liste.csv

Docker içinde:
    docker compose -f docker-compose.base44.yml exec app \
        python /app/scripts/generate_outbound_list.py --output /tmp/liste.csv

DB bağlantısı DATABASE_URL ortam değişkeninden okunur.
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.database import engine  # noqa: E402
from app.export import build_outbound_list  # noqa: E402


def main():
    parser = argparse.ArgumentParser(
        description="Outbound arama listesi üretici (Pandas)")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today(),
                        help="Referans tarih YYYY-MM-DD (varsayılan: bugün)")
    parser.add_argument("--segment", choices=["all", "converted", "non_converted"],
                        default="all")
    parser.add_argument("--due-only", action="store_true",
                        help="Sadece vadesi gelmiş görevler")
    parser.add_argument("--output", default=None,
                        help="CSV çıktı dosyası (varsayılan: outbound_list_YYYYMMDD.csv)")
    args = parser.parse_args()

    out = args.output or f"outbound_list_{args.as_of:%Y%m%d}.csv"
    df = build_outbound_list(engine, args.as_of, segment=args.segment,
                             due_only=args.due_only)
    df.to_csv(out, index=False, encoding="utf-8-sig")

    due = 0
    if not df.empty and "tekrar_aranma_tarihi" in df.columns:
        due = int((df["tekrar_aranma_tarihi"] <= args.as_of.isoformat()).sum())
    print(f"[OK] {len(df)} satır üretildi ({due} tanesi vadesi gelmiş). Çıktı: {out}")


if __name__ == "__main__":
    main()
