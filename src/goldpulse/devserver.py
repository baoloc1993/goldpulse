"""`make dev`: migrate → chạy web (uvicorn) + worker + (v2) não bot cùng lúc, Ctrl+C dừng cả ba."""

import os
import subprocess
import sys
import time

WEB = [
    sys.executable,
    "-m",
    "uvicorn",
    "goldpulse.main:app",
    # Không --reload: trên Windows reloader treo khi tắt process cũ và lặng lẽ phục vụ code cũ
    # (thử thật 2026-09-24). Sửa code → Ctrl+C, chạy lại make dev.
    "--host",
    "127.0.0.1",
    "--port",
    "8000",
    # Trang S1 giữ kết nối keep-alive (tải lại 5s/lần) — thiếu mốc này thì reload treo mãi
    "--timeout-graceful-shutdown",
    "3",
    # Như Dockerfile production: không tin X-Forwarded-For do client tự đặt (giới hạn tốc độ theo IP thật)
    "--no-proxy-headers",
]
WORKER = [sys.executable, "-m", "goldpulse.worker"]
BRAIN = [sys.executable, "-m", "goldpulse.brain"]


def main() -> int:
    # Console Windows mặc định cp1252 — in tiếng Việt sẽ chết nếu không ép UTF-8
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True, env=env)
    procs = [subprocess.Popen(WEB, env=env), subprocess.Popen(WORKER, env=env), subprocess.Popen(BRAIN, env=env)]
    print("\n  GoldPulse: http://127.0.0.1:8000  (khoá ở deployment/local/.env → OPS_ACCESS_KEY)\n", flush=True)
    try:
        while all(p.poll() is None for p in procs):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            if p.poll() is None:
                if os.name == "nt":
                    # uvicorn --reload đẻ process con qua multiprocessing — terminate() chỉ giết cha,
                    # con mồ côi giữ cổng 8000 và phục vụ code cũ. Giết cả cây.
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
                else:
                    p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
    return max((p.returncode or 0) for p in procs)


if __name__ == "__main__":
    sys.exit(main())
