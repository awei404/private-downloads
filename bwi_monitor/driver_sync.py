"""Bounded, read-only TMS task lookups for local driver profiles.

No transport records, scores, alerts or notifications are written by this module.
Names alone, vehicle plates and TMS operator names never identify a driver.
"""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import unicodedata

from . import departures, driver_scores, rules, tms
from .shift import reporting_date


PROFILE_SOURCE = "TMS司机资料"
# TMS regular-bus detail page uses this GET with the exact task id and this
# header. The list response can omit driver fields even when they are registered.
TASK_DRIVER_DETAIL_URL = "https://dms.gofoexpress.com/prod-api/dbu_tms/api/task/transportTask/regularBus/info"
CANCELLED_SOURCE = "TMS任务已取消"
MISMATCH_SOURCE = "TMS任务范围不符"
MISSING_SOURCE = "TMS任务资料未匹配"
MAX_BATCH = 120
MAX_WORKERS = 3
CACHE_SECONDS = 600
HISTORY_CACHE_SECONDS = 86400
CONTINUE_SECONDS = 15


def utcnow():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.utcoffset() is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _text(value, limit=200):
    return value.strip()[:limit] if isinstance(value, str) else ""


def normalized_phone(value):
    """Return a complete E.164 number, or None; never match on a suffix.

    Unprefixed 10-digit numbers are US/Canada numbers. Other countries require
    an explicit +country prefix; extensions and incomplete numbers are rejected.
    """
    value = unicodedata.normalize("NFKC", _text(value, 80))
    if not value or not re.fullmatch(r"\+?[0-9 ()\-.]+", value):
        return None
    digits = re.sub(r"[^0-9]", "", value)
    if len(set(digits)) < 2:
        return None
    if len(digits) == 10 and not value.startswith("+"):
        digits = "1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        if digits[1] not in "23456789" or digits[4] not in "23456789":
            return None
        return "+" + digits
    if value.startswith("+") and re.fullmatch(r"[2-9][0-9]{7,14}", digits):
        return "+" + digits
    return None


def driver_key(name, phone):
    """A deterministic key requires both the normalized name and full phone."""
    name = " ".join(unicodedata.normalize("NFKC", _text(name)).casefold().split())
    number = normalized_phone(phone)
    if not name or not any(c.isalnum() for c in name) or not number:
        return None
    return "tms:" + hashlib.sha256((name + "\0" + number).encode("utf-8")).hexdigest()


def _business_day(now):
    return reporting_date(now)


def _first_departure_matches(record, destination, *, name_key, points=None):
    """Compare BWI's first outbound leg, not a round trip's final destination."""
    if not isinstance(record, dict) or record.get("placeOfOrigin") != "BWI.H":
        return False
    if points is not None:
        if not isinstance(points, list):
            return False
        first = [p for p in points if isinstance(p, dict) and p.get("sortNum") in (1, "1")]
        second = [p for p in points if isinstance(p, dict) and p.get("sortNum") in (2, "2")]
        return (len(first) == len(second) == 1 and first[0].get("meridianStopPoint") == "BWI.H"
                and second[0].get("meridianStopPoint") == destination)
    name = record.get(name_key)
    if isinstance(name, str) and name.strip():
        stations = [part.strip() for part in name.split("(", 1)[0].split("-")]
        return len(stations) >= 2 and stations[:2] == ["BWI.H", destination]
    return record.get("destination") == destination


