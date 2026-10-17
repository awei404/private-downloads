"""BWI TMS monitor package.

Settings come from environment variables; a `.env` file in the working directory (or BWI_ENV_FILE) is read
first so the Windows production computer can keep its settings in one file. Values already set in the
environment win over the file. Credentials stay in secrets/ files, not in .env.
"""
import os


def _load_env(path):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_env(os.environ.get("BWI_ENV_FILE", ".env"))
