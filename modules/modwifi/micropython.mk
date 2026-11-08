USERMOD_DIR := $(USERMOD_DIR)
# Passive WiFi promiscuous sniffer (modwifi)
SRC_USERMOD += $(USERMOD_DIR)/modwifi.c
# Link it to the build system
CFLAGS_USERMOD += -I$(USERMOD_DIR)
