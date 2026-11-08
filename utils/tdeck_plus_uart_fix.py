# Startup workaround for T-Deck Plus firmware built with the UART0 REPL enabled.
# Add this to /flash/main.py, preserving any existing startup code, then restart.
# New firmware built with MICROPY_HW_ENABLE_UART_REPL=0 does not need this fix.
import machine

# ESP32-S3 UART0_INT_ENA_REG: mask RXFIFO_FULL (bit 0) and RXFIFO_TOUT (bit 8).
# This stops GPS traffic on GPIO 44 from entering stdin; USB input stays enabled.
machine.mem32[0x6000000C] &= ~0x101
