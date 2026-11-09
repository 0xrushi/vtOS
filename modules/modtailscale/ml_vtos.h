// Included only in the pinned MicroLink component, never by Python code.
#pragma once
#include "microlink_internal.h"

#define ML_VTOS_NET_EXIT BIT9
#define ML_VTOS_DERP_EXIT BIT10
#define ML_VTOS_COORD_EXIT BIT11
#define ML_VTOS_WG_EXIT BIT12

void ml_vtos_task_exit(microlink_t *ml, EventBits_t bit);
esp_err_t ml_vtos_stop(microlink_t *ml);
