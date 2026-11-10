# A tiny VNC server to test the vtOS `vnc` client against.
#
#   docker build -t vtos-vnctest tools/vnctest
#   docker run --rm --shm-size=512m -p 5901:5901 vtos-vnctest
#
# (--shm-size keeps Firefox from crashing its tabs.) Then on the T-Deck, with
# your Mac's Wi-Fi address (`ipconfig getifaddr en0`):
#
#   vnc <mac-ip>:1 secret
#
# Options (docker run -e ...): VNC_PASSWORD (default "secret"; only the first 8
# characters count), GEOMETRY (default 320x240, the T-Deck's own size).
#
# Everything is unencrypted VNC with a weak password: run it on a trusted
# network, and stop it when you are done.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tigervnc-standalone-server tigervnc-tools \
      x11-xkb-utils xkb-data x11-xserver-utils \
      xterm xfonts-base \
 && rm -rf /var/lib/apt/lists/*

# Firefox: Ubuntu's own `firefox` package is a Snap stub that cannot run in
# Docker, so take the real .deb from Mozilla's team PPA (pinned above Ubuntu's),
# plus pcmanfm as a light file manager and the fonts/icons/dbus they expect.
RUN apt-get update \
 && apt-get install -y --no-install-recommends software-properties-common gpg-agent ca-certificates \
 && add-apt-repository -y ppa:mozillateam/ppa \
 && printf 'Package: *\nPin: release o=LP-PPA-mozillateam\nPin-Priority: 1001\n' \
      > /etc/apt/preferences.d/mozilla-firefox \
 && apt-get install -y --no-install-recommends \
      firefox-esr pcmanfm dbus-x11 fonts-dejavu-core adwaita-icon-theme \
 && apt-get purge -y software-properties-common gpg-agent && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

# A real desktop with a panel (menu, launchers, window list, clock): LXDE,
# the lighter of LXDE/XFCE, which keeps VNC updates small for the T-Deck.
RUN apt-get update && apt-get install -y --no-install-recommends \
      lxde-core librsvg2-common \
 && rm -rf /var/lib/apt/lists/*

COPY firefox-user.js firefox-userChrome.css /usr/share/vtos/
COPY firefox-fit /usr/local/bin/firefox
# no login-session manager in Docker: lxpolkit would pop up an error dialog
# (lxsession starts the agent itself, so the binary is replaced with a no-op)
RUN rm -f /etc/xdg/autostart/lxpolkit.desktop /etc/xdg/autostart/light-locker.desktop \
 && mv /usr/bin/lxpolkit /usr/bin/lxpolkit.real \
 && printf '#!/bin/sh\nexit 0\n' > /usr/bin/lxpolkit && chmod +x /usr/bin/lxpolkit
# the panel's Firefox launcher and the menu entry go through the scaled launcher
RUN sed -i 's#^Exec=firefox-esr#Exec=/usr/local/bin/firefox#' /usr/share/applications/firefox-esr.desktop
# every window opens maximised above the panel, without a title bar
RUN mkdir -p /root/.config/openbox /root/.config/lxpanel/LXDE/panels /root/.config/lxsession/LXDE \
 && test "$(grep -c '</applications>' /etc/xdg/openbox/LXDE/rc.xml)" = 1 \
 && sed 's#</applications>#<application class="*"><maximized>yes</maximized><decor>no</decor></application></applications>#' \
      /etc/xdg/openbox/LXDE/rc.xml > /root/.config/openbox/lxde-rc.xml
COPY pcmanfm-fit /usr/local/bin/pcmanfm
COPY pcmanfm.conf /root/.config/pcmanfm/default/pcmanfm.conf
COPY pcmanfm.conf /root/.config/pcmanfm/LXDE/pcmanfm.conf
COPY lxpanel-panel /root/.config/lxpanel/LXDE/panels/panel
COPY lxsession-autostart /root/.config/lxsession/LXDE/autostart
COPY Xresources /root/.Xresources
COPY start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh

EXPOSE 5901
CMD ["/usr/local/bin/start.sh"]
