import { defineHostConfig } from "@pocketjs/framework/host-config";

/**
 * Pocket Shell on the LilyGO T-Deck (host id tdeck-home): the launcher's
 * 400x240 primary surface stays off screen; the 320x240 auxiliary surface is
 * the panel. Buttons only for now (keyboard + BOOT).
 */
export default defineHostConfig({ host: "./pocket.host.json" });
