/* A scripted libei event source exercises the production device/region policy.
 * This is not an EIS transport or compositor-delivery test. */
#include "ei_input.h"
#include <libei.h>
#include <assert.h>
#include <math.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>

struct ei {int fd;};
struct ei_region {const char *mapping;uint32_t x,y,w,h;};
struct ei_device {struct ei_region region;bool caps;struct ei_region second;unsigned regions;enum ei_device_type type;};
struct ei_seat {int unused;};
struct ei_event {enum ei_event_type type;struct ei_device *device;};
static struct ei context;
static struct ei_device a,b;
static struct ei_seat seat;
static struct ei_event queue[32];
static unsigned head,tail,presses,releases,motions,wheels,frames;
static bool disconnect_on_frame;
static void push(enum ei_event_type type,struct ei_device *device){assert(tail<32);queue[tail++]=(struct ei_event){type,device};}
static void reset(void){head=tail=presses=releases=motions=wheels=frames=0;disconnect_on_frame=false;a=(struct ei_device){.region={"monitor",10,20,100,200},.caps=true,.type=EI_DEVICE_TYPE_VIRTUAL};b=a;}
struct ei *ei_new_sender(void *p){(void)p;return &context;}
void ei_configure_name(struct ei *e,const char *name){(void)e;(void)name;}
int ei_setup_backend_fd(struct ei *e,int fd){e->fd=fd;return 0;}
struct ei *ei_unref(struct ei *e){close(e->fd);return NULL;}
void ei_dispatch(struct ei *e){(void)e;}
struct ei_event *ei_get_event(struct ei *e){(void)e;return head<tail?&queue[head++]:NULL;}
enum ei_event_type ei_event_get_type(struct ei_event *event){return event->type;}
struct ei_device *ei_event_get_device(struct ei_event *event){return event->device;}
struct ei_seat *ei_event_get_seat(struct ei_event *event){(void)event;return &seat;}
struct ei_event *ei_event_unref(struct ei_event *event){(void)event;return NULL;}
void ei_seat_bind_capabilities(struct ei_seat *s,...){assert(s==&seat);}
struct ei_device *ei_device_ref(struct ei_device *device){return device;}
struct ei_device *ei_device_unref(struct ei_device *device){(void)device;return NULL;}
bool ei_device_has_capability(struct ei_device *device,enum ei_device_capability cap){(void)cap;return device->caps;}
enum ei_device_type ei_device_get_type(struct ei_device *device){return device->type;}
struct ei_region *ei_device_get_region(struct ei_device *device,size_t index){
    if(index==0)return &device->region;
    return index==1&&device->regions>1?&device->second:NULL;
}
const char *ei_region_get_mapping_id(struct ei_region *r){return r->mapping;}
uint32_t ei_region_get_x(struct ei_region *r){return r->x;}
uint32_t ei_region_get_y(struct ei_region *r){return r->y;}
uint32_t ei_region_get_width(struct ei_region *r){return r->w;}
uint32_t ei_region_get_height(struct ei_region *r){return r->h;}
void ei_device_start_emulating(struct ei_device *d,uint32_t seq){assert(d==&a&&seq>0);}
void ei_device_stop_emulating(struct ei_device *d){assert(d==&a);}
void ei_device_pointer_motion_absolute(struct ei_device *d,double x,double y){assert(d==&a&&x>=10&&x<=109&&y>=20&&y<=219);motions++;}
void ei_device_scroll_discrete(struct ei_device *d,int32_t x,int32_t y){assert(d==&a&&x==0&&(y==120||y==-120));wheels++;}
void ei_device_button_button(struct ei_device *d,uint32_t button,bool press){assert(d==&a&&(button==272||button==273));if(press)presses++;else releases++;}
void ei_device_frame(struct ei_device *d,uint64_t time){assert(d==&a&&time>0);frames++;if(disconnect_on_frame){push(EI_EVENT_DISCONNECT,NULL);disconnect_on_frame=false;}}
uint64_t ei_now(struct ei *e){(void)e;return 123;}
static NativeInput *open_input(void){int fd=open("/dev/null",O_RDONLY);assert(fd>=0);NativeInput *i=input_open(fd,"monitor");assert(i);push(EI_EVENT_SEAT_ADDED,NULL);push(EI_EVENT_DEVICE_ADDED,&a);return i;}
static bool region(NativeInput *i){uint32_t x,y,w,h;return input_region(i,&x,&y,&w,&h);}
int main(void){
    /* Startup diagnostics identify the first missing protocol boundary without
     * weakening the requirement for a resumed, mapped absolute device. */
    reset();
    int probe_fd=open("/dev/null",O_RDONLY);assert(probe_fd>=0);
    NativeInput *probe=input_open(probe_fd,"monitor");assert(probe);
    assert(!strcmp(input_startup_status(probe),"EIS_CONNECT_TIMEOUT"));
    push(EI_EVENT_CONNECT,NULL);
    assert(!strcmp(input_startup_status(probe),"EIS_SEAT_TIMEOUT"));
    push(EI_EVENT_SEAT_ADDED,NULL);
    assert(!strcmp(input_startup_status(probe),"EIS_DEVICE_TIMEOUT"));
    push(EI_EVENT_DEVICE_ADDED,&a);
    assert(!strcmp(input_startup_status(probe),"EIS_DEVICE_RESUME_TIMEOUT"));
    push(EI_EVENT_DEVICE_RESUMED,&a);
    assert(input_startup_status(probe)==NULL);
    input_close(probe);

    /* Repeated announcement of one device is still one candidate. */
    reset();probe_fd=open("/dev/null",O_RDONLY);assert(probe_fd>=0);
    probe=input_open(probe_fd,"monitor");assert(probe);
    push(EI_EVENT_CONNECT,NULL);push(EI_EVENT_SEAT_ADDED,NULL);
    push(EI_EVENT_DEVICE_ADDED,&a);push(EI_EVENT_DEVICE_ADDED,&a);
    push(EI_EVENT_DEVICE_RESUMED,&a);
    assert(input_startup_status(probe)==NULL);
    assert(region(probe));
    input_close(probe);

    /* Equivalent virtual devices are two routes to the same mapped region. */
    reset();probe_fd=open("/dev/null",O_RDONLY);assert(probe_fd>=0);
    probe=input_open(probe_fd,"monitor");assert(probe);
    push(EI_EVENT_CONNECT,NULL);push(EI_EVENT_SEAT_ADDED,NULL);
    push(EI_EVENT_DEVICE_ADDED,&a);push(EI_EVENT_DEVICE_RESUMED,&a);
    push(EI_EVENT_DEVICE_ADDED,&b);push(EI_EVENT_DEVICE_RESUMED,&b);
    assert(input_startup_status(probe)==NULL);
    assert(region(probe));assert(input_commit(probe));
    assert(!input_send(probe,"motion",10,20,NULL,0,100));
    input_close(probe);

    reset();probe_fd=open("/dev/null",O_RDONLY);assert(probe_fd>=0);
    probe=input_open(probe_fd,"monitor");assert(probe);
    b.region.x=1000;
    push(EI_EVENT_CONNECT,NULL);push(EI_EVENT_SEAT_ADDED,NULL);
    push(EI_EVENT_DEVICE_ADDED,&a);push(EI_EVENT_DEVICE_RESUMED,&a);
    push(EI_EVENT_DEVICE_ADDED,&b);push(EI_EVENT_DEVICE_RESUMED,&b);
    assert(!strcmp(input_startup_status(probe),"EIS_MAPPING_MULTIPLE_DEVICES_DIFFERENT_REGIONS"));
    input_close(probe);

    reset();probe_fd=open("/dev/null",O_RDONLY);assert(probe_fd>=0);
    probe=input_open(probe_fd,"monitor");assert(probe);
    b.type=EI_DEVICE_TYPE_PHYSICAL;
    push(EI_EVENT_CONNECT,NULL);push(EI_EVENT_SEAT_ADDED,NULL);
    push(EI_EVENT_DEVICE_ADDED,&a);push(EI_EVENT_DEVICE_RESUMED,&a);
    push(EI_EVENT_DEVICE_ADDED,&b);push(EI_EVENT_DEVICE_RESUMED,&b);
    assert(region(probe));assert(input_startup_status(probe)==NULL);
    input_close(probe);

    reset();probe_fd=open("/dev/null",O_RDONLY);assert(probe_fd>=0);
    probe=input_open(probe_fd,"monitor");assert(probe);
    a.second=a.region;a.regions=2;
    push(EI_EVENT_CONNECT,NULL);push(EI_EVENT_SEAT_ADDED,NULL);
    push(EI_EVENT_DEVICE_ADDED,&a);push(EI_EVENT_DEVICE_RESUMED,&a);
    assert(!strcmp(input_startup_status(probe),"EIS_MAPPING_MULTIPLE_REGIONS"));
    input_close(probe);

    reset();probe_fd=open("/dev/null",O_RDONLY);assert(probe_fd>=0);
    probe=input_open(probe_fd,"monitor");assert(probe);
    push(EI_EVENT_CONNECT,NULL);push(EI_EVENT_SEAT_ADDED,NULL);
    b.region.mapping="other";push(EI_EVENT_DEVICE_ADDED,&b);
    push(EI_EVENT_DEVICE_RESUMED,&b);
    assert(!strcmp(input_startup_status(probe),"EIS_MAPPING_NOT_FOUND"));
    input_close(probe);

    reset();NativeInput *i=open_input();
    assert(!region(i)); /* ADDED is not RESUMED. */
    push(EI_EVENT_DEVICE_RESUMED,&a);assert(region(i));assert(input_commit(i));
    assert(!strcmp(input_send(i,"motion",NAN,20,NULL,0,100),"INVALID_EVENT"));
    assert(!strcmp(input_send(i,"motion",110,20,NULL,0,100),"INVALID_EVENT"));
    assert(!strcmp(input_send(i,"motion",10,20,NULL,0,0),"DISPATCH_DEADLINE_EXCEEDED"));
    assert(motions==0);
    assert(!input_send(i,"motion",109,219,NULL,0,100));
    assert(!input_send(i,"button_down",0,0,"left",0,100));
    assert(!input_send(i,"button_up",0,0,"left",0,100));
    assert(!input_send(i,"release_cleanup",0,0,"left",0,100));
    assert(presses==1&&releases==1&&motions==1);
    assert(!input_send(i,"wheel_step",0,0,NULL,1,100));assert(wheels==1);
    input_close(i);assert(releases==1); /* No duplicate releases on shutdown. */

    reset();i=open_input();push(EI_EVENT_DEVICE_RESUMED,&a);assert(region(i));assert(input_commit(i));
    push(EI_EVENT_DEVICE_ADDED,&b);push(EI_EVENT_DEVICE_RESUMED,&b);
    assert(region(i)&&!input_error(i));
    push(EI_EVENT_DEVICE_REMOVED,&b);assert(region(i));input_close(i);

    reset();i=open_input();push(EI_EVENT_DEVICE_RESUMED,&a);assert(region(i));assert(input_commit(i));
    b.region.x=1000;
    push(EI_EVENT_DEVICE_ADDED,&b);push(EI_EVENT_DEVICE_RESUMED,&b);
    assert(!region(i)&&!strcmp(input_error(i),"EIS_MAPPING_AMBIGUOUS"));input_close(i);

    reset();i=open_input();push(EI_EVENT_DEVICE_RESUMED,&a);
    push(EI_EVENT_DEVICE_ADDED,&b);push(EI_EVENT_DEVICE_RESUMED,&b);
    assert(region(i));assert(input_commit(i));
    push(EI_EVENT_DEVICE_PAUSED,&a);
    assert(!region(i)&&!strcmp(input_error(i),"INPUT_DEVICE_NOT_READY"));input_close(i);

    reset();i=open_input();push(EI_EVENT_DEVICE_RESUMED,&a);assert(region(i));assert(input_commit(i));
    assert(!input_send(i,"button_down",0,0,"right",0,100));
    input_invalidate(i);input_close(i);assert(releases==0);

    reset();i=open_input();push(EI_EVENT_DEVICE_RESUMED,&a);assert(region(i));assert(input_commit(i));
    disconnect_on_frame=true;
    assert(!strcmp(input_send(i,"motion",10,20,NULL,0,100),"ACTION_DELIVERY_UNKNOWN"));
    assert(motions==1);input_close(i);

    reset();i=open_input();push(EI_EVENT_DEVICE_RESUMED,&a);assert(region(i));assert(input_commit(i));
    push(EI_EVENT_DEVICE_PAUSED,&a);assert(!region(i));
    assert(!strcmp(input_error(i),"INPUT_DEVICE_NOT_READY"));input_close(i);
    /* Provisional device replacement is allowed until startup publication. */
    reset();i=open_input();push(EI_EVENT_DEVICE_RESUMED,&a);assert(region(i));
    assert(!strcmp(input_send(i,"motion",10,20,NULL,0,100),"INPUT_BINDING_UNCOMMITTED"));
    push(EI_EVENT_DEVICE_REMOVED,&a);push(EI_EVENT_DEVICE_ADDED,&b);push(EI_EVENT_DEVICE_RESUMED,&b);
    assert(region(i)&&!input_error(i));assert(input_commit(i));
    push(EI_EVENT_DEVICE_REMOVED,&b);
    assert(!region(i)&&!strcmp(input_error(i),"INPUT_DEVICE_NOT_READY"));input_close(i);

    return 0;
}
