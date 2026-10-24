#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "capture.h"
typedef enum { DESKTOP_WAYLAND, DESKTOP_X11, DESKTOP_UNAVAILABLE } DesktopBackend;
DesktopBackend desktop_backend(void);
const char *x11_probe_code(void);
bool x11_ready(void); /* Discovery alone never establishes an authorized binding. */
typedef struct X11Session X11Session;
typedef struct {
    uint32_t x,y,width,height;
    unsigned long root,monitor,outputs[32];
    unsigned output_count;
    char monitor_name[256];
} X11Geometry;
X11Session *x11_open(size_t max_frame_bytes);
void x11_close(X11Session *session);
void x11_pump(X11Session *session);
bool x11_geometry(X11Session *session,X11Geometry *geometry);
const char *x11_error(X11Session *session);
bool x11_snapshot(X11Session *session,CaptureFrame *frame);
const char *x11_send(X11Session *session,const char *kind,double x,double y,
                     const char *button,int units,double deadline_monotonic);
