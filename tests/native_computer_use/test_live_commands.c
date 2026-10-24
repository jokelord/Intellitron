#include <assert.h>
#include <string.h>
#include "live.h"

static void check(Live *live, const char *command, const char *json,
                  const char *expected_kind, const char *expected_code) {
    JsonParser *parser=json_parser_new();
    assert(json_parser_load_from_data(parser,json,-1,NULL));
    JsonObject *payload=json_node_get_object(json_parser_get_root(parser));
    const char *kind=NULL;
    int fd=123;
    JsonNode *result=live_command(live,command,payload,&kind,&fd);
    assert(result && fd==-1);
    assert(!strcmp(kind,expected_kind));
    assert(!strcmp(json_object_get_string_member(json_node_get_object(result),"code"),expected_code));
    json_node_free(result);
    g_object_unref(parser);
}
#define START_BASE "\"binding_id\":\"b\",\"owner_session_id\":\"owner\",\"context_nonce\":\"context\",\"authorization_timeout_ms\":100,\"max_frame_bytes\":1024"
#define START START_BASE ",\"startup_timeout_ms\":100"
int main(void) {
    /* No test is allowed to request a real Portal dialog. Admission failures
     * below execute the production schema and reject before portal_open. */
    g_setenv("XDG_SESSION_TYPE","wayland",TRUE);
    g_setenv("DBUS_SESSION_BUS_ADDRESS","unix:path=/nonexistent/computer-use-test-bus",TRUE);
    Live *live=live_new();
    check(live,"status","{}","unavailable","NATIVE_SESSION_NOT_STARTED");
    check(live,"start","{" START ",\"allow_input\":false}","error","DESKTOP_INPUT_AUTHORITY_REQUIRED");
    check(live,"start","{" START_BASE ",\"allow_input\":false}","error","INVALID_START");
    check(live,"start","{" START_BASE ",\"startup_timeout_ms\":0,\"allow_input\":false}","error","INVALID_START");
    check(live,"start","{" START_BASE ",\"startup_timeout_ms\":300001,\"allow_input\":false}","error","INVALID_START");
    check(live,"start","{" START_BASE ",\"startup_timeout_ms\":1.5,\"allow_input\":false}","error","INVALID_START");
    check(live,"start","{" START_BASE ",\"startup_timeout_ms\":1,\"allow_input\":false}","error","DESKTOP_INPUT_AUTHORITY_REQUIRED");
    check(live,"start","{" START_BASE ",\"startup_timeout_ms\":300000,\"allow_input\":false}","error","DESKTOP_INPUT_AUTHORITY_REQUIRED");
    check(live,"start","{" START ",\"allow_input\":1}","error","INVALID_START");
    check(live,"start","{" START ",\"allow_input\":false,\"unknown\":1}","error","INVALID_START");
    check(live,"start","{}","error","INVALID_START");
    check(live,"freeze","{\"binding_id\":\"other\",\"timeout_ms\":100}","error","INVALID_FREEZE");
    check(live,"send_event","{}","error","INVALID_EVENT");
    check(live,"status","{\"unknown\":1}","error","INVALID_STATUS");
    check(live,"not_a_command","{}","error","UNSUPPORTED_COMMAND");
    check(live,"start","{" START ",\"allow_input\":true}","error","SESSION_BUS_UNAVAILABLE");
    check(live,"start","{" START ",\"allow_input\":true}","error","HELPER_ALREADY_STARTED");
    live_free(live);
    return 0;
}
