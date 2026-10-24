#define _GNU_SOURCE
#include "portal.h"
#include <assert.h>
#include <gio/gio.h>
#include <gio/gunixfdlist.h>
#include <fcntl.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#define DEST "org.freedesktop.portal.Desktop"
#define ROOT "/org/freedesktop/portal/desktop"
#define SESSION "/org/freedesktop/portal/desktop/session/test/session"
#define RD "org.freedesktop.portal.RemoteDesktop"
#define SC "org.freedesktop.portal.ScreenCast"

static void result_parser(void) {
    const char *valid="{'devices': <uint32 2>, 'streams': <[(uint32 42, {'source_type': <uint32 1>, 'mapping_id': <'monitor-A'>, 'pipewire-serial': <uint64 900>})]>}";
    GVariant *result=g_variant_parse(G_VARIANT_TYPE_VARDICT,valid,NULL,NULL,NULL);
    PortalState state={0};
    assert(result && portal_parse_start(&state,result));
    assert(state.node_id==42 && state.serial==900 && !strcmp(state.mapping_id,"monitor-A"));
    g_free(state.mapping_id);g_variant_unref(result);
    const char *v5="{'devices': <uint32 2>, 'streams': <[(uint32 42, {'source_type': <uint32 1>, 'mapping_id': <'monitor-A'>, 'position': <(-1920, 0)>, 'size': <(1920, 1080)>})]>}";
    memset(&state,0,sizeof(state));
    result=g_variant_parse(G_VARIANT_TYPE_VARDICT,v5,NULL,NULL,NULL);
    assert(result&&portal_parse_start(&state,result)&&state.serial==0);
    assert(state.has_position&&state.has_size&&state.logical_x==-1920&&state.logical_height==1080);
    g_free(state.mapping_id);g_variant_unref(result);
    const char *invalid[]={
        "{'devices': <uint32 2>, 'streams': <[(uint32 42, {'source_type': <uint32 1>, 'mapping_id': <'m'>, 'size': <(0, 100)>})]>}",
        "{'devices': <uint32 0>, 'streams': <[(uint32 42, {'source_type': <uint32 1>, 'mapping_id': <'m'>})]>}",
        "{'devices': <uint32 2>, 'streams': <[(uint32 42, {'source_type': <uint32 2>, 'mapping_id': <'m'>})]>}",
        "{'devices': <uint32 2>, 'streams': <[(uint32 42, {'source_type': <uint32 1>})]>}",
        "{'devices': <uint32 2>, 'streams': <[(uint32 42, {'source_type': <uint32 1>, 'mapping_id': <'a'>}), (uint32 43, {'source_type': <uint32 1>, 'mapping_id': <'b'>})]>}"
    };
    for(unsigned i=0;i<G_N_ELEMENTS(invalid);i++){
        memset(&state,0,sizeof(state));
        result=g_variant_parse(G_VARIANT_TYPE_VARDICT,invalid[i],NULL,NULL,NULL);
        assert(result && !portal_parse_start(&state,result) && state.code);
        if(i==1)assert(!strcmp(state.code,"PORTAL_POINTER_NOT_GRANTED"));
        g_free(state.mapping_id);g_variant_unref(result);
    }
}

typedef struct {
    GDBusConnection *connection;
    GMainContext *context;
    GMainLoop *loop;
    GMutex mutex;
    GCond cond;
    bool initialized;
    unsigned mode, calls, closed;
} FakePortal;

static const char xml[]=
"<node>"
"<interface name='" RD "'>"
"<method name='CreateSession'><arg type='a{sv}' direction='in'/><arg type='o' direction='out'/></method>"
"<method name='SelectDevices'><arg type='o' direction='in'/><arg type='a{sv}' direction='in'/><arg type='o' direction='out'/></method>"
"<method name='Start'><arg type='o' direction='in'/><arg type='s' direction='in'/><arg type='a{sv}' direction='in'/><arg type='o' direction='out'/></method>"
"<method name='ConnectToEIS'><arg type='o' direction='in'/><arg type='a{sv}' direction='in'/><arg type='h' direction='out'/></method>"
"</interface><interface name='" SC "'>"
"<method name='SelectSources'><arg type='o' direction='in'/><arg type='a{sv}' direction='in'/><arg type='o' direction='out'/></method>"
"<method name='OpenPipeWireRemote'><arg type='o' direction='in'/><arg type='a{sv}' direction='in'/><arg type='h' direction='out'/></method>"
"</interface><interface name='org.freedesktop.portal.Session'><method name='Close'/></interface>"
"</node>";

