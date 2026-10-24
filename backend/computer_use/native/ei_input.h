#pragma once
#include <stdbool.h>
#include <stdint.h>
typedef struct {
    bool connected,resumed,emulating,absolute,button,scroll;
    bool unique_region,full_monitor_coverage,mapping_proven;
    unsigned long generation,geometry_version;
} InputState;
typedef struct NativeInput NativeInput;
NativeInput *input_open(int fd,const char *mapping);
void input_pump(NativeInput *input);
void input_close(NativeInput *input);
void input_invalidate(NativeInput *input);
bool input_commit(NativeInput *input);
bool input_region(NativeInput *input,uint32_t *x,uint32_t *y,uint32_t *width,uint32_t *height);
const char *input_send(NativeInput *input,const char *kind,double x,double y,const char *button,int units,double deadline_monotonic);
const char *input_error(NativeInput *input);
/* Fixed diagnostic for an incomplete EIS startup; NULL once mapped. */
const char *input_startup_status(NativeInput *input);
bool input_can_emit(const InputState *state);

/* Returns whether an event needs emission; held state changes only on valid transitions. */
bool input_button_transition(bool *held,bool pressed);
