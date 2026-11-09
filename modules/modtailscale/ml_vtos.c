// Lifecycle glue for MicroLink 216da330. Compiled into its IDF component.
#include "ml_vtos.h"
#include "freertos/idf_additions.h"
#include "lwip/sockets.h"

void ml_vtos_task_exit(microlink_t *ml, EventBits_t bit) {
    // The owner joins suspended workers before deleting their stacks/TCBs.
    // No task may touch ml after publishing its acknowledgement.
    xEventGroupSetBits(ml->events, bit);
    vTaskSuspend(NULL);
    abort();
}

esp_err_t ml_vtos_stop(microlink_t *ml) {
    if (!ml) return ESP_ERR_INVALID_ARG;
    if (ml->events && ml->vtos_tasks_started) {
        xEventGroupSetBits(ml->events, ML_EVT_SHUTDOWN_REQUEST);
        ml_coord_cmd_t cmd = ML_CMD_DISCONNECT;
        if (ml->coord_cmd_queue) xQueueSend(ml->coord_cmd_queue, &cmd, 0);
        // Wake TCP reads without closing/reusing a descriptor in another task.
        if (ml->coord_sock >= 0) shutdown(ml->coord_sock, SHUT_RDWR);
        if (ml->derp.sockfd >= 0) shutdown(ml->derp.sockfd, SHUT_RDWR);
        EventBits_t bits = xEventGroupWaitBits(ml->events, ml->vtos_tasks_started,
                              pdFALSE, pdTRUE, pdMS_TO_TICKS(15000));
        if ((bits & ml->vtos_tasks_started) != ml->vtos_tasks_started) {
            // Retain the entire context. A subsequent down can finish joining;
            // freeing live queues/keys here would corrupt the heap.
            return ESP_ERR_TIMEOUT;
        }
        TaskHandle_t tasks[] = {ml->net_io_task, ml->derp_tx_task,
                               ml->coord_task, ml->wg_mgr_task};
        for (unsigned i = 0; i < sizeof(tasks) / sizeof(tasks[0]); i++) {
            if (!tasks[i]) continue;
            while (eTaskGetState(tasks[i]) != eSuspended) vTaskDelay(1);
            vTaskDeleteWithCaps(tasks[i]);
        }
        ml->net_io_task = ml->derp_tx_task = NULL;
        ml->coord_task = ml->wg_mgr_task = NULL;
        ml->vtos_tasks_started = 0;
    }
    if (ml->disco_sock4 >= 0) { close(ml->disco_sock4); ml->disco_sock4 = -1; }
    if (ml->stun_sock >= 0) { close(ml->stun_sock); ml->stun_sock = -1; }
    if (ml->stun_sock6 >= 0) { close(ml->stun_sock6); ml->stun_sock6 = -1; }
    if (ml->disco_sock6 >= 0) { close(ml->disco_sock6); ml->disco_sock6 = -1; }
    ml_rx_packet_t packet;
    QueueHandle_t queues[] = {ml->disco_rx_queue, ml->wg_rx_queue, ml->stun_rx_queue};
    for (unsigned i = 0; i < sizeof(queues) / sizeof(queues[0]); i++) {
        if (queues[i]) while (xQueueReceive(queues[i], &packet, 0) == pdTRUE) free(packet.data);
    }
    ml_peer_update_t *update;
    if (ml->peer_update_queue) {
        while (xQueueReceive(ml->peer_update_queue, &update, 0) == pdTRUE) free(update);
    }
    ml_derp_tx_item_t tx;
    if (ml->derp_tx_queue) {
        while (xQueueReceive(ml->derp_tx_queue, &tx, 0) == pdTRUE) free(tx.data);
    }
    ml->state = ML_STATE_IDLE;
    return ESP_OK;
}
