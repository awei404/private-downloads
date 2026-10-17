"""Serve the driver-arrival dashboard against an existing monitor database.

This process serves the dashboard and reads scoped TMS driver registration details.
The existing monitor remains responsible for transport polling and notifications.
"""
import argparse
import os
from pathlib import Path
import threading


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", required=True, help="existing monitor SQLite database")
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--readonly", action="store_true")
    parser.add_argument("--no-driver-sync", action="store_true", help="disable TMS driver reads for offline testing")
    args = parser.parse_args(argv)
    path = Path(args.database).expanduser().resolve()
    if not path.is_file():
        parser.error("the monitor database must already exist")
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    os.environ["BWI_DB_PATH"] = str(path)
    os.environ["BWI_DASHBOARD_FILE"] = "dashboard_v2.html"
    if args.readonly:
        os.environ["BWI_DASHBOARD_READONLY"] = "1"
    # Import after setting the database path: the shared dashboard may use it
    # for manually requested shift reports. No Monitor or Notifier is created.
    from .web import Handler, serve
    server = serve(str(path), host="127.0.0.1", port=args.port)
    synchronizer = None
    if not args.no_driver_sync and os.environ.get("BWI_DASHBOARD_READONLY") != "1":
        from .driver_sync import DriverSync
        synchronizer = DriverSync(str(path))
        Handler.driver_sync = synchronizer
        synchronizer.start_periodic()
    print(f"Driver arrival dashboard: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        if synchronizer:
            synchronizer.stop()
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
