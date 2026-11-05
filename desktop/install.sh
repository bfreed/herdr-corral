#!/bin/sh
# Adds the Corral panel to Herdr Desktop on this computer (the one whose
# window you use). After this, the panel updates itself from Corral.
#   curl -fsSL https://raw.githubusercontent.com/bfreed/herdr-corral/main/desktop/install.sh | sh
set -eu
ref="${CORRAL_REF:-main}"
base="https://raw.githubusercontent.com/bfreed/herdr-corral/$ref/desktop/corral/mods/corral"
dir="${HERDR_APP_HOME:-$HOME/.config/herdr-app}/mods/corral"
if [ -f "$dir/main.js" ] && ! head -n 1 "$dir/main.js" | grep -q "^// Corral panel"; then
  echo "$dir holds a different mod named corral; leaving it alone." >&2
  exit 1
fi
mkdir -p "$dir"
# main.js last: Herdr reloads the panel when it changes.
for f in mod.json main.js; do
  curl -fsSL "$base/$f" -o "$dir/.$f.part"
  mv "$dir/.$f.part" "$dir/$f"
done
echo "Corral panel installed in $dir. Herdr docks it on the left."
