#!/usr/bin/env bash
# macOS: Finder'da çift tıklayınca VidNote sayfasını açar. Kapatmak için bu pencereyi kapat.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "Önce kurulum yapılacak (bir kez)."; ./kur.sh || { echo; read -r -p "Kurulum tamamlanamadı. Kapatmak için Enter'a bas."; exit 1; }
fi
exec ./vidnote serve
