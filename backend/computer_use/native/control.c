#include "control.h"
#include "ipc.h"
#include "wayland_probe.h"
#include "x11.h"
#include "live.h"
#include <unistd.h>
#include <poll.h>
#include <sys/socket.h>
volatile sig_atomic_t control_stop_requested;
#ifdef HAVE_JSON_GLIB
#include <json-glib/json-glib.h>
#include <string.h>

typedef struct { GPtrArray *objects; gboolean duplicate; } ParseGuard;
static void object_start(JsonParser *parser, gpointer data) {
    (void)parser;
    ParseGuard *guard = data;
    g_ptr_array_add(guard->objects, g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL));
}
static void object_member(JsonParser *parser, JsonObject *object, const char *name, gpointer data) {
    (void)parser; (void)object;
    ParseGuard *guard = data;
    GHashTable *keys = g_ptr_array_index(guard->objects, guard->objects->len - 1);
    if (g_hash_table_contains(keys, name)) guard->duplicate = TRUE;
    g_hash_table_add(keys, g_strdup(name));
}
static void object_end(JsonParser *parser, JsonObject *object, gpointer data) {
    (void)parser; (void)object;
    ParseGuard *guard = data;
    g_ptr_array_remove_index(guard->objects, guard->objects->len - 1);
}
static gboolean typed(JsonObject *object, const char *key, GType type) {
    JsonNode *node = json_object_get_member(object, key);
    return node && JSON_NODE_HOLDS_VALUE(node) && json_node_get_value_type(node) == type;
}
static gboolean reply(int fd, const char *nonce, gint64 generation, gint64 sequence,
                      const char *kind, const char *code, const char *x11_code, const WaylandProbe *probe) {
    JsonBuilder *builder = json_builder_new();
    json_builder_begin_object(builder);
#define INT_FIELD(key, val) json_builder_set_member_name(builder, key); json_builder_add_int_value(builder, val)
#define STR_FIELD(key, val) json_builder_set_member_name(builder, key); json_builder_add_string_value(builder, val)
    INT_FIELD("version", 1); INT_FIELD("generation", generation); INT_FIELD("sequence", sequence);
    STR_FIELD("nonce", nonce); STR_FIELD("kind", kind);
    json_builder_set_member_name(builder, "payload"); json_builder_begin_object(builder);
    STR_FIELD("code", code);
    json_builder_set_member_name(builder, "capture_ready"); json_builder_add_boolean_value(builder, FALSE);
    json_builder_set_member_name(builder, "input_ready"); json_builder_add_boolean_value(builder, FALSE);
    STR_FIELD("x11_code", x11_code ? x11_code : "X11_NOT_PROBED");
    if (probe) {
        STR_FIELD("wayland_code", probe->code);
        INT_FIELD("remote_desktop_version", probe->remote_desktop_version);
        INT_FIELD("screencast_version", probe->screencast_version);
        INT_FIELD("available_device_types", probe->available_device_types);
        INT_FIELD("available_source_types", probe->available_source_types);
    }
    json_builder_end_object(builder); json_builder_end_object(builder);
    JsonNode *root = json_builder_get_root(builder);
    JsonGenerator *generator = json_generator_new();
    json_generator_set_root(generator, root);
    gsize size;
    char *data = json_generator_to_data(generator, &size);
    gboolean ok = send(fd, data, size, MSG_NOSIGNAL) == (ssize_t)size;
    g_free(data); g_object_unref(generator); json_node_free(root); g_object_unref(builder);
    return ok;
}

