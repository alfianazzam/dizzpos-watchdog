#!/usr/bin/env python3
"""Watchdog auto-pulih produksi DizzPOS (dipakai .github/workflows/prod-watchdog.yml).

Kejadian 2026-10-19 11:22 UTC: VM Contabo (194.233.76.46) dimatikan dari dalam
(API menerima sinyal stop yang rapi), status Contabo `stopped`, seluruh
aplikasi mati ±22 menit dan baru ketahuan dari laporan pengguna (Cloudflare 521)
karena prod-monitor hanya jalan tiap jam.

Alur tiap jalan (±5 menit sekali):
1. GET HEALTH_URL hingga HEALTH_ATTEMPTS kali (jeda HEALTH_GAP detik). Sekali
   200 → sehat, selesai (exit 0).
2. Masih gagal → baca status VM di API Contabo.
   - `stopped`/`error` → aksi `start`.
   - `running` tapi API tetap mati → aksi `restart` (server hang).
   - Lainnya (provisioning, installing, …) → jangan sentuh.
   Aksi dilewati bila ada aksi apa pun pada VM dalam COOLDOWN_MINUTES terakhir
   (mencegah restart berulang saat server sedang boot / ditangani manusia).
3. Tunggu sampai RECOVERY_WAIT detik untuk health 200, tulis ringkasan, lalu
   exit 1 supaya job merah → email GitHub ke pemilik repo (selalu, karena
   server memang sempat mati).

Env wajib: CONTABO_CLIENT_ID, CONTABO_CLIENT_SECRET, CONTABO_API_USER,
CONTABO_API_PASSWORD, CONTABO_INSTANCE_ID.
Opsional: HEALTH_URL, HEALTH_ATTEMPTS (3), HEALTH_GAP (30), COOLDOWN_MINUTES (15),
RECOVERY_WAIT (420), DRY_RUN=1 (baca saja, tanpa aksi), FORCE_CHECK=1 (lewati
cek health — untuk uji jalur Contabo), GITHUB_STEP_SUMMARY.
Tanpa dependensi selain stdlib.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

HEALTH_URL = os.environ.get("HEALTH_URL", "https://pos.dizzbooth.com/healthz/ready")
HEALTH_ATTEMPTS = int(os.environ.get("HEALTH_ATTEMPTS", "3"))
HEALTH_GAP = int(os.environ.get("HEALTH_GAP", "30"))
COOLDOWN = int(os.environ.get("COOLDOWN_MINUTES", "15"))
RECOVERY_WAIT = int(os.environ.get("RECOVERY_WAIT", "420"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
FORCE_CHECK = os.environ.get("FORCE_CHECK") == "1"
TOKEN_URL = "https://auth.contabo.com/auth/realms/contabo/protocol/openid-connect/token"
API = "https://api.contabo.com/v1"
UA = "dizzpos-watchdog/1.0"

summary_lines = []


def say(msg):
    print(msg, flush=True)
    summary_lines.append(msg)


def write_summary():
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("## Watchdog produksi\n\n")
            f.write("\n".join(f"- {line}" for line in summary_lines) + "\n")


def healthy(timeout=15):
    try:
        req = urllib.request.Request(HEALTH_URL, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200, str(r.status)
    except urllib.error.HTTPError as e:
        return False, str(e.code)
    except Exception as e:  # noqa: BLE001 — timeout/DNS/koneksi ditolak
        return False, type(e).__name__


def contabo_token():
    data = urllib.parse.urlencode({
        "grant_type": "password",
        "client_id": os.environ["CONTABO_CLIENT_ID"],
        "client_secret": os.environ["CONTABO_CLIENT_SECRET"],
        "username": os.environ["CONTABO_API_USER"],
        "password": os.environ["CONTABO_API_PASSWORD"],
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["access_token"]


def contabo(token, method, path, body=None):
    req = urllib.request.Request(
        API + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": "Bearer " + token,
            "x-request-id": str(uuid.uuid4()),
            "Content-Type": "application/json",
            "User-Agent": UA,
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def recent_action(token, instance_id):
    """Aksi terakhir pada VM (start/restart/stop/…) dalam COOLDOWN menit, atau None."""
    res = contabo(token, "GET", f"/compute/instances/actions/audits?instanceId={instance_id}&size=20")
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=COOLDOWN)
    for item in res.get("data", []):
        ts = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
        if ts >= cutoff:
            action = (item.get("changes") or {}).get("new", {}).get("instanceAction", "?")
            return f"{action} pada {item['timestamp']}"
    return None


def main():
    instance_id = os.environ["CONTABO_INSTANCE_ID"]
    if not FORCE_CHECK:
        last = ""
        for attempt in range(1, HEALTH_ATTEMPTS + 1):
            ok, last = healthy()
            if ok:
                print(f"Sehat ({HEALTH_URL} -> 200).")
                return 0
            print(f"Percobaan {attempt}/{HEALTH_ATTEMPTS}: health gagal ({last}).")
            if attempt < HEALTH_ATTEMPTS:
                time.sleep(HEALTH_GAP)
        say(f"API produksi tidak sehat {HEALTH_ATTEMPTS}x berturut-turut ({last}).")

    token = contabo_token()
    inst = contabo(token, "GET", f"/compute/instances/{instance_id}")["data"][0]
    status = inst.get("status")
    say(f"Status VM Contabo {instance_id}: `{status}`.")
    if FORCE_CHECK:
        print("FORCE_CHECK: jalur Contabo terbaca, tidak ada aksi.")
        write_summary()
        return 0

    action = {"stopped": "start", "error": "start", "running": "restart"}.get(status)
    if action is None:
        say("Status VM sedang transisi — tidak ada aksi otomatis.")
    else:
        busy = recent_action(token, instance_id)
        if busy:
            say(f"Lewati `{action}`: sudah ada aksi {busy} (cooldown {COOLDOWN} menit).")
        elif DRY_RUN:
            say(f"DRY_RUN: akan menjalankan `{action}`.")
        else:
            contabo(token, "POST", f"/compute/instances/{instance_id}/actions/{action}")
            say(f"Aksi `{action}` dikirim ke Contabo.")
            deadline = time.time() + RECOVERY_WAIT
            while time.time() < deadline:
                time.sleep(20)
                ok, code = healthy()
                if ok:
                    say("API kembali sehat (200) setelah aksi otomatis.")
                    break
            else:
                say(f"API belum pulih dalam {RECOVERY_WAIT} detik — perlu dicek manual.")

    write_summary()
    # Selalu merah saat server sempat mati → email GitHub ke pemilik repo.
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        say(f"Watchdog gagal: {type(e).__name__}: {e}")
        write_summary()
        sys.exit(1)
