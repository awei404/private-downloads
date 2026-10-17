"""Windows entrypoint: the unchanged monitor plus read-only driver synchronization."""
import os
from pathlib import Path


def main():
    from . import config
    from .__main__ import main as run_monitor
    from .driver_sync import DriverSync
    from .store import Store
    from .web import Handler
    Path(config.DB_PATH).resolve().parent.mkdir(parents=True, exist_ok=True)
    store = Store(config.DB_PATH)
    store.db.close()
    sync = None
    try:
        if os.environ.get('BWI_DASHBOARD_READONLY') != '1':
            sync = DriverSync(config.DB_PATH, auth_file=os.environ.get('GOFO_AUTH_FILE', 'secrets/gofo_auth'))
            Handler.driver_sync = sync
            sync.start_periodic()
        return run_monitor([])
    finally:
        if sync:
            sync.stop()
        Handler.driver_sync = None


if __name__ == '__main__':
    main()
