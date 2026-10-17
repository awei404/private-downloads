"""Read-only GOFO TMS client for transportTaskArrival/pageList, scoped to BWI."""
import os
import time

import requests

from . import config

URL = "https://dms.gofoexpress.com/prod-api/dbu_tms/api/task/transportTaskArrival/pageList"
# Transport-task list: the authoritative task status (taskStatus "6" = Cancelled / 已作废). Read-only.
TASK_URL = "https://dms.gofoexpress.com/prod-api/dbu_tms/api/task/transportTask/pageList"
CANCELLED = "6"
PAGE_SIZE = 100


class AuthError(RuntimeError):
    """Token expired or rejected; needs fresh GOFO_AUTH."""


class FeedError(RuntimeError):
    """Network/API failure after retries."""


class TmsClient:
    def __init__(self, auth=None, tag=None):
        auth = auth or os.environ.get("GOFO_AUTH")
        if not auth:
            raise AuthError("GOFO_AUTH is not set")
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Authorization": auth,
            "auth-tag-x": tag or os.environ.get("GOFO_TAG", "unauthorized"),
            "lang": "en",
            "User-Time-Zone": "America/New_York",
        })

    def _page(self, filters, page_num, page_size=PAGE_SIZE, url=URL):
        body = {"data": filters, "pageNum": page_num, "pageSize": page_size}
        last = None
        for attempt in range(3):
            try:
                r = self.session.post(url, json=body, timeout=30)
                if r.status_code == 401:
                    raise AuthError("401 Unauthorized: token expired")
                r.raise_for_status()
                payload = r.json()
                if payload.get("code") == 401:
                    raise AuthError(f"API 401: {payload.get('msg')}")
                if payload.get("code") != 200:
                    raise FeedError(f"API error {payload.get('code')}: {payload.get('msg')}")
                return payload["data"]
            except (requests.RequestException, ValueError) as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise FeedError(f"request failed after retries: {type(last).__name__}: {last}")

    def _all(self, filters):
        records, page = [], 1
        while True:
            data = self._page(filters, page)
            batch = data.get("records") or []
            records.extend(batch)
            if not batch or len(records) >= int(data.get("total") or 0):
                return records
            page += 1
            time.sleep(0.3)

    def fetch_bwi(self):
        """All segments leaving or arriving at BWI, de-duplicated by segment id."""
        out = {}
        for key in ("originTransitIdList", "destinationTransitIdList"):
            for r in self._all({key: [config.SITE_TRANSIT_ID]}):
                out[r["id"]] = r
        return list(out.values())

    def task_status(self, task_no):
        """(taskStatus, taskStatusStr) from the transport-task list, or None if TMS does not return the task."""
        records = self._page({"taskNo": task_no}, 1, page_size=10, url=TASK_URL).get("records") or []
        rec = next((r for r in records if r.get("taskNo") == task_no), None)
        return (str(rec.get("taskStatus")), rec.get("taskStatusStr")) if rec else None

    def verify(self, store, seg_id, task_no, now):
        """Validity of a segment the arrival list no longer returns: VOIDED only when TMS says taskStatus 6
        (Cancelled / 已作废); otherwise UNVERIFIED (uncertain), never voided by inference."""
        status = self.task_status(task_no)
        if status and status[0] == CANCELLED:
            store.mark_validity(seg_id, now, "VOIDED", f"TMS taskStatus {status[0]} {status[1]}")
            return "VOIDED"
        detail = f"missing from arrival list; TMS taskStatus {status[0]} {status[1]}" if status else \
            "missing from arrival list and transport-task list"
        store.mark_validity(seg_id, now, "UNVERIFIED", detail)
        return "UNVERIFIED"

    def reconcile_removed(self, store, poll_started, now, lookback_days=4, max_checks=25):
        """After a successful full poll: segments TMS did not return are re-checked by task number. Found again ->
        refreshed (a pagination gap never voids a task). Still absent -> verify() by task status.
        Returns (voided ids, unverified ids, refreshed ids)."""
        from datetime import timedelta
        voided, unverified, refreshed, by_task = [], [], [], {}
        for row in store.missing_since(poll_started, now - timedelta(days=lookback_days)):
            by_task.setdefault(row["task_no"], []).append(row["id"])
        for task_no, ids in list(by_task.items())[:max_checks]:
            found = {r["id"]: r for r in self.fetch_task(task_no)}
            for sid in ids:
                if sid in found:
                    store.upsert_segments([found[sid]], now)
                    refreshed.append(sid)
                elif self.verify(store, sid, task_no, now) == "VOIDED":
                    voided.append(sid)
                else:
                    unverified.append(sid)
        return voided, unverified, refreshed

    def fetch_task(self, task_no):
        return self._page({"taskNo": task_no}, 1, page_size=50).get("records") or []
