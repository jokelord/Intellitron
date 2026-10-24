#pragma once
#include <stdbool.h>
#include <stdint.h>
typedef enum { PORTAL_CLOSED, PORTAL_CREATING, PORTAL_DEVICES, PORTAL_SOURCES,
               PORTAL_STARTING, PORTAL_STREAM, PORTAL_EIS, PORTAL_UNAVAILABLE } PortalPhase;
typedef struct {
    PortalPhase phase; bool ready; unsigned long generation;
    void *bus; unsigned closed_subscription, owner_subscription;
    char *session, *mapping_id; uint32_t node_id; uint64_t serial;
    int pipewire_fd, eis_fd; const char *code;
    int64_t deadline_us;
    bool has_position,has_size;
    int32_t logical_x,logical_y,logical_width,logical_height;
} PortalState;
bool portal_start(PortalState *state);
bool portal_open(PortalState *state, unsigned timeout_ms);
void portal_pump(PortalState *state);
void portal_close(PortalState *state);

bool portal_parse_start(PortalState *state,void *result);

const char *portal_call_failure(void *error);
