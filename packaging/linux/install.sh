#!/usr/bin/env bash
# SigmaCode Desk — per-user install on Linux (no sudo). Run from the extracted folder.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.local/opt/SigmaCodeDesk"
mkdir -p "$DEST" "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/256x256/apps"
rm -rf "$DEST"/*
cp -a "$SRC/SigmaCodeDesk/." "$DEST/"
cp "$SRC/sigmacode-desk.png" "$HOME/.local/share/icons/hicolor/256x256/apps/sigmacode-desk.png"
ln -sf "$DEST/SigmaCodeDesk" "$HOME/.local/bin/sigmacode-desk"
cat > "$HOME/.local/share/applications/sigmacode-desk.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=SigmaCode Desk
Name[ar]=سيجماكود ديسك
Comment=Offline Arabic OCR and document assistant
Exec=$DEST/SigmaCodeDesk
Icon=sigmacode-desk
Terminal=false
Categories=Office;
DESK
update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
echo "Installed. Open 'SigmaCode Desk' from the applications menu, or run: sigmacode-desk"
