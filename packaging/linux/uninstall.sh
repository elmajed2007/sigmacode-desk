#!/usr/bin/env bash
# Removes the program; your data in ~/.local/share/SigmaCodeDesk is kept.
rm -rf "$HOME/.local/opt/SigmaCodeDesk" "$HOME/.local/bin/sigmacode-desk" \
       "$HOME/.local/share/applications/sigmacode-desk.desktop" \
       "$HOME/.local/share/icons/hicolor/256x256/apps/sigmacode-desk.png"
echo "Removed. Data kept in ~/.local/share/SigmaCodeDesk"