static void method(GDBusConnection *bus,const char *sender,const char *path,
                   const char *interface,const char *name,GVariant *args,
                   GDBusMethodInvocation *invocation,void *data) {
    (void)path;(void)interface;
    FakePortal *fake=data;
    if(!strcmp(name,"Close")){
        fake->closed++;
        g_dbus_method_invocation_return_value(invocation,NULL);
        return;
    }
    static const char *ordered[]={"CreateSession","SelectDevices","SelectSources","Start","OpenPipeWireRemote","ConnectToEIS"};
    assert(fake->calls<G_N_ELEMENTS(ordered) && !strcmp(name,ordered[fake->calls++]));
    bool create=!strcmp(name,"CreateSession"),start=!strcmp(name,"Start");
    GVariant *options=g_variant_get_child_value(args,create?0:start?2:1);
    if(!create){
        GVariant *session=g_variant_get_child_value(args,0);
        assert(!strcmp(g_variant_get_string(session,NULL),SESSION));
        g_variant_unref(session);
    }
    if(!strcmp(name,"SelectDevices")||!strcmp(name,"SelectSources")){
        guint32 types=0;
        assert(g_variant_lookup(options,"types","u",&types));
        assert(types==(!strcmp(name,"SelectDevices")?2u:1u));
    }
    if(!strcmp(name,"OpenPipeWireRemote")||!strcmp(name,"ConnectToEIS")){
        int pair[2];assert(socketpair(AF_UNIX,SOCK_STREAM,0,pair)==0);
        GUnixFDList *list=g_unix_fd_list_new();
        int index=g_unix_fd_list_append(list,pair[0],NULL);assert(index==0);
        g_dbus_method_invocation_return_value_with_unix_fd_list(invocation,g_variant_new("(h)",index),list);
        close(pair[0]);close(pair[1]);g_object_unref(list);g_variant_unref(options);return;
    }
    const char *token=NULL;assert(g_variant_lookup(options,"handle_token","&s",&token));
    char *unique=g_strdup(sender+1);for(char *p=unique;*p;p++)if(*p=='.')*p='_';
    char *request=g_strdup_printf("/org/freedesktop/portal/desktop/request/%s/%s",unique,token);
    GVariantBuilder result;g_variant_builder_init(&result,G_VARIANT_TYPE_VARDICT);
    if(create)g_variant_builder_add(&result,"{sv}","session_handle",g_variant_new_string(SESSION));
    if(start){
        g_variant_builder_add(&result,"{sv}","devices",g_variant_new_uint32(2));
        GVariant *streams=g_variant_parse(G_VARIANT_TYPE("a(ua{sv})"),"[(uint32 42, {'source_type': <uint32 1>, 'mapping_id': <'monitor-A'>})]",NULL,NULL,NULL);
        g_variant_builder_add(&result,"{sv}","streams",streams);g_variant_unref(streams);
    }
    /* Emit before method return: catches subscription-after-call races. */
    if(fake->mode!=2){
        guint response=fake->mode==1&&start?1:0;
        assert(g_dbus_connection_emit_signal(bus,sender,request,"org.freedesktop.portal.Request","Response",g_variant_new("(ua{sv})",response,&result),NULL));
    }else g_variant_builder_clear(&result);
    g_dbus_method_invocation_return_value(invocation,g_variant_new("(o)",request));
    g_free(request);g_free(unique);g_variant_unref(options);
}
static const GDBusInterfaceVTable vtable={.method_call=method};
static void *serve(void *data){
    FakePortal *fake=data;
    fake->context=g_main_context_new();g_main_context_push_thread_default(fake->context);
    fake->connection=g_dbus_connection_new_for_address_sync(g_getenv("DBUS_SESSION_BUS_ADDRESS"),G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT|G_DBUS_CONNECTION_FLAGS_MESSAGE_BUS_CONNECTION,NULL,NULL,NULL);
    assert(fake->connection);
    GVariant *r=g_dbus_connection_call_sync(fake->connection,"org.freedesktop.DBus","/org/freedesktop/DBus","org.freedesktop.DBus","RequestName",g_variant_new("(su)",DEST,0),NULL,G_DBUS_CALL_FLAGS_NONE,1000,NULL,NULL);assert(r);g_variant_unref(r);
    GDBusNodeInfo *info=g_dbus_node_info_new_for_xml(xml,NULL);assert(info);
    assert(g_dbus_connection_register_object(fake->connection,ROOT,info->interfaces[0],&vtable,fake,NULL,NULL));
    assert(g_dbus_connection_register_object(fake->connection,ROOT,info->interfaces[1],&vtable,fake,NULL,NULL));
    assert(g_dbus_connection_register_object(fake->connection,SESSION,info->interfaces[2],&vtable,fake,NULL,NULL));
    fake->loop=g_main_loop_new(fake->context,FALSE);
    g_mutex_lock(&fake->mutex);fake->initialized=true;g_cond_signal(&fake->cond);g_mutex_unlock(&fake->mutex);
    g_main_loop_run(fake->loop);
    g_dbus_connection_close_sync(fake->connection,NULL,NULL);g_object_unref(fake->connection);
    g_main_loop_unref(fake->loop);g_dbus_node_info_unref(info);
    g_main_context_pop_thread_default(fake->context);g_main_context_unref(fake->context);return NULL;
}
int main(int argc,char **argv){
    result_parser();
    GError *timeout=g_error_new_literal(G_IO_ERROR,G_IO_ERROR_TIMED_OUT,"fixture");
    assert(!strcmp(portal_call_failure(timeout),"PORTAL_REQUEST_TIMEOUT"));g_error_free(timeout);
    timeout=g_error_new_literal(G_DBUS_ERROR,G_DBUS_ERROR_NO_REPLY,"fixture");
    assert(!strcmp(portal_call_failure(timeout),"PORTAL_REQUEST_TIMEOUT"));g_error_free(timeout);
    GError *denied=g_error_new_literal(G_DBUS_ERROR,G_DBUS_ERROR_ACCESS_DENIED,"fixture");
    assert(!strcmp(portal_call_failure(denied),"PORTAL_METHOD_FAILED"));g_error_free(denied);
    if(argc==1)return 0;
    /* Only run under the private dbus-run-session test wrapper. */
    assert(!strcmp(argv[1],"private-bus"));
    FakePortal fake={0};
    g_mutex_init(&fake.mutex);g_cond_init(&fake.cond);
    GThread *thread=g_thread_new("fake-portal",serve,&fake);
    g_mutex_lock(&fake.mutex);while(!fake.initialized)g_cond_wait(&fake.cond,&fake.mutex);g_mutex_unlock(&fake.mutex);
    PortalState state={0};
    assert(portal_open(&state,1000));assert(state.ready&&fake.calls==6);
    assert(fcntl(state.pipewire_fd,F_GETFD)>=0&&fcntl(state.eis_fd,F_GETFD)>=0);
    unsigned long generation=state.generation;
    assert(g_dbus_connection_emit_signal(fake.connection,NULL,SESSION,"org.freedesktop.portal.Session","Closed",g_variant_new("(a{sv})",NULL),NULL));
    g_dbus_connection_flush_sync(fake.connection,NULL,NULL);
    gint64 end=g_get_monotonic_time()+1000000;
    while(state.ready&&g_get_monotonic_time()<end){portal_pump(&state);g_usleep(1000);}
    assert(!state.ready&&state.generation>generation&&!strcmp(state.code,"PORTAL_REVOKED"));
    portal_close(&state);
    fake.calls=0;fake.mode=1;
    assert(!portal_open(&state,1000)&&!strcmp(state.code,"PORTAL_CANCELLED"));
    fake.calls=0;fake.mode=2;
    gint64 begin=g_get_monotonic_time();
    assert(!portal_open(&state,100)&&!strcmp(state.code,"PORTAL_REQUEST_TIMEOUT"));
    assert(g_get_monotonic_time()-begin<500000);
    g_main_loop_quit(fake.loop);g_thread_join(thread);
    portal_close(&state);g_cond_clear(&fake.cond);g_mutex_clear(&fake.mutex);
    return 0;
}
