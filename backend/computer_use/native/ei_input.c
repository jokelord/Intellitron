#include "ei_input.h"
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <math.h>
bool input_button_transition(bool *held,bool pressed) {
    if(!held||*held==pressed)return false;
    *held=pressed;return true;
}
#ifdef HAVE_WAYLAND_INPUT_DEPS
#include <libei.h>
#include <linux/input-event-codes.h>
struct NativeInput {
    struct ei *ei;
    struct ei_device *devices[32];
    bool resumed[32];
    unsigned count;
    char *mapping;
    const char *error;
    uint32_t x,y,w,h;
    bool bound,emulating;
    bool held_left,held_right;
    struct ei_device *selected;
    unsigned sequence;
    bool connected,seat_seen;
};
NativeInput *input_open(int fd,const char *mapping) {
    NativeInput *i=calloc(1,sizeof(*i));
    if(!i){
        close(fd);
        return NULL;
    }
    i->mapping=strdup(mapping);
    i->ei=ei_new_sender(NULL);
    if(!i->ei){
        close(fd);
        input_close(i);
        return NULL;
    }
    ei_configure_name(i->ei,"computer-use-helper");
    if(ei_setup_backend_fd(i->ei,fd)<0){
        input_close(i);
        return NULL;
    }
    return i;
}
void input_pump(NativeInput *i) {
    if(!i||i->error)return;
    ei_dispatch(i->ei);
    struct ei_event *event;
    while((event=ei_get_event(i->ei))) {
        enum ei_event_type type=ei_event_get_type(event);
        struct ei_device *device=ei_event_get_device(event);
        if(type==EI_EVENT_DISCONNECT)i->error="EIS_DISCONNECTED";
        else if(type==EI_EVENT_CONNECT)i->connected=true;
        else if(type==EI_EVENT_SEAT_ADDED){
            i->seat_seen=true;
            ei_seat_bind_capabilities(ei_event_get_seat(event),EI_DEVICE_CAP_POINTER_ABSOLUTE,EI_DEVICE_CAP_BUTTON,EI_DEVICE_CAP_SCROLL,NULL);
        }
        else if(type==EI_EVENT_DEVICE_ADDED){
            bool known=false;
            for(unsigned n=0;n<i->count;n++)if(i->devices[n]==device){known=true;break;}
            if(!known){
                if(i->count==32)i->error="EIS_DEVICE_LIMIT";
                else i->devices[i->count++]=ei_device_ref(device);
            }
        }
        else if(type==EI_EVENT_DEVICE_RESUMED||type==EI_EVENT_DEVICE_PAUSED||type==EI_EVENT_DEVICE_REMOVED) {
            for(unsigned n=0;n<i->count;n++)if(i->devices[n]==device) {
                i->resumed[n]=type==EI_EVENT_DEVICE_RESUMED;
                if(device==i->selected&&type!=EI_EVENT_DEVICE_RESUMED){
                    if(i->bound)i->error="INPUT_DEVICE_NOT_READY";
                    i->selected=NULL;
                }
                if(type==EI_EVENT_DEVICE_REMOVED){
                    ei_device_unref(i->devices[n]);
                    i->devices[n]=i->devices[i->count-1];
                    i->resumed[n]=i->resumed[i->count-1];
                    i->count--;
                }
                break;
            }
        }ei_event_unref(event);
    }
}
typedef struct {
    bool resumed,capable,invalid,same_region;
    unsigned matches,matched_devices;
    uint32_t x,y,w,h;
    struct ei_device *first,*preferred;
} MappingScan;
static MappingScan scan_mapping(NativeInput *i){
    MappingScan s={.same_region=true};
    for(unsigned n=0;n<i->count;n++){
        if(!i->resumed[n])continue;
        s.resumed=true;
        struct ei_device *d=i->devices[n];
        if(ei_device_get_type(d)!=EI_DEVICE_TYPE_VIRTUAL||
           !ei_device_has_capability(d,EI_DEVICE_CAP_POINTER_ABSOLUTE)||
           !ei_device_has_capability(d,EI_DEVICE_CAP_BUTTON)||
           !ei_device_has_capability(d,EI_DEVICE_CAP_SCROLL))continue;
        s.capable=true;
        bool matched=false;
        for(unsigned j=0;;j++){
            struct ei_region *r=ei_device_get_region(d,j);
            if(!r)break;
            const char *mapping=ei_region_get_mapping_id(r);
            if(!mapping||strcmp(mapping,i->mapping))continue;
            uint32_t x=ei_region_get_x(r),y=ei_region_get_y(r);
            uint32_t w=ei_region_get_width(r),h=ei_region_get_height(r);
            if(!w||!h||w>16384||h>16384)s.invalid=true;
            if(!s.first){s.first=d;s.x=x;s.y=y;s.w=w;s.h=h;}
            else if(x!=s.x||y!=s.y||w!=s.w||h!=s.h)s.same_region=false;
            if(d==i->selected)s.preferred=d;
            s.matches++;
            matched=true;
        }
        if(matched)s.matched_devices++;
    }
    return s;
}
static bool mapping_usable(const MappingScan *s){
    return s->matches&&!s->invalid&&s->same_region&&
           s->matches==s->matched_devices;
}
bool input_region(NativeInput *i,uint32_t *x,uint32_t *y,uint32_t *w,uint32_t *h) {
    input_pump(i);
    if(!i||i->error)return false;
    MappingScan s=scan_mapping(i);
    if(!mapping_usable(&s)){
        if(i->bound)i->error=!s.matches?"EIS_MAPPING_LOST":
                                 s.invalid?"EIS_REGION_INVALID":"EIS_MAPPING_AMBIGUOUS";
        return false;
    }
    if(i->bound&&(!s.preferred||i->x!=s.x||i->y!=s.y||i->w!=s.w||i->h!=s.h)){
        i->error=s.preferred?"EIS_GEOMETRY_CHANGED":"INPUT_DEVICE_NOT_READY";
        return false;
    }
    i->selected=s.preferred?s.preferred:s.first;
    i->x=s.x;i->y=s.y;i->w=s.w;i->h=s.h;
    *x=s.x;*y=s.y;*w=s.w;*h=s.h;
    return true;
}
bool input_commit(NativeInput *i){
    uint32_t x,y,w,h;
    if(!input_region(i,&x,&y,&w,&h))return false;
    i->bound=true;
    return true;
}
const char *input_send(NativeInput *i,const char *kind,double x,double y,const char *button,int units,double deadline_monotonic) {
    uint32_t rx,ry,w,h;
    if(!i||!i->bound)return "INPUT_BINDING_UNCOMMITTED";
    if(!input_region(i,&rx,&ry,&w,&h))return i&&i->error?i->error:"INPUT_DEVICE_NOT_READY";
    bool motion=!strcmp(kind,"motion"),wheel=!strcmp(kind,"wheel_step"),down=!strcmp(kind,"button_down"),up=!strcmp(kind,"button_up")||!strcmp(kind,"release_cleanup");
    if((motion&&(!isfinite(x)||!isfinite(y)||x<rx||y<ry||x>(double)rx+w-1||y>(double)ry+h-1))||        (wheel&&units!=1&&units!=-1)||((down||up)&&(!button||(strcmp(button,"left")&&strcmp(button,"right"))))||(!motion&&!wheel&&!down&&!up))return "INVALID_EVENT";
    if(!isfinite(deadline_monotonic)||ei_now(i->ei)/1e6>=deadline_monotonic)return "DISPATCH_DEADLINE_EXCEEDED";
    bool *held=NULL;
    if(down||up){
        held=!strcmp(button,"left")?&i->held_left:&i->held_right;
        if(!input_button_transition(held,down))return NULL;
    }
    if(!i->emulating){
        ei_device_start_emulating(i->selected,++i->sequence);
        i->emulating=true;
    }
    if(motion)ei_device_pointer_motion_absolute(i->selected,x,y);
    else if(wheel)ei_device_scroll_discrete(i->selected,0,-units*120);
    else ei_device_button_button(i->selected,!strcmp(button,"left")?BTN_LEFT:BTN_RIGHT,down);
    ei_device_frame(i->selected,ei_now(i->ei));
    input_pump(i);
    return i->error?"ACTION_DELIVERY_UNKNOWN":NULL;
}
const char *input_error(NativeInput *i){
    return i?i->error:"EIS_UNAVAILABLE";
}
const char *input_startup_status(NativeInput *i){
    if(!i)return "EIS_UNAVAILABLE";
    input_pump(i);
    if(i->error)return i->error;
    if(!i->connected)return "EIS_CONNECT_TIMEOUT";
    if(!i->seat_seen)return "EIS_SEAT_TIMEOUT";
    if(!i->count)return "EIS_DEVICE_TIMEOUT";
    MappingScan s=scan_mapping(i);
    if(!s.resumed)return "EIS_DEVICE_RESUME_TIMEOUT";
    if(!s.capable)return "EIS_REQUIRED_CAPABILITY_MISSING";
    if(!s.matches)return "EIS_MAPPING_NOT_FOUND";
    if(s.invalid)return "EIS_REGION_INVALID";
    if(!s.same_region)return s.matched_devices>1?
        "EIS_MAPPING_MULTIPLE_DEVICES_DIFFERENT_REGIONS":"EIS_MAPPING_MULTIPLE_REGIONS";
    if(s.matches!=s.matched_devices)return "EIS_MAPPING_MULTIPLE_REGIONS";
    return NULL;
}
void input_invalidate(NativeInput *i){if(i)i->error="INPUT_REVOKED";}
void input_close(NativeInput *i){
    if(!i)return;
    if(i->emulating&&!i->error&&i->selected){
        if(i->held_left)ei_device_button_button(i->selected,BTN_LEFT,false);
        if(i->held_right)ei_device_button_button(i->selected,BTN_RIGHT,false);
        ei_device_frame(i->selected,ei_now(i->ei));
        ei_device_stop_emulating(i->selected);
    }
    for(unsigned n=0;n<i->count;n++)ei_device_unref(i->devices[n]);
    if(i->ei)ei_unref(i->ei);
    free(i->mapping);
    free(i);
}
#else
bool input_commit(NativeInput *i){(void)i;return false;}
void input_invalidate(NativeInput *i){(void)i;}
struct NativeInput{
    int unused;
};
NativeInput *input_open(int fd,const char *mapping){
    (void)mapping;
    close(fd);
    return NULL;
}
void input_pump(NativeInput *i){
    (void)i;
}
void input_close(NativeInput *i){
    (void)i;
}
bool input_region(NativeInput *i,uint32_t *x,uint32_t *y,uint32_t *w,uint32_t *h){
    (void)i;
    (void)x;
    (void)y;
    (void)w;
    (void)h;
    return false;
}
const char *input_send(NativeInput *i,const char *kind,double x,double y,const char *button,int units,double deadline_monotonic){
    (void)i;
    (void)kind;
    (void)x;
    (void)y;
    (void)button;
    (void)units;
    (void)deadline_monotonic;
    return "EIS_UNAVAILABLE";
}
const char *input_error(NativeInput *i){
    (void)i;
    return "EIS_UNAVAILABLE";
}
const char *input_startup_status(NativeInput *i){
    (void)i;
    return "EIS_UNAVAILABLE";
}
#endif
bool input_can_emit(const InputState *s){
    (void)s;
    return false;
}
