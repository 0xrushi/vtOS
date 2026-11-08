# ESP32-S3 RF SRAM IQ playback lab module (GPL-3.0-or-later, from
# whstudio123/esp32-iq-tx -- see mod_iqtx.c header)
SRC_USERMOD += $(USERMOD_DIR)/mod_iqtx.c
CFLAGS_USERMOD += -I$(USERMOD_DIR)
