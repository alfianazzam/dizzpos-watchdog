# dizzpos-watchdog

Penjaga auto-pulih server produksi DizzPOS. Tiap ±5 menit:

1. Cek `https://pos.dizzbooth.com/healthz/ready` (3x, jeda 30 detik).
2. Bila tetap gagal: baca status VM di API Contabo.
   - `stopped` → **start**
   - `running` tapi API mati (hang) → **restart**
   - Tidak ada aksi ulang dalam 15 menit setelah aksi apa pun (sedang boot / ditangani manual).
3. Job merah → email GitHub ke pemilik repo, ringkasan di halaman run.

Repo ini publik hanya supaya menit GitHub Actions gratis. Tidak ada kode aplikasi
dan tidak ada kredensial di sini — semuanya di **Secrets** repo
(`CONTABO_CLIENT_ID`, `CONTABO_CLIENT_SECRET`, `CONTABO_API_USER`,
`CONTABO_API_PASSWORD`) dan variabel `CONTABO_INSTANCE_ID`.

Uji manual: Actions → watchdog → Run workflow (`dry_run` = baca saja,
`force_check` = lewati cek health untuk menguji akses Contabo).

Latar: 2026-10-19 11:22 UTC VM produksi mati sendiri ±22 menit dan baru
ketahuan dari laporan pengguna. Sumber skrip juga ada di repo aplikasi
(`deploy/monitor/prod-watchdog.py`).
