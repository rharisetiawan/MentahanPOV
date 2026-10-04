#!/bin/sh
# mentahan — satu perintah buat ngurus server HG680 tanpa hafal docker.
#   mentahan status     cek disk, docker, container, tailscale
#   mentahan fix        nyalakan docker + semua container + tailscale
#   mentahan cleanup    hapus video lama yang sudah selesai (lewat bot)
#   mentahan logs       30 baris log bot terakhir
#   mentahan disk       siapa yang makan disk
#   mentahan watchdog   pasang penjaga otomatis (cek tiap 5 menit)
# Jalankan pakai sudo kalau diminta password. Aman dijalankan berulang.
APP=/www/apps/mentahanpov
TS=tailscale-hg680
cd "$APP" || { echo "folder $APP tidak ada"; exit 1; }
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

docker_ok() { $SUDO systemctl is-active --quiet docker; }

status() {
  echo "== Disk /www =="; df -h /www | tail -1
  echo "== Docker =="; docker_ok && echo "jalan" || echo "MATI  -> jalankan: mentahan fix"
  echo "== Container =="
  $SUDO docker ps -a --format 'table {{.Names}}\t{{.Status}}' 2>/dev/null || echo "(docker tidak merespons)"
  echo "== Tailscale =="
  $SUDO docker exec $TS tailscale status 2>/dev/null | head -4 || echo "(tailscale tidak jalan)"
}

fix() {
  free_kb=$(df -Pk /www | awk 'NR==2{print $4}')
  if [ "$free_kb" -lt 1000000 ]; then
    echo "!! Sisa disk < 1 GB. Bersihkan cache build dulu."
    docker_ok && $SUDO docker builder prune -af >/dev/null 2>&1
  fi
  docker_ok || { echo "Docker mati, menyalakan..."; $SUDO systemctl restart docker; sleep 8; }
  docker_ok || { echo "Docker gagal naik. Cek: sudo journalctl -u docker -n 40"; exit 1; }
  $SUDO docker compose up -d
  $SUDO docker start $TS >/dev/null 2>&1
  sleep 3; status
}

cleanup() {
  $SUDO docker compose exec -T mentahanpov-bot python - <<'PY'
from config import config
from core import cleanup
from pathlib import Path
root = Path("/app")
c = cleanup.plan(config.state_file, retention_days=config.cleanup_retention_days,
    ignore_platforms=config.cleanup_ignore_platforms,
    allowed_roots=[root / "incoming", config.posting_copy_dir],
    posting_copy_dir=config.posting_copy_dir)
files, freed = cleanup.apply(c)
print(f"{files} file dihapus, {freed/1024**3:.2f} GB bebas")
PY
  df -h /www | tail -1
}

watchdog() {
  $SUDO sh -c "echo '*/5 * * * * root /usr/local/bin/mentahan watchdog-run' > /etc/cron.d/mentahan-watchdog"
  echo "Penjaga terpasang: cek tiap 5 menit (log: /var/log/mentahan-watchdog.log)"
}

watchdog_run() {
  # Diam kalau sehat; kalau ada yang mati, jalankan fix dan catat.
  if docker_ok && [ "$(docker ps -q | wc -l)" -ge 3 ]; then exit 0; fi
  echo "$(date '+%F %T') ada yang mati, menjalankan fix" >> /var/log/mentahan-watchdog.log
  fix >> /var/log/mentahan-watchdog.log 2>&1
}

case "$1" in
  status) status ;;
  fix) fix ;;
  cleanup) cleanup ;;
  logs) $SUDO docker compose logs --tail 30 mentahanpov-bot ;;
  disk) $SUDO du -sh /www/apps/mentahanpov/incoming /www/apps/mentahanpov/state \
          /www/docker /www/containerd-data /www/swapfile 2>/dev/null ;;
  watchdog) watchdog ;;
  watchdog-run) watchdog_run ;;
  *) sed -n '2,8p' "$0" ;;
esac
