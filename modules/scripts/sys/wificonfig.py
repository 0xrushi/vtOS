"""Start Wi-Fi from the last successful nm connection."""

import network
import modwifi


def start_saved(wlan=None):
    profile = modwifi.load_station()
    if profile is None:
        return False
    if wlan is None:
        wlan = network.WLAN(network.STA_IF)
    if not wlan.isconnected():
        wlan.active(True)
        wlan.connect(*profile)
    return True


def start_at_boot():
    try:
        start_saved()
    except (OSError, ValueError):
        print("Wi-Fi: saved connection could not start. Use nm to reconnect.")
