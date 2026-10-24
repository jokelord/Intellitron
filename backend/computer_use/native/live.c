#define _GNU_SOURCE
#include "live.h"
#ifdef HAVE_JSON_GLIB
#include "portal.h"
#include "capture.h"
#include "ei_input.h"
#include "x11.h"
#include <sys/mman.h>
#include <fcntl.h>
#include <unistd.h>
#include <math.h>
#include <string.h>
struct Live {
    PortalState portal;
    Capture *capture;
    NativeInput *input;
    X11Session *x11;
    X11Geometry x11_geometry;
    DesktopBackend backend;
    char *binding,*owner,*context;
    uint32_t x,y,w,h;
    bool started;
    uint64_t last_sequence;
    const char *failure;
};
Live *live_new(void){
    Live *l=g_new0(Live,1);
    l->portal.pipewire_fd=l->portal.eis_fd=-1;
    return l;
}
void live_close(Live *l){
    if(!l)return;
    /* After revocation never manufacture a cleanup event on a stale device. */
    if(!l->portal.ready)input_invalidate(l->input);
    input_close(l->input);l->input=NULL;
    x11_close(l->x11);l->x11=NULL;
    capture_close(l->capture);l->capture=NULL;
    portal_close(&l->portal);l->started=false;
}
void live_free(Live *l){
    if(!l)return;
    live_close(l);
    g_free(l->binding);
    g_free(l->owner);
    g_free(l->context);
    g_free(l);
}
void live_pump(Live *l){
    if(l){
        if(l->backend==DESKTOP_X11){
            x11_pump(l->x11);
            if(l->started&&!l->failure&&x11_error(l->x11)){l->failure=x11_error(l->x11);live_close(l);}
            return;
        }
        portal_pump(&l->portal);
        input_pump(l->input);
        if(l->started&&!l->failure){
            const char *failure=l->portal.code;
            if(!failure)failure=capture_error(l->capture);
            if(!failure)failure=input_error(l->input);
            if(failure){l->failure=failure;input_invalidate(l->input);live_close(l);}
        }
    }
}
static bool typed(JsonObject *o,const char *key,GType type){
    JsonNode *n=json_object_get_member(o,key);
    return n&&JSON_NODE_HOLDS_VALUE(n)&&json_node_get_value_type(n)==type;
}
static bool number(JsonObject *o,const char *key){
    return (typed(o,key,G_TYPE_DOUBLE)||typed(o,key,G_TYPE_INT64))&&isfinite(json_object_get_double_member(o,key));
}
static bool string(JsonObject *o,const char *key){
    return typed(o,key,G_TYPE_STRING)&&strlen(json_object_get_string_member(o,key))>0&&strlen(json_object_get_string_member(o,key))<=256;
}
static JsonNode *error(const char *code,const char **kind){
    *kind="error";
    JsonObject *o=json_object_new();
    json_object_set_string_member(o,"code",code);
    JsonNode *n=json_node_new(JSON_NODE_OBJECT);
    json_node_take_object(n,o);
    return n;
}
static bool commit_input(void *input){return input_commit(input);}
static bool ready(Live *l){
    live_pump(l);
    if(l->backend==DESKTOP_X11){
        if(!l->started||!x11_geometry(l->x11,&l->x11_geometry))return false;
        l->x=l->x11_geometry.x;l->y=l->x11_geometry.y;l->w=l->x11_geometry.width;l->h=l->x11_geometry.height;
        return true;
    }
    return l->started&&l->portal.ready&&!capture_error(l->capture)&&input_region(l->input,&l->x,&l->y,&l->w,&l->h);
}
static JsonObject *binding(Live *l){
    JsonObject *o=json_object_new();
    json_object_set_string_member(o,"binding_id",l->binding);
    json_object_set_string_member(o,"session_id",l->owner);
    json_object_set_int_member(o,"geometry_version",1);
    json_object_set_string_member(o,"backend",l->backend==DESKTOP_X11?"x11":"wayland");
    JsonArray *origin=json_array_new(),*size=json_array_new();
    json_array_add_int_element(origin,l->x);
    json_array_add_int_element(origin,l->y);
    json_array_add_int_element(size,l->w);
    json_array_add_int_element(size,l->h);
    json_object_set_array_member(o,"origin",origin);
    json_object_set_array_member(o,"size",size);
    json_object_set_int_member(o,"stream_generation",l->backend==DESKTOP_X11?1:l->portal.generation);
    json_object_set_string_member(o,"device_id",l->binding);
    JsonObject *e=json_object_new();
    json_object_set_boolean_member(e,"full_monitor",true);
    if(l->backend==DESKTOP_X11){
        X11Geometry *g=&l->x11_geometry;
        char *region=g_strdup_printf("xrandr:%lu:%lu",g->root,g->monitor);
        json_object_set_string_member(o,"region_id",region);
        json_object_set_string_member(e,"mapping_id",region);
        json_object_set_string_member(e,"source","xrandr");
        json_object_set_string_member(e,"coordinate_space","x11_root");
        json_object_set_int_member(e,"root_id",g->root);
        json_object_set_int_member(e,"monitor_atom",g->monitor);
        json_object_set_string_member(e,"monitor_name",g->monitor_name);
        JsonArray *outputs=json_array_new();
        for(unsigned n=0;n<g->output_count;n++)json_array_add_int_element(outputs,g->outputs[n]);
        json_object_set_array_member(e,"output_ids",outputs);
        g_free(region);
    }else{
        json_object_set_string_member(o,"region_id",l->portal.mapping_id);
        json_object_set_string_member(e,"mapping_id",l->portal.mapping_id);
        if(l->portal.has_position){
            JsonArray *position=json_array_new();
            json_array_add_int_element(position,l->portal.logical_x);
            json_array_add_int_element(position,l->portal.logical_y);
            json_object_set_array_member(e,"portal_position",position);
        }
        if(l->portal.has_size){
            JsonArray *logical_size=json_array_new();
            json_array_add_int_element(logical_size,l->portal.logical_width);
            json_array_add_int_element(logical_size,l->portal.logical_height);
            json_object_set_array_member(e,"portal_size",logical_size);
        }
    }
    json_object_set_object_member(o,"mapping_evidence",e);
    json_object_set_boolean_member(o,"ready",true);
    return o;
}
static const char *failure_code(Live *l){
    if(l->failure)return l->failure;
    if(l->backend==DESKTOP_X11)return x11_error(l->x11);
    if(l->portal.code)return l->portal.code;
    const char *c=capture_error(l->capture);
    if(c)return c;
    c=input_error(l->input);
    return c;
}
static const char *reason(Live *l){
    const char *failure=failure_code(l);
    return failure?failure:"INPUT_DEVICE_NOT_READY";
}
JsonNode *live_command(Live *l,const char *command,JsonObject *p,const char **kind,int *fd){
    *fd=-1;
    JsonObject *out=NULL;
    if(!strcmp(command,"start")) {
        if(l->started||l->binding)return error("HELPER_ALREADY_STARTED",kind);
        if(json_object_get_size(p)!=7||!string(p,"binding_id")||!string(p,"owner_session_id")||!string(p,"context_nonce")||!typed(p,"allow_input",G_TYPE_BOOLEAN)||!typed(p,"authorization_timeout_ms",G_TYPE_INT64)||!typed(p,"startup_timeout_ms",G_TYPE_INT64)||!typed(p,"max_frame_bytes",G_TYPE_INT64))return error("INVALID_START",kind);
        gint64 timeout=json_object_get_int_member(p,"authorization_timeout_ms"),max=json_object_get_int_member(p,"max_frame_bytes"),startup=json_object_get_int_member(p,"startup_timeout_ms");
        if(timeout<1||timeout>300000||startup<1||startup>300000||max<3||max>268435456)return error("INVALID_START",kind);
        if(!json_object_get_boolean_member(p,"allow_input"))return error("DESKTOP_INPUT_AUTHORITY_REQUIRED",kind);
        l->binding=g_strdup(json_object_get_string_member(p,"binding_id"));
        l->owner=g_strdup(json_object_get_string_member(p,"owner_session_id"));
        l->context=g_strdup(json_object_get_string_member(p,"context_nonce"));
        l->backend=desktop_backend();
        if(l->backend==DESKTOP_UNAVAILABLE)return error("DESKTOP_SESSION_UNSUPPORTED",kind);
        if(l->backend==DESKTOP_X11){
            gint64 end=g_get_monotonic_time()+startup*1000;
            l->x11=x11_open((size_t)max);
            l->started=true;
            CaptureFrame frame={0};
            if(!ready(l)||!x11_snapshot(l->x11,&frame)){
                l->failure=reason(l);live_close(l);return error(l->failure,kind);
            }
            capture_frame_clear(&frame);
            if(g_get_monotonic_time()>=end){
                l->failure="NATIVE_STARTUP_RENDEZVOUS_TIMEOUT";live_close(l);return error(l->failure,kind);
            }
        }else{
        if(!portal_open(&l->portal,(unsigned)timeout))return error(reason(l),kind);
        gint64 end=g_get_monotonic_time()+startup*1000;
        int pwfd=l->portal.pipewire_fd;
        l->portal.pipewire_fd=-1;
        l->capture=capture_open(pwfd,l->portal.node_id,l->portal.serial,(size_t)max);
        int eifd=l->portal.eis_fd;
        l->portal.eis_fd=-1;
        l->input=input_open(eifd,l->portal.mapping_id);
        l->started=true;
        CaptureFrame frame={
            0
        };
        bool committed=false;
        while(g_get_monotonic_time()<end){
            bool input_ready=ready(l);
            if(input_ready&&capture_snapshot(l->capture,&frame)){
                committed=capture_commit(l->capture,&frame,commit_input,l->input);
                capture_frame_clear(&frame);
                if(committed)break;
            }
            if(l->portal.code||capture_error(l->capture)||input_error(l->input))break;
            g_usleep(1000);
        }
        if(!committed||g_get_monotonic_time()>=end){
            const char *failure=failure_code(l);
            if(!failure)failure=input_startup_status(l->input);
            if(!failure)failure=capture_startup_status(l->capture);
            if(!failure)failure="NATIVE_STARTUP_RENDEZVOUS_TIMEOUT";
            l->failure=failure;
            live_close(l);
            return error(failure,kind);
        }
        if(!ready(l)||!capture_snapshot(l->capture,&frame))return error(reason(l),kind);
        capture_frame_clear(&frame);
        }
        *kind="ready";
        out=json_object_new();
        json_object_set_object_member(out,"binding",binding(l));
    } else if(!strcmp(command,"status")) {
        if(json_object_get_size(p))return error("INVALID_STATUS",kind);
        if(l->failure)return error(l->failure,kind);
        if(!l->started){
            JsonNode *n=error("NATIVE_SESSION_NOT_STARTED",kind);
            *kind="unavailable";
            return n;
        }
        if(!ready(l))return error(reason(l),kind);
        *kind="binding";
        out=json_object_new();
        json_object_set_object_member(out,"binding",binding(l));
    } else if(!strcmp(command,"freeze")) {
        if(json_object_get_size(p)!=2||!string(p,"binding_id")||!typed(p,"timeout_ms",G_TYPE_INT64)||!l->binding||strcmp(l->binding,json_object_get_string_member(p,"binding_id")))return error("INVALID_FREEZE",kind);
        gint64 timeout=json_object_get_int_member(p,"timeout_ms");
        if(timeout<1||timeout>300000)return error("INVALID_FREEZE",kind);
        CaptureFrame frame={
            0
        };
        if(l->backend==DESKTOP_X11){
            gint64 end=g_get_monotonic_time()+timeout*1000;
            if(!ready(l)||!x11_snapshot(l->x11,&frame))return error(reason(l),kind);
            if(!ready(l)){capture_frame_clear(&frame);return error(reason(l),kind);}
            if(g_get_monotonic_time()>=end){capture_frame_clear(&frame);return error("FRAME_TIMEOUT",kind);}
        }else{
        CaptureProgress progress=capture_progress(l->capture);
        gint64 end=g_get_monotonic_time()+timeout*1000;
        while(g_get_monotonic_time()<end&&ready(l)) {
            if(capture_snapshot_newer(l->capture,l->last_sequence,&frame))break;
            g_usleep(1000);
        }
        if(!frame.rgb)return error(ready(l)?capture_freeze_timeout_status(l->capture,progress,l->last_sequence):reason(l),kind);
        if(!ready(l)){
            capture_frame_clear(&frame);
            return error(reason(l),kind);
        }
        }
        int mem=memfd_create("computer-use-frame",MFD_CLOEXEC|MFD_ALLOW_SEALING);
        if(mem<0||ftruncate(mem,(off_t)frame.bytes)<0){
            if(mem>=0)close(mem);
            capture_frame_clear(&frame);
            return error("FRAME_FD_FAILED",kind);
        }
        size_t done=0;
        while(done<frame.bytes){
            ssize_t n=write(mem,frame.rgb+done,frame.bytes-done);
            if(n<=0)break;
            done+=(size_t)n;
        }
        if(done!=frame.bytes||fcntl(mem,F_ADD_SEALS,F_SEAL_WRITE|F_SEAL_GROW|F_SEAL_SHRINK|F_SEAL_SEAL)<0){
            close(mem);
            capture_frame_clear(&frame);
            return error("FRAME_SEAL_FAILED",kind);
        }
        *fd=mem;
        *kind="frame";
        out=json_object_new();
        json_object_set_object_member(out,"binding",binding(l));
        char *id=g_strdup_printf("%s-%llu",l->binding,(unsigned long long)frame.sequence);
        json_object_set_string_member(out,"frame_id",id);
        json_object_set_string_member(out,"observation_id",id);
        g_free(id);
        json_object_set_int_member(out,"width",frame.width);
        json_object_set_int_member(out,"height",frame.height);
        json_object_set_int_member(out,"stride",frame.width*3);
        json_object_set_int_member(out,"byte_size",frame.bytes);
        json_object_set_string_member(out,"format","RGB");
        char *hash=g_compute_checksum_for_data(G_CHECKSUM_SHA256,frame.rgb,frame.bytes);
        json_object_set_string_member(out,"sha256",hash);
        g_free(hash);
        json_object_set_int_member(out,"sequence",frame.sequence);
        json_object_set_double_member(out,"captured_monotonic",frame.timestamp);
        json_object_set_boolean_member(out,"liveness_proven",true);
        l->last_sequence=frame.sequence;
        capture_frame_clear(&frame);
    } else if(!strcmp(command,"send_event")) {
        if(json_object_get_size(p)!=4||!string(p,"binding_id")||!typed(p,"geometry_version",G_TYPE_INT64)||!number(p,"deadline_monotonic")||!l->binding||strcmp(l->binding,json_object_get_string_member(p,"binding_id"))||json_object_get_int_member(p,"geometry_version")!=1)return error("INVALID_EVENT",kind);
        JsonNode *node=json_object_get_member(p,"event");
        if(!node||!JSON_NODE_HOLDS_OBJECT(node))return error("INVALID_EVENT",kind);
        JsonObject *e=json_node_get_object(node);
        if(!string(e,"kind"))return error("INVALID_EVENT",kind);
        const char *event=json_object_get_string_member(e,"kind");
        double x=0,y=0;
        int units=0;
        const char *button=NULL;
        if(!strcmp(event,"motion")){
            if(json_object_get_size(e)!=3||!number(e,"x")||!number(e,"y"))return error("INVALID_EVENT",kind);
            x=json_object_get_double_member(e,"x");
            y=json_object_get_double_member(e,"y");
        }
        else if(!strcmp(event,"wheel_step")){
            if(json_object_get_size(e)!=2||!typed(e,"units",G_TYPE_INT64))return error("INVALID_EVENT",kind);
            gint64 u=json_object_get_int_member(e,"units");
            if(u!=1&&u!=-1)return error("INVALID_EVENT",kind);
            units=(int)u;
        }
        else if(!strcmp(event,"button_down")||!strcmp(event,"button_up")||!strcmp(event,"release_cleanup")){
            if(json_object_get_size(e)!=2||!string(e,"button"))return error("INVALID_EVENT",kind);
            button=json_object_get_string_member(e,"button");
        }
        else return error("INVALID_EVENT",kind);
        if(json_object_get_double_member(p,"deadline_monotonic")<g_get_monotonic_time()/1e6)return error("DISPATCH_DEADLINE_EXCEEDED",kind);
        if(!ready(l))return error(reason(l),kind);
        const char *failure=l->backend==DESKTOP_X11?
            x11_send(l->x11,event,x,y,button,units,json_object_get_double_member(p,"deadline_monotonic")):
            input_send(l->input,event,x,y,button,units,json_object_get_double_member(p,"deadline_monotonic"));
        if(failure)return error(failure,kind);
        *kind="event";
        out=json_object_new();
        json_object_set_string_member(out,"code","EVENT_SUBMITTED");
    } else return error("UNSUPPORTED_COMMAND",kind);
    JsonNode *result=json_node_new(JSON_NODE_OBJECT);
    json_node_take_object(result,out);
    return result;
}
#endif
