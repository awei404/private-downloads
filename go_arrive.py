"""LEGACY prototype — superseded by the bwi_monitor package (python3 -m bwi_monitor).

Poll GOFO TMS Departure & Arrival Management (goArrive) data.

Setup: in Chrome DevTools > Network, click the `pageList` request and copy the
`Authorization` and `auth-tag-x` request header values, then:

    export GOFO_AUTH='Bearer <token from DevTools>'
    export GOFO_TAG='unauthorized'
    python go_arrive.py
"""
import csv
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from anomaly import evaluate, on_time_rate

URL = "https://dms.gofoexpress.com/prod-api/dbu_tms/api/task/transportTaskArrival/pageList"
POLL_SECONDS = 60
EASTERN = ZoneInfo("America/New_York")
PAGE_SIZE = 100

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Authorization": os.environ["GOFO_AUTH"],
    "auth-tag-x": os.environ["GOFO_TAG"],
    "lang": "en",
    "User-Time-Zone": "America/New_York",
    "timeZone": "GMT-0400",
}

# Same filters as the page; fill lists to narrow results (e.g. vehicleStatusList=["3"]).
FILTERS = {
    "destinationTransitIdList": [],
    "licensePlateNoList": [],
    "originTransitIdList": [],
    "sealNo": "",
    "vehicleStatusList": [],
    "trailerNoList": [],
    "dispatchTypeList": [],
    "departTypeList": [],
    "modelIdList": [],
    "lineIdList": [],
    "supplierIdList": [],
    "transportTypeList": [],
}

FIELDS = [
    "id", "taskNo", "sortNum", "taskName", "licensePlateNo", "vehicleStatusStr", "transportTypeStr",
    "placeOfOrigin", "destination", "plannedDepartureTime", "actualDepartureTime",
    "plannedArrivalTime", "actualArrivalTime", "updateTime",
]


def fetch_page(session, page_num):
    body = {"data": FILTERS, "pageNum": page_num, "pageSize": PAGE_SIZE}
    r = session.post(URL, json=body, timeout=30)
    if r.status_code == 401:
        sys.exit("401 Unauthorized: token expired, copy fresh headers from DevTools")
    r.raise_for_status()
    payload = r.json()
    if payload.get("code") != 200:
        sys.exit(f"API error: {payload.get('code')} {payload.get('msg')}")
    return payload["data"]


def fetch_all(session):
    records, page = [], 1
    while True:
        data = fetch_page(session, page)
        batch = data.get("records") or []
        records.extend(batch)
        total = int(data.get("total") or 0)
        if not batch or len(records) >= total:
            return records
        page += 1
        time.sleep(0.5)  # be gentle with the server


def save_csv(records, path="go_arrive.csv"):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(records)


def main():
    session = requests.Session()
    session.headers.update(HEADERS)
    alerted = set()  # (segment key, status) already reported
    while True:
        records = fetch_all(session)
        save_csv(records)
        now = datetime.now(EASTERN).replace(tzinfo=None)
        results = evaluate(records, now)
        anomalies = [r for r in results if r["status"] in ("LATE", "OVERDUE", "ARRIVED_NOT_DEPARTED")]
        save_anomalies(anomalies)

        for r in anomalies:
            if (r["key"], r["status"]) in alerted:
                continue
            alerted.add((r["key"], r["status"]))
            print(f'  ⚠ {r["status"]:<20} {r["taskNo"]} {r["origin"]}->{r["destination"]} '
                  f'plate={r["licensePlateNo"]} planned={r["planned"]} '
                  f'actual={r["actualDeparture"] or "-"} delay={r["delayMinutes"]}min')

        today = now.strftime("%Y-%m-%d")
        rates = on_time_rate(results, today)
        summary = ", ".join(f"{o} {ok}/{n} ({ok / n:.0%})" for o, (ok, n) in sorted(rates.items()))
        print(f"[{now:%H:%M:%S}] {len(records)} segments, {len(anomalies)} anomalies | "
              f"on-time {today}: {summary or 'n/a'}")
        time.sleep(POLL_SECONDS)


def save_anomalies(rows, path="anomalies.csv"):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["status", "taskNo", "taskName", "licensePlateNo", "origin",
                                          "destination", "originalPlanned", "planned",
                                          "actualDeparture", "delayMinutes", "key"])
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["status"], r["planned"])))


if __name__ == "__main__":
    main()
