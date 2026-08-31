#!/usr/bin/env python3
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import watcher


def main() -> None:
    out_path = Path(sys.argv[1] if len(sys.argv) > 1 else ".state/monthly-130k-report.json")
    config = watcher.load_config()
    session = requests.Session()
    watcher.bootstrap(session)
    now = datetime.now(timezone.utc)

    report = {
        "generated_at_utc": now.isoformat(),
        "window_days": 30,
        "filters": [],
    }

    for spec in config.searches:
        listings = watcher.fetch_search(session, spec)
        recent = []
        for car in listings:
            cid = car.get("Id")
            ctx = watcher.fetch_listing_context(session, cid)
            adv = ctx.advertised_at.astimezone(timezone.utc) if ctx and ctx.advertised_at else None
            if adv:
                age_h = (now - adv).total_seconds() / 3600
                if 0 <= age_h <= 720:
                    recent.append({
                        "id": str(cid),
                        "title": " ".join(str(v) for v in [
                            car.get("Manufacturer"), car.get("Model"), car.get("Badge"), car.get("BadgeDetail")
                        ] if v),
                        "year": car.get("FormYear") or car.get("Year"),
                        "mileage": car.get("Mileage"),
                        "price_10k_krw": car.get("Price"),
                        "firstAdvertisedDateTime_utc": adv.isoformat(),
                        "reRegistered": ctx.re_registered if ctx else None,
                        "url": watcher.DETAIL_URL.format(car_id=cid),
                    })
            time.sleep(0.03)

        recent.sort(key=lambda x: x["firstAdvertisedDateTime_utc"], reverse=True)
        report["filters"].append({
            "key": spec.key,
            "label": spec.label,
            "current_count": len(listings),
            "last_30d_count": len(recent),
            "rows": recent,
        })

    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
