"""`make dev`: migrate → chạy web (uvicorn --reload) + worker cùng lúc, Ctrl+C dừng cả hai."""

import subprocess
import sys
import time

WEB = [
    sys.executable,
    "-m",
    "uvicorn",
    "goldpulse.main:app",
    "--reload",
    "--reload-dir",
    "src",
    "--host",
    "127.0.0.1",
    "--port",
    "8000",
]
WORKER = [sys.executable, "-m", "goldpulse.worker"]


def main() -> int:
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
    procs = [subprocess.Popen(WEB), subprocess.Popen(WORKER)]
    print("\n  GoldPulse: http://127.0.0.1:8000  (khoá ở deployment/local/.env → OPS_ACCESS_KEY)\n", flush=True)
    try:
        while all(p.poll() is None for p in procs):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
    return max((p.returncode or 0) for p in procs)


if __name__ == "__main__":
    sys.exit(main())
