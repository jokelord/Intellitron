#define _GNU_SOURCE
#include "ipc.h"
#include "control.h"
#include "wayland_probe.h"
#include "x11.h"
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
static void request_stop(int signal_number){(void)signal_number;control_stop_requested=1;}
int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--probe")) {
        WaylandProbe probe = {0};
        if(desktop_backend()==DESKTOP_X11)probe.code="WAYLAND_SESSION_NOT_SELECTED";
        else if(desktop_backend()==DESKTOP_UNAVAILABLE)probe.code="DESKTOP_SESSION_UNSUPPORTED";
        else probe=wayland_probe();
        /* Successful discovery is not capture/input readiness. Fixed strings
         * and numeric values only; no desktop data is exposed by this probe. */
        printf("{\"probe_code\":\"%s\",\"portal_interfaces_supported\":%s,"
               "\"remote_desktop_version\":%u,\"screencast_version\":%u,"
               "\"available_device_types\":%u,\"available_source_types\":%u,"
               "\"capture_ready\":false,\"input_ready\":false,\"ready\":false,"
               "\"x11_code\":\"%s\"}\n",
               probe.code, !strcmp(probe.code, "PORTAL_INTERFACES_SUPPORTED") ? "true" : "false",
               probe.remote_desktop_version, probe.screencast_version,
               probe.available_device_types, probe.available_source_types, x11_probe_code());
        return 0; /* The read-only diagnostic ran; inspect the readiness fields. */
    }
    if (argc != 3 || strcmp(argv[1], "--control-fd")) return 2;
    char *end = NULL;
    errno = 0;
    long fd = strtol(argv[2], &end, 10);
    if (errno || !end || *end || fd < 3 || fd > INT_MAX) return 2;
    if (!ipc_validate_channel((int)fd)) return 3;
    struct sigaction action={0};
    action.sa_handler=request_stop;sigemptyset(&action.sa_mask);
    if(sigaction(SIGTERM,&action,NULL)<0||sigaction(SIGINT,&action,NULL)<0)return 3;
    int result = control_run((int)fd, getppid());
    close((int)fd);
    return result;
}
