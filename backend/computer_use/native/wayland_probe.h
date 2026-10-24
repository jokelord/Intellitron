#pragma once
/* Read-only discovery. No authorization dialog, capture or input is started. */
typedef struct {
    const char *code;
    unsigned remote_desktop_version, screencast_version;
    unsigned available_device_types, available_source_types;
} WaylandProbe;
WaylandProbe wayland_probe(void);