static int control_loop(int fd, pid_t parent_pid, Live *live) {
    char buffer[65536], nonce[65] = {0};
    gint64 generation = 0, sequence = 0;
    for (;;) {
        if(control_stop_requested)return 0;
        live_pump(live);
        int waiting=ipc_wait_control(fd,10);
        if(waiting<0)return 4;
        if(!waiting)continue;
        ssize_t size = ipc_receive_control(fd, buffer, sizeof(buffer), parent_pid);
        if (size == 0) return 0;
        if (size < 0) return 4;
        JsonParser *parser = json_parser_new();
        ParseGuard guard = {.objects=g_ptr_array_new_with_free_func((GDestroyNotify)g_hash_table_unref)};
        g_signal_connect(parser, "object-start", G_CALLBACK(object_start), &guard);
        g_signal_connect(parser, "object-member", G_CALLBACK(object_member), &guard);
        g_signal_connect(parser, "object-end", G_CALLBACK(object_end), &guard);
        gboolean parsed = json_parser_load_from_data(parser, buffer, size, NULL);
        JsonNode *root = parsed ? json_parser_get_root(parser) : NULL;
        JsonObject *object = root && JSON_NODE_HOLDS_OBJECT(root) ? json_node_get_object(root) : NULL;
        gboolean valid = object && !guard.duplicate && json_object_get_size(object) == 6
            && typed(object,"version",G_TYPE_INT64) && typed(object,"generation",G_TYPE_INT64)
            && typed(object,"sequence",G_TYPE_INT64) && typed(object,"nonce",G_TYPE_STRING)
            && typed(object,"kind",G_TYPE_STRING) && json_object_has_member(object,"payload")
            && JSON_NODE_HOLDS_OBJECT(json_object_get_member(object,"payload"));
        if (valid) {
            const char *n = json_object_get_string_member(object,"nonce");
            gint64 gen = json_object_get_int_member(object,"generation");
            valid = json_object_get_int_member(object,"version") == 1 && gen > 0
                && sequence < G_MAXINT64 && json_object_get_int_member(object,"sequence") == sequence + 1
                && strlen(n) == 64 && strspn(n,"0123456789abcdef") == 64;
            if (valid && sequence == 0) { memcpy(nonce,n,65); generation = gen; }
            valid = valid && gen == generation && !strcmp(n,nonce);
        }
        if (!valid) { g_ptr_array_unref(guard.objects); g_object_unref(parser); return 4; }
        const char *kind = json_object_get_string_member(object,"kind");
        gboolean close_requested = !strcmp(kind,"close");
        gboolean handshake = sequence == 0;
        if ((handshake && strcmp(kind,"hello")) || (!handshake && !strcmp(kind,"hello"))
            || ((handshake || close_requested) && json_object_get_size(json_object_get_object_member(object,"payload")) != 0)) {
            g_ptr_array_unref(guard.objects); g_object_unref(parser); return 4;
        }
        ++sequence;
        gboolean ok;
        if (handshake) {
            WaylandProbe probe = {0};
            const char *code;
            if(desktop_backend()==DESKTOP_X11){probe.code="WAYLAND_SESSION_NOT_SELECTED";code=x11_probe_code();}
            else if(desktop_backend()==DESKTOP_UNAVAILABLE){probe.code="DESKTOP_SESSION_UNSUPPORTED";code=probe.code;}
            else {probe=wayland_probe();code=probe.code;}
            ok = reply(fd,nonce,generation,sequence,"unavailable",code,
                       desktop_backend()==DESKTOP_X11?code:"X11_SESSION_NOT_SELECTED",&probe);
        } else if (!close_requested) {
            const char *response_kind;
            int frame_fd = -1;
            JsonNode *payload = live_command(live, kind, json_object_get_object_member(object,"payload"), &response_kind, &frame_fd);
            JsonObject *envelope = json_object_new();
            json_object_set_int_member(envelope,"version",1);
            json_object_set_int_member(envelope,"generation",generation);
            json_object_set_int_member(envelope,"sequence",sequence);
            json_object_set_string_member(envelope,"nonce",nonce);
            json_object_set_string_member(envelope,"kind",response_kind);
            json_object_set_member(envelope,"payload",payload);
            JsonNode *node=json_node_new(JSON_NODE_OBJECT);json_node_take_object(node,envelope);
            JsonGenerator *generator=json_generator_new();json_generator_set_root(generator,node);
            gsize length;char *data=json_generator_to_data(generator,&length);
            ok=ipc_send_frame(fd,data,length,frame_fd);
            if(frame_fd>=0)close(frame_fd);
            g_free(data);g_object_unref(generator);json_node_free(node);
        } else {
            live_close(live);
            ok = reply(fd,nonce,generation,sequence,close_requested ? "closed" : "error",
                       close_requested ? "CLOSED" : "UNSUPPORTED_COMMAND",NULL,NULL);
        }
        g_ptr_array_unref(guard.objects); g_object_unref(parser);
        if (!ok) return 5;
        if (close_requested) return 0;
    }
}
int control_run(int fd,pid_t parent_pid) {
    Live *live=live_new();
    int result=control_loop(fd,parent_pid,live);
    live_free(live);
    return result;
}
#else
int control_run(int fd, pid_t parent_pid) { (void)fd; (void)parent_pid; return 78; }
#endif
