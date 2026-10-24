#include "wayland_probe.h"
#ifdef HAVE_GIO_PIPEWIRE_DEPS
#include <gio/gio.h>

static GVariant *properties(GDBusConnection *bus, const char *interface) {
    /* Bound each read; do not launch a Portal backend as a side effect. */
    return g_dbus_connection_call_sync(bus, "org.freedesktop.portal.Desktop",
        "/org/freedesktop/portal/desktop", "org.freedesktop.DBus.Properties",
        "GetAll", g_variant_new("(s)", interface), G_VARIANT_TYPE("(a{sv})"),
        G_DBUS_CALL_FLAGS_NO_AUTO_START, 1000, NULL, NULL);
}

WaylandProbe wayland_probe(void) {
    WaylandProbe result = {.code="SESSION_BUS_UNAVAILABLE"};
    GDBusConnection *bus = g_bus_get_sync(G_BUS_TYPE_SESSION, NULL, NULL);
    if (!bus) return result;
    GVariant *remote = properties(bus, "org.freedesktop.portal.RemoteDesktop");
    GVariant *screen = properties(bus, "org.freedesktop.portal.ScreenCast");
    result.code = "PORTAL_INTERFACES_UNAVAILABLE";
    if (remote && screen) {
        GVariant *r = g_variant_get_child_value(remote, 0);
        GVariant *s = g_variant_get_child_value(screen, 0);
        g_variant_lookup(r, "version", "u", &result.remote_desktop_version);
        g_variant_lookup(s, "version", "u", &result.screencast_version);
        g_variant_lookup(r, "AvailableDeviceTypes", "u", &result.available_device_types);
        g_variant_lookup(s, "AvailableSourceTypes", "u", &result.available_source_types);
        if (result.remote_desktop_version < 2)
            result.code = "PORTAL_CONNECT_TO_EIS_UNSUPPORTED";
        else if (!(result.available_device_types & 2))
            result.code = "PORTAL_POINTER_UNSUPPORTED";
        else if (!(result.available_source_types & 1))
            result.code = "PORTAL_MONITOR_CAPTURE_UNSUPPORTED";
        else
            result.code = "PORTAL_INTERFACES_SUPPORTED";
        g_variant_unref(r);
        g_variant_unref(s);
    }
    if (remote) g_variant_unref(remote);
    if (screen) g_variant_unref(screen);
    g_object_unref(bus);
    return result;
}
#else
WaylandProbe wayland_probe(void) {
    return (WaylandProbe){.code="GIO_PIPEWIRE_DEVELOPMENT_DEPENDENCIES_MISSING"};
}
#endif