class DriverSync:
    """One background batch at a time, with independent SQLite/client connections.

    ``start`` returns immediately. ``start_periodic`` starts an optional daemon
    scheduler; successful backlog batches continue after 15 seconds, otherwise
    the normal interval is used. ``stop`` prevents further lookups and writes;
    an already running HTTP request can finish its client's bounded timeout.
    """

    def __init__(self, db_path, auth_file=None, client_factory=None):
        self.db_path = Path(db_path).expanduser().resolve()
        self.auth_file = Path(auth_file) if auth_file is not None else Path(__file__).resolve().parents[1] / "secrets/gofo_auth"
        self.client_factory = client_factory or tms.TmsClient
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._done = threading.Event()
        self._done.set()
        self._worker = None
        self._periodic = None
        self._retry_after = {}
        self._state = {"running": False, "last_success_et": None, "error": None,
                       "profiles_updated": 0, "remaining": 0}

    def status(self):
        with self._lock:
            return dict(self._state)

    def start(self):
        with self._lock:
            if self._state["running"] or self._stop.is_set():
                return dict(self._state)
            self._state.update(running=True, profiles_updated=0)
            self._done.clear()
            self._wake.set()  # A manual refresh wakes an idle periodic scheduler.
            self._worker = threading.Thread(target=self._run, name="bwi-driver-sync", daemon=True)
            self._worker.start()
            return dict(self._state)

    def start_periodic(self, interval=600):
        if not isinstance(interval, (int, float)) or interval <= 0:
            raise ValueError("interval must be positive")
        with self._lock:
            if self._stop.is_set() or (self._periodic and self._periodic.is_alive()):
                return dict(self._state)
            self._periodic = threading.Thread(target=self._schedule, args=(interval,),
                                              name="bwi-driver-sync-periodic", daemon=True)
            self._periodic.start()
            return dict(self._state)

    def stop(self):
        # A commit holds this same lock. When stop returns, any earlier commit
        # has finished and a later writer must observe the stop flag.
        with self._lock:
            self._stop.set()
            self._wake.set()
            return dict(self._state)

    def _schedule(self, interval):
        while not self._stop.is_set():
            self.start()
            while not self._done.wait(0.25):
                if self._stop.is_set():
                    return
            with self._lock:
                if self._stop.is_set():
                    return
                state = dict(self._state)
                self._wake.clear()
            delay = CONTINUE_SECONDS if state["remaining"] and not state["error"] else interval
            self._wake.wait(delay)

    def _connect(self):
        # mode=rw deliberately refuses to create a database at a mistaken path.
        db = sqlite3.connect(self.db_path.as_uri() + "?mode=rw", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def _candidates(self, db, now, *, include_cooldown=False):
        today = _business_day(now)
        groups = defaultdict(list)
        for row in db.execute("SELECT task_no,data,validity FROM segments"):
            try:
                seg = json.loads(row["data"])
            except (ValueError, TypeError):
                continue
            if not isinstance(seg, dict) or not isinstance(row["task_no"], str):
                continue
            if seg.get("taskNo") != row["task_no"]:
                continue
            if row["validity"]:
                seg["_validity"] = row["validity"]
            groups[row["task_no"]].append(seg)
        profiles = {row["task_no"]: dict(row) for row in db.execute(
            "SELECT task_no,source,updated_at FROM driver_task_profiles")}
        candidates = []
        for task_no, segments in groups.items():
            if not re.fullmatch(r"MT[0-9]+", task_no):
                continue
            valid = [seg for seg in segments if driver_scores.in_task_scope(seg, segments)]
            if not valid or len({seg["destination"] for seg in valid}) != 1:
                continue
            days = set()
            for seg in valid:
                try:
                    matched = departures.match(seg)
                    day = matched.get("op_date") if matched else None
                except (ValueError, TypeError, OverflowError, rules.BadTime):
                    day = None
                days.add(day)
            if None in days or len(days) != 1:
                continue
            day = next(iter(days))
            if not today - timedelta(days=29) <= day <= today + timedelta(days=2):
                continue
            profile = profiles.get(task_no)
            updated = _timestamp(profile.get("updated_at")) if profile else None
            if updated and updated <= now:
                if day < today - timedelta(days=2) and profile["source"] in (PROFILE_SOURCE, CANCELLED_SOURCE):
                    if now - updated < timedelta(seconds=HISTORY_CACHE_SECONDS):
                        continue
                if now - updated < timedelta(seconds=CACHE_SECONDS):
                    continue
            retry = self._retry_after.get(task_no)
            if not include_cooldown and retry and retry > now:
                continue
            candidates.append({"task_no": task_no, "destination": valid[0]["destination"], "day": day,
                               "profile_updated_at": profile.get("updated_at") if profile else None})
        # Newer operational days first; task number makes batches deterministic.
        candidates.sort(key=lambda item: (item["day"], item["task_no"]), reverse=True)
        return candidates

    def _fetch(self, candidate, auth):
        if self._stop.is_set():
            return None
        client = self.client_factory(auth=auth)
        try:
            data = client._page({"taskNo": candidate["task_no"]}, 1, page_size=10, url=tms.TASK_URL)
            records = data.get("records") if isinstance(data, dict) else None
            if not isinstance(records, list):
                raise tms.FeedError("invalid task response")
            matches = [r for r in records if isinstance(r, dict) and r.get("taskNo") == candidate["task_no"]]
            # Multiple exact records are not resolved by choosing one arbitrarily.
            if len(matches) != 1:
                return self._empty_profile(candidate["task_no"], MISSING_SOURCE)
            remote = matches[0]
            if remote.get("taskStatus") in (6, "6"):
                return self._empty_profile(candidate["task_no"], CANCELLED_SOURCE)
            if not _first_departure_matches(remote, candidate["destination"], name_key="taskName"):
                return self._empty_profile(candidate["task_no"], MISMATCH_SOURCE)
            task_id = remote.get("id")
            if isinstance(task_id, bool) or not re.fullmatch(r"[1-9][0-9]*", str(task_id)):
                raise tms.FeedError("invalid task id")
            if self._stop.is_set():
                return None
            response = client.session.get(TASK_DRIVER_DETAIL_URL, params={"id": task_id},
                                          headers={"X-Need-Decrypt": "true"}, timeout=30)
            if response.status_code == 401:
                raise tms.AuthError("driver detail authorization rejected")
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("code") != 200:
                raise tms.FeedError("driver detail request failed")
            detail = payload.get("data")
            if not isinstance(detail, dict) or str(detail.get("id")) != str(task_id):
                raise tms.FeedError("driver detail identity mismatch")
            if detail.get("taskStatus") in (6, "6"):
                return self._empty_profile(candidate["task_no"], CANCELLED_SOURCE)
            line = detail.get("lineVO") or {}
            if not _first_departure_matches(line, candidate["destination"], name_key="lineName",
                                            points=detail.get("taskLinePointVOList")):
                return self._empty_profile(candidate["task_no"], MISMATCH_SOURCE)
            # Only the task's registered driver counts. vehicleVO describes the
            # vehicle's current default driver, which can differ on old tasks.
            name, phone = _text(detail.get("mainDriver")), _text(detail.get("mainDriverTelephone"), 80)
            return {"task_no": candidate["task_no"], "driver_key": driver_key(name, phone),
                    "driver_name": name, "driver_phone": phone,
                    "supplier_name": _text(remote.get("supplierName")),
                    "vehicle_plate": _text(remote.get("licensePlateNo"), 80), "source": PROFILE_SOURCE}
        finally:
            session = getattr(client, "session", None)
            if session is not None:
                session.close()

    @staticmethod
    def _empty_profile(task_no, source):
        return {"task_no": task_no, "driver_key": None, "driver_name": "", "driver_phone": "",
                "supplier_name": "", "vehicle_plate": "", "source": source}

    def _write(self, db, profile, candidate, now):
        if self._stop.is_set():
            return False
        try:
            db.execute("BEGIN IMMEDIATE")
            # BEGIN may have waited for another SQLite writer while stop() ran.
            if self._stop.is_set():
                return False
            # A live TMS poll may have changed the task's scope while the lookup
            # was in flight. Recheck before associating any driver information.
            segments = []
            for row in db.execute("SELECT data,validity FROM segments WHERE task_no=?", (candidate["task_no"],)):
                seg = json.loads(row["data"])
                if not isinstance(seg, dict) or seg.get("taskNo") != candidate["task_no"]:
                    continue
                if row["validity"]:
                    seg["_validity"] = row["validity"]
                segments.append(seg)
            valid = [seg for seg in segments if driver_scores.in_task_scope(seg, segments)]
            if not valid or {seg["destination"] for seg in valid} != {candidate["destination"]}:
                return False
            current = db.execute("SELECT updated_at FROM driver_task_profiles WHERE task_no=?",
                                 (candidate["task_no"],)).fetchone()
            # Do not let a slower independent sync overwrite a newer profile.
            if (current["updated_at"] if current else None) != candidate["profile_updated_at"]:
                return False
            with self._lock:
                if self._stop.is_set():
                    return False
                db.execute(
                    "INSERT INTO driver_task_profiles(task_no,driver_key,driver_name,driver_phone,supplier_name,"
                    "vehicle_plate,source,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(task_no) DO UPDATE SET "
                    "driver_key=excluded.driver_key,driver_name=excluded.driver_name,driver_phone=excluded.driver_phone,"
                    "supplier_name=excluded.supplier_name,vehicle_plate=excluded.vehicle_plate,source=excluded.source,"
                    "updated_at=excluded.updated_at",
                    (profile["task_no"], profile["driver_key"], profile["driver_name"], profile["driver_phone"],
                     profile["supplier_name"], profile["vehicle_plate"], profile["source"], _iso(now)))
                db.commit()
            return True
        finally:
            # Includes stop/scope/version early returns and any write failure.
            if db.in_transaction:
                db.rollback()

    def _run(self):
        db, updated_count, failed = None, 0, False
        try:
            db = self._connect()
            candidates = self._candidates(db, utcnow())
            with self._lock:
                self._state["remaining"] = len(self._candidates(db, utcnow(), include_cooldown=True))
                if not self._state["remaining"]:
                    self._state["error"] = None
            if not candidates:
                return
            try:
                auth = self.auth_file.expanduser().read_text(encoding="utf-8").strip()
                if not auth:
                    raise ValueError()
            except (OSError, UnicodeError, ValueError):
                with self._lock:
                    self._state["error"] = "无法读取本地 TMS 凭证，请更新凭证后重试。"
                return
            with ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="bwi-driver-lookup") as pool:
                futures = {pool.submit(self._fetch, candidate, auth): candidate for candidate in candidates[:MAX_BATCH]}
                for future in as_completed(futures):
                    candidate = futures[future]
                    if self._stop.is_set():
                        for pending in futures:
                            pending.cancel()
                        break
                    try:
                        profile = future.result()
                        if profile is not None and self._write(db, profile, candidate, utcnow()):
                            updated_count += 1
                            self._retry_after.pop(candidate["task_no"], None)
                    except Exception:
                        # Error strings can include HTTP headers, names or phone
                        # numbers; never persist or expose exception contents.
                        failed = True
                        self._retry_after[candidate["task_no"]] = utcnow() + timedelta(seconds=CACHE_SECONDS)
                    with self._lock:
                        self._state["profiles_updated"] = updated_count
            now = utcnow()
            remaining = len(self._candidates(db, now, include_cooldown=True))
            with self._lock:
                self._state["remaining"] = remaining
                if updated_count:
                    self._state["last_success_et"] = now.astimezone(rules.EASTERN).strftime("%Y-%m-%d %H:%M")
                if failed:
                    self._state["error"] = "部分司机资料更新失败，已保留上次资料，请稍后重试。"
                elif not self._stop.is_set():
                    self._state["error"] = None
        except Exception:
            with self._lock:
                self._state["error"] = "司机资料暂时无法更新，已保留上次资料，请稍后重试。"
        finally:
            if db is not None:
                db.close()
            with self._lock:
                self._state["running"] = False
                self._done.set()
