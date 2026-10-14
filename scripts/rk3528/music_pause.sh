#!/bin/sh
# Ставит музыку Ирины (mpv) на паузу — вызывается, когда начинается AirPlay/DLNA/Bluetooth.
SOCK=/tmp/irene-mpv.sock
[ -S "$SOCK" ] || exit 0
printf '{"command":["set_property","pause",true]}\n' | socat - "UNIX-CONNECT:$SOCK" >/dev/null 2>&1
exit 0
