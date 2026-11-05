#!/bin/sh
# Adds the Corral panel to Herdr Desktop on this computer (the one whose
# window you use). After this, the panel updates itself from Corral.
#   curl -fsSL https://raw.githubusercontent.com/bfreed/herdr-corral/main/desktop/install.sh | sh
# On Windows, use install.ps1 beside this file.
set -eu
ref="${CORRAL_REF:-main}"
base="https://raw.githubusercontent.com/bfreed/herdr-corral/$ref/desktop/corral/mods/corral"
home="${HERDR_APP_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/herdr-app}"
dir="$home/mods/corral"
if [ -f "$dir/main.js" ] && ! head -n 1 "$dir/main.js" | grep -q "^// Corral panel"; then
  echo "$dir holds a different mod named corral; leaving it alone." >&2
  exit 1
fi

# Herdr docks a new panel the first time it sees it, but never into its own
# layouts (main, terminals, win98): those are read-only, and while one shows
# the panel waits. So on one of those, show a copy of it first; Herdr then
# docks the panel there. The copy keeps using Herdr's panels, which keep
# updating; only its arrangement stops following Herdr's.
# Shows $copy: switches to it, or makes it from $shown (which switches too).
show_copy() {
  if [ -f "$home/layouts/$copy.json" ]; then "$1" do layout.switch --name "$copy"
  else "$1" do layout.clone --name "$shown" --as "$copy"; fi
}
shown=$(cat "$home/shared.json" 2>/dev/null | tr -d '\n' | sed -n 's/.*"layout"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' || true)
{ [ -n "$shown" ] && [ -f "$home/layouts/$shown.json" ]; } || shown=main
seen=no
cat "$home/seen.json" 2>/dev/null | tr -d ' \n' | grep -q '"corral"' && seen=yes
copy=""
case "$shown" in main|terminals|win98) [ "$seen" = no ] && copy="$shown-corral";; esac

# Download first, so a failed download changes nothing.
mkdir -p "$dir"
for f in mod.json main.js; do
  curl -fsSL "$base/$f" -o "$dir/.$f.part"
done

if [ -n "$copy" ]; then
  app=$(command -v herdr-app 2>/dev/null || true)
  for p in "$HOME/.local/bin/herdr-app" /Applications/Herdr.app/Contents/MacOS/Herdr; do
    [ -z "$app" ] && [ -x "$p" ] && app="$p"
  done
  if [ -n "$app" ] && show_copy "$app" </dev/null >/dev/null 2>&1; then
    echo "Herdr now shows $copy, your copy of its $shown layout; the panel docks there (ctrl-shift-l switches layouts)."
  else
    manual=yes
  fi
fi

# main.js last: Herdr reloads the panel when it changes.
for f in mod.json main.js; do
  mv "$dir/.$f.part" "$dir/$f"
done
echo "Corral panel installed in $dir."
if [ "${manual:-}" = yes ]; then
  cat <<NOTE
Herdr shows new panels only in a layout of your own, and it's showing its own
"$shown". With Herdr open, make a copy of it, and Herdr docks the panel there:
  herdr-app do layout.clone --name $shown --as $copy
(Or in Herdr's command palette: Duplicate layout.)
NOTE
elif [ -z "$copy" ]; then
  [ "$seen" = yes ] && echo "It shows wherever your layout has it." || echo "Herdr docks it on the left."
fi
