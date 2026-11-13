#!/usr/bin/env bash
# VidNote kurulumu (Linux ve macOS). Tekrar çalıştırmak güvenlidir.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

OS="$(uname -s)"
PYTHON="python3"

echo "== 1/4 Sistem paketleri (ffmpeg, tesseract + Türkçe, Python)"
if [ "$OS" = "Darwin" ]; then
  if ! command -v brew >/dev/null; then
    echo "Homebrew bulunamadı. Önce https://brew.sh adresindeki tek satırlık komutla kur, sonra bu betiği yeniden çalıştır."; exit 1
  fi
  command -v ffmpeg >/dev/null || brew install ffmpeg
  command -v tesseract >/dev/null || brew install tesseract
  tesseract --list-langs 2>/dev/null | grep -qx tur || brew install tesseract-lang
  # macOS ile gelen Python eski olabilir; 3.10 ve üstü gerekir
  for c in python3.13 python3.12 python3.11 python3.10; do command -v "$c" >/dev/null && { PYTHON="$c"; break; }; done
  if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    brew install python@3.12; PYTHON="$(brew --prefix)/bin/python3.12"
  fi
else
  need=0
  command -v ffmpeg >/dev/null || need=1
  command -v tesseract >/dev/null || need=1
  python3 -c "import venv, ensurepip" 2>/dev/null || need=1
  tesseract --list-langs 2>/dev/null | grep -qx tur || need=1
  if [ "$need" = 1 ]; then
    SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo"
    if command -v apt-get >/dev/null; then
      $SUDO apt-get update && $SUDO apt-get install -y ffmpeg tesseract-ocr tesseract-ocr-tur python3-venv
    elif command -v dnf >/dev/null; then
      $SUDO dnf install -y ffmpeg tesseract tesseract-langpack-tur python3
    elif command -v pacman >/dev/null; then
      $SUDO pacman -S --needed --noconfirm ffmpeg tesseract tesseract-data-tur python
    else
      echo "Paket yöneticisi tanınmadı. Şunları elle kur: ffmpeg, tesseract (Türkçe dil paketiyle), python3 venv"; exit 1
    fi
  else
    echo "   hepsi kurulu, atlandı"
  fi
fi

echo "== 2/4 Python ortamı (.venv)"
[ -d .venv ] || "$PYTHON" -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt
if [ "$OS" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then
  echo "   Apple Silicon: hızlı konuşma tanıma (mlx-whisper) kuruluyor"
  .venv/bin/python -m pip install -q mlx-whisper || echo "   mlx-whisper kurulamadı; daha yavaş olan faster-whisper kullanılacak"
fi

echo "== 3/4 Ayar dosyası"
[ -f ayarlar.env ] || cp ayarlar.ornek.env ayarlar.env
chmod 600 ayarlar.env
chmod +x vidnote VidNote.command

echo "== 4/4 'vidnote' komutu"
mkdir -p "$HOME/.local/bin"
ln -sf "$DIR/vidnote" "$HOME/.local/bin/vidnote"
RC="~/.bashrc"; [ "$OS" = "Darwin" ] && RC="~/.zshrc"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "   Not: $HOME/.local/bin PATH içinde değil. Şunu $RC dosyana ekle: export PATH=\"\$HOME/.local/bin:\$PATH\"";; esac

echo
echo "Kurulum tamam. Dene:"
echo "   vidnote /yol/video.mp4        # terminalden döküm"
echo "   vidnote serve                 # tarayıcı sayfası: http://localhost:8765"
[ "$OS" = "Darwin" ] && echo "   ya da Finder'da VidNote.command dosyasına çift tıkla"
echo "İlk çalıştırmada konuşma modeli (yaklaşık 1,6 GB) bir kez indirilir."
