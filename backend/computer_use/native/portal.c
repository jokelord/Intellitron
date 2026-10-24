#include "portal.h"
#include <unistd.h>
#ifdef HAVE_GIO_PIPEWIRE_DEPS
#include <gio/gio.h>
#include <gio/gunixfdlist.h>
#include <string.h>
#define DEST "org.freedesktop.portal.Desktop"
#define PATH "/org/freedesktop/portal/desktop"
#define RD "org.freedesktop.portal.RemoteDesktop"
#define SC "org.freedesktop.portal.ScreenCast"
typedef struct {
    GVariant *result;
    guint response;
    gboolean done;
} Pending;
static void response_cb(GDBusConnection *c,const gchar *sender,const gchar *path,const gchar *iface,const gchar *signal,GVariant *args,gpointer data) {
    (void)c;
    (void)sender;
    (void)path;
    (void)iface;
    (void)signal;
    Pending *p=data;
    g_variant_get(args,"(u@a{sv})",&p->response,&p->result);
    p->done=TRUE;
}
static void revoked(GDBusConnection *c,const gchar *sender,const gchar *path,const gchar *iface,const gchar *signal,GVariant *args,gpointer data) {
    (void)c;
    (void)sender;
    (void)path;
    (void)iface;
    (void)signal;
    (void)args;
    PortalState *s=data;
    s->ready=false;
    s->generation++;
    s->code="PORTAL_REVOKED";
}
static void owner_changed(GDBusConnection *c,const gchar *sender,const gchar *path,const gchar *iface,const gchar *signal,GVariant *args,gpointer data) {
    const char *name,*old,*current;
    g_variant_get(args,"(&s&s&s)",&name,&old,&current);
    (void)name;
    (void)current;
    if (*old) revoked(c,sender,path,iface,signal,args,data);
}
const char *portal_call_failure(void *error){
    GError *e=error;
    return e&&(g_error_matches(e,G_IO_ERROR,G_IO_ERROR_TIMED_OUT)||
               g_error_matches(e,G_DBUS_ERROR,G_DBUS_ERROR_TIMEOUT)||
               g_error_matches(e,G_DBUS_ERROR,G_DBUS_ERROR_NO_REPLY))
        ? "PORTAL_REQUEST_TIMEOUT" : "PORTAL_METHOD_FAILED";
}
static GVariant *request(PortalState *s,const char *iface,const char *method,GVariantBuilder *options,bool create,bool start,unsigned timeout) {
    int remaining=(int)((s->deadline_us-g_get_monotonic_time()+999)/1000);
    if(remaining<=0){g_variant_builder_clear(options);s->code="PORTAL_REQUEST_TIMEOUT";return NULL;}
    timeout=(unsigned)remaining;
    char *token=g_strdup_printf("cu_%u_%u",g_random_int(),g_random_int());
    char *sender=g_strdup(g_dbus_connection_get_unique_name(s->bus)+1);
    for(char *p=sender;*p;p++) if(*p=='.') *p='_';
    char *path=g_strdup_printf("/org/freedesktop/portal/desktop/request/%s/%s",sender,token);
    g_variant_builder_add(options,"{sv}","handle_token",g_variant_new_string(token));
    Pending pending={
        0
    };
    guint subscription=g_dbus_connection_signal_subscribe(s->bus,DEST,"org.freedesktop.portal.Request","Response",path,NULL,G_DBUS_SIGNAL_FLAGS_NONE,response_cb,&pending,NULL);
    GVariant *args=create ? g_variant_new("(a{sv})",options) : start ? g_variant_new("(osa{sv})",s->session,"",options) : g_variant_new("(oa{sv})",s->session,options);
    GError *call_error=NULL;
    GVariant *reply=g_dbus_connection_call_sync(s->bus,DEST,PATH,iface,method,args,G_VARIANT_TYPE("(o)"),G_DBUS_CALL_FLAGS_NONE,(gint)timeout,NULL,&call_error);
    if(reply) {
        const char *actual;
        g_variant_get(reply,"(&o)",&actual);
        /* Modern handle-token contract is mandatory to avoid missed signals. */
        if(strcmp(actual,path)) s->code="PORTAL_REQUEST_HANDLE_MISMATCH";
        else {
            gint64 end=s->deadline_us;
            while(!pending.done && g_get_monotonic_time()<end && s->code==NULL) {
                while(g_main_context_iteration(NULL,FALSE));
                if(!pending.done) g_usleep(1000);
            }
        }
        g_variant_unref(reply);
    }
    if(!pending.done) {
        g_dbus_connection_call(s->bus,DEST,path,"org.freedesktop.portal.Request","Close",NULL,NULL,G_DBUS_CALL_FLAGS_NONE,1000,NULL,NULL,NULL);
        if(!s->code) s->code=reply ? "PORTAL_REQUEST_TIMEOUT":portal_call_failure(call_error);
    } else if(pending.response!=0) s->code=pending.response==1 ? "PORTAL_CANCELLED":"PORTAL_DENIED";
    /* Subscribe/unsubscribe and dispatch share this main context: callbacks
     * cannot outlive the stack-owned Pending after unsubscription. */
    g_dbus_connection_signal_unsubscribe(s->bus,subscription);
    g_free(path);
    g_free(sender);
    g_free(token);
    g_clear_error(&call_error);
    if(s->code) {
        if(pending.result)g_variant_unref(pending.result);
        return NULL;
    }
    return pending.result;
}
static int get_fd(PortalState *s,const char *iface,const char *method,unsigned timeout) {
    int remaining=(int)((s->deadline_us-g_get_monotonic_time()+999)/1000);
    if(remaining<=0){s->code="PORTAL_REQUEST_TIMEOUT";return -1;}
    timeout=(unsigned)remaining;
    GUnixFDList *list=NULL;
    GError *call_error=NULL;
    GVariant *reply=g_dbus_connection_call_with_unix_fd_list_sync(s->bus,DEST,PATH,iface,method,         g_variant_new("(oa{sv})",s->session,NULL),G_VARIANT_TYPE("(h)"),G_DBUS_CALL_FLAGS_NONE,(gint)timeout,NULL,&list,NULL,&call_error);
    int result=-1,index;
    if(reply&&list) {
        g_variant_get(reply,"(h)",&index);
        result=g_unix_fd_list_get(list,index,NULL);
    }
    if(!reply)s->code=portal_call_failure(call_error);
    g_clear_error(&call_error);
    if(reply)g_variant_unref(reply);
    if(list)g_object_unref(list);
    return result;
}
bool portal_parse_start(PortalState *s,void *result) {
    GVariant *r=result;
    guint32 devices=0;
    g_variant_lookup(r,"devices","u",&devices);
    if(!(devices&2)){
        s->code="PORTAL_POINTER_NOT_GRANTED";
        return false;
    }
    GVariant *streams=g_variant_lookup_value(r,"streams",G_VARIANT_TYPE("a(ua{sv})"));
    if(!streams||g_variant_n_children(streams)!=1){
        s->code="PORTAL_INVALID_STREAM";
        if(streams)g_variant_unref(streams);
        return false;
    }
    GVariant *stream=g_variant_get_child_value(streams,0),*properties;
    g_variant_get(stream,"(u@a{sv})",&s->node_id,&properties);
    guint32 source=0;
    g_variant_lookup(properties,"source_type","u",&source);
    g_variant_lookup(properties,"mapping_id","s",&s->mapping_id);
    g_variant_lookup(properties,"pipewire-serial","t",&s->serial);
    /* Portal compositor coordinates are evidence about the selected monitor,
     * not EIS coordinates. mapping_id joins the two coordinate spaces. */
    GVariant *position=g_variant_lookup_value(properties,"position",NULL);
    GVariant *size=g_variant_lookup_value(properties,"size",NULL);
    bool geometry_valid=(!position||g_variant_is_of_type(position,G_VARIANT_TYPE("(ii)"))) &&
                        (!size||g_variant_is_of_type(size,G_VARIANT_TYPE("(ii)")));
    if(geometry_valid){
        if(position){g_variant_get(position,"(ii)",&s->logical_x,&s->logical_y);s->has_position=true;}
        if(size){g_variant_get(size,"(ii)",&s->logical_width,&s->logical_height);s->has_size=true;
            geometry_valid=s->logical_width>0&&s->logical_height>0;}
    }
    if(position)g_variant_unref(position);
    if(size)g_variant_unref(size);
    g_variant_unref(properties);
    g_variant_unref(stream);
    g_variant_unref(streams);
    if(!geometry_valid||source!=1||s->node_id==UINT32_MAX||!s->mapping_id||!*s->mapping_id||strlen(s->mapping_id)>256){
        s->code="PORTAL_MAPPING_UNPROVEN";
        return false;
    }
    return true;
}
bool portal_open(PortalState *s,unsigned timeout) {
    if(!s || timeout<1 || timeout>300000) return false;
    memset(s,0,sizeof(*s));
    s->pipewire_fd=s->eis_fd=-1;
    s->generation=1;
    s->deadline_us=g_get_monotonic_time()+(int64_t)timeout*1000;
    s->bus=g_bus_get_sync(G_BUS_TYPE_SESSION,NULL,NULL);
    if(!s->bus){
        s->code="SESSION_BUS_UNAVAILABLE";
        return false;
    }
    s->owner_subscription=g_dbus_connection_signal_subscribe(s->bus,"org.freedesktop.DBus","org.freedesktop.DBus","NameOwnerChanged","/org/freedesktop/DBus",DEST,G_DBUS_SIGNAL_FLAGS_NONE,owner_changed,s,NULL);
    GVariantBuilder options;
    g_variant_builder_init(&options,G_VARIANT_TYPE_VARDICT);
    char *session_token=g_strdup_printf("cu_%u",g_random_int());
    g_variant_builder_add(&options,"{sv}","session_handle_token",g_variant_new_string(session_token));
    g_free(session_token);
    s->phase=PORTAL_CREATING;
    GVariant *r=request(s,RD,"CreateSession",&options,true,false,timeout);
    if(!r)goto fail;
    g_variant_lookup(r,"session_handle","s",&s->session);
    g_variant_unref(r);
    if(!s->session||!g_variant_is_object_path(s->session)){
        s->code="PORTAL_INVALID_SESSION";
        goto fail;
    }
    s->closed_subscription=g_dbus_connection_signal_subscribe(s->bus,DEST,"org.freedesktop.portal.Session","Closed",s->session,NULL,G_DBUS_SIGNAL_FLAGS_NONE,revoked,s,NULL);
    g_variant_builder_init(&options,G_VARIANT_TYPE_VARDICT);
    g_variant_builder_add(&options,"{sv}","types",g_variant_new_uint32(2));
    s->phase=PORTAL_DEVICES;
    r=request(s,RD,"SelectDevices",&options,false,false,timeout);
    if(!r)goto fail;
    g_variant_unref(r);
    g_variant_builder_init(&options,G_VARIANT_TYPE_VARDICT);
    g_variant_builder_add(&options,"{sv}","types",g_variant_new_uint32(1));
    g_variant_builder_add(&options,"{sv}","multiple",g_variant_new_boolean(FALSE));
    s->phase=PORTAL_SOURCES;
    r=request(s,SC,"SelectSources",&options,false,false,timeout);
    if(!r)goto fail;
    g_variant_unref(r);
    g_variant_builder_init(&options,G_VARIANT_TYPE_VARDICT);
    s->phase=PORTAL_STARTING;
    r=request(s,RD,"Start",&options,false,true,timeout);
    if(!r)goto fail;
    bool parsed=portal_parse_start(s,r);
    g_variant_unref(r);
    if(!parsed)goto fail;
    s->phase=PORTAL_STREAM;
    s->pipewire_fd=get_fd(s,SC,"OpenPipeWireRemote",timeout);
    if(s->pipewire_fd<0){
        if(!s->code)s->code="PORTAL_PIPEWIRE_UNAVAILABLE";
        goto fail;
    }
    s->eis_fd=get_fd(s,RD,"ConnectToEIS",timeout);
    if(s->eis_fd<0){
        if(!s->code)s->code="PORTAL_EIS_UNAVAILABLE";
        goto fail;
    }
    portal_pump(s);
    if(s->code)goto fail;
    s->phase=PORTAL_EIS;
    s->ready=true;
    return true;
    fail: portal_close(s);
    return false;
}
void portal_pump(PortalState *s) {
    if(s&&s->bus){
        while(g_main_context_iteration(NULL,FALSE));
        if(g_dbus_connection_is_closed(s->bus)){
            s->ready=false;
            s->code="PORTAL_REVOKED";
        }
    }
}
void portal_close(PortalState *s) {
    if(!s)return;
    if(s->bus){
        if(s->closed_subscription)g_dbus_connection_signal_unsubscribe(s->bus,s->closed_subscription);
        if(s->owner_subscription)g_dbus_connection_signal_unsubscribe(s->bus,s->owner_subscription);
        if(s->session&&!g_dbus_connection_is_closed(s->bus)){
            GVariant *closed=g_dbus_connection_call_sync(s->bus,DEST,s->session,"org.freedesktop.portal.Session","Close",NULL,NULL,G_DBUS_CALL_FLAGS_NONE,1000,NULL,NULL);
            if(closed)g_variant_unref(closed);
        }
        g_object_unref(s->bus);
    }
    if(s->pipewire_fd>=0&&s->phase!=PORTAL_CLOSED)close(s->pipewire_fd);
    if(s->eis_fd>=0&&s->phase!=PORTAL_CLOSED)close(s->eis_fd);
    g_free(s->session);
    g_free(s->mapping_id);
    s->session=s->mapping_id=NULL;
    s->bus=NULL;
    s->pipewire_fd=s->eis_fd=-1;
    s->ready=false;
    s->phase=PORTAL_CLOSED;
    s->generation++;
}
#else
const char *portal_call_failure(void *error){(void)error;return "PORTAL_METHOD_FAILED";}
bool portal_parse_start(PortalState *s,void *result){(void)s;(void)result;return false;}
bool portal_open(PortalState *s,unsigned timeout){
    (void)timeout;
    if(s)s->code="GIO_PIPEWIRE_DEVELOPMENT_DEPENDENCIES_MISSING";
    return false;
}
void portal_pump(PortalState *s){
    (void)s;
}
void portal_close(PortalState *s){
    if(s){
        s->ready=false;
        s->phase=PORTAL_CLOSED;
        s->generation++;
    }
}
#endif
/* Legacy readiness query never requests authorization. */
bool portal_start(PortalState *s){
    return s&&s->ready;
}
