#!/bin/sh
# Xvnc on display :1 (port 5901) with VNC-password auth and an LXDE desktop
# (panel, menu, openbox) sized for a 320x240 screen.
set -e
: "${VNC_PASSWORD:=secret}" "${GEOMETRY:=320x240}"   # defaults; override with docker run -e
mkdir -p "$HOME/.vnc"
printf '%s\n' "$VNC_PASSWORD" | vncpasswd -f > "$HOME/.vnc/passwd"
chmod 600 "$HOME/.vnc/passwd"

Xvnc :1 -geometry "$GEOMETRY" -depth 24 -rfbport 5901 \
     -SecurityTypes VncAuth -PasswordFile "$HOME/.vnc/passwd" \
     -AlwaysShared -localhost no &
export DISPLAY=:1
# the first successful xsetroot means the X server is accepting clients
for _ in $(seq 1 50); do xsetroot -solid '#203040' 2>/dev/null && break; sleep 0.2; done
xrdb -merge "$HOME/.Xresources"
echo "apps: panel menu / launchers at the bottom (terminal, Firefox, files)"
echo "vnc test server: port 5901 (display :1), $GEOMETRY, password '$VNC_PASSWORD'"
exec dbus-launch --exit-with-session startlxde
