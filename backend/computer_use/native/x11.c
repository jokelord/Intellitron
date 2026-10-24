#define _POSIX_C_SOURCE 200809L
#include "x11.h"
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <time.h>
DesktopBackend desktop_backend(void) {
    const char *type=getenv("XDG_SESSION_TYPE"),*wayland=getenv("WAYLAND_DISPLAY"),*display=getenv("DISPLAY");
    if(type&&*type){
        if(!strcmp(type,"wayland"))return DESKTOP_WAYLAND;
        if(!strcmp(type,"x11"))return DESKTOP_X11;
        return DESKTOP_UNAVAILABLE;
    }
    if(wayland&&*wayland)return DESKTOP_WAYLAND;
    return display&&*display?DESKTOP_X11:DESKTOP_WAYLAND;
}
bool x11_ready(void){return false;}
#ifdef HAVE_X11_INPUT_DEPS
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <X11/extensions/Xrandr.h>
#include <X11/extensions/XTest.h>
struct X11Session {
    Display *display;
    int screen,event_base;
    X11Geometry geometry;
    uint64_t fingerprint,sequence;
    size_t max_bytes;
    bool bound,held_left,held_right;
    unsigned held_wheel;
    const char *error;
    int (*old_handler)(Display *,XErrorEvent *);
};
/* The helper has one control loop and one X connection. X errors are checked
 * synchronously at every capture/input barrier rather than aborting Xlib. */
static int server_error;
static int catch_error(Display *display,XErrorEvent *event){(void)display;server_error=event->error_code;return 0;}
static double monotonic(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec+t.tv_nsec/1e9;}
static uint64_t mix(uint64_t h,uint64_t n){return (h^n)*UINT64_C(1099511628211);}
static bool scan(X11Session *s,X11Geometry *g,uint64_t *fingerprint){
    Window root=RootWindow(s->display,s->screen);
    XWindowAttributes attributes;
    if(!XGetWindowAttributes(s->display,root,&attributes)||attributes.visual->class!=TrueColor)return false;
    int count=0;
    XRRMonitorInfo *monitors=XRRGetMonitors(s->display,root,True,&count);
    if(!monitors||count<1){if(monitors)XRRFreeMonitors(monitors);return false;}
    int selected=-1;
    for(int n=0;n<count;n++)if(monitors[n].primary){if(selected!=-1){XRRFreeMonitors(monitors);return false;}selected=n;}
    if(selected<0&&count==1)selected=0;
    if(selected<0){XRRFreeMonitors(monitors);return false;}
    XRRMonitorInfo *m=&monitors[selected];
    bool ok=m->name&&m->x>=0&&m->y>=0&&m->width>0&&m->height>0&&m->width<=16384&&m->height<=16384&&
        m->x<=attributes.width-m->width&&m->y<=attributes.height-m->height&&m->noutput>0&&m->noutput<=32;
    uint64_t h=UINT64_C(1469598103934665603);
    h=mix(h,root);h=mix(h,attributes.width);h=mix(h,attributes.height);
    for(int n=0;n<count;n++){
        XRRMonitorInfo *item=&monitors[n];
        h=mix(h,item->name);h=mix(h,item->primary);h=mix(h,item->x);h=mix(h,item->y);
        h=mix(h,item->width);h=mix(h,item->height);h=mix(h,item->noutput);
        for(int j=0;j<item->noutput;j++)h=mix(h,item->outputs[j]);
    }
    memset(g,0,sizeof(*g));
    g->root=root;g->monitor=m->name;g->x=m->x;g->y=m->y;g->width=m->width;g->height=m->height;
    char *name=XGetAtomName(s->display,m->name);
    if(!name||!*name||strlen(name)>=sizeof(g->monitor_name))ok=false;
    else strcpy(g->monitor_name,name);
    if(name)XFree(name);
    XRRScreenResources *resources=XRRGetScreenResourcesCurrent(s->display,root);
    if(!resources)ok=false;
    if(resources){
        h=mix(h,resources->configTimestamp);
        for(int n=0;ok&&n<m->noutput;n++){
            RROutput id=m->outputs[n];
            XRROutputInfo *output=XRRGetOutputInfo(s->display,resources,id);
            if(!id||!output||output->connection!=RR_Connected||!output->crtc)ok=false;
            if(output&&output->nameLen>=8&&!strncmp(output->name,"XWAYLAND",8)){s->error="X11_XWAYLAND_NOT_SUPPORTED";ok=false;}
            if(ok){
                XRRCrtcInfo *crtc=XRRGetCrtcInfo(s->display,resources,output->crtc);
                if(!crtc||!crtc->mode||!crtc->width||!crtc->height)ok=false;
                else{
                    h=mix(h,id);h=mix(h,output->crtc);h=mix(h,crtc->mode);h=mix(h,crtc->rotation);
                    h=mix(h,crtc->x);h=mix(h,crtc->y);h=mix(h,crtc->width);h=mix(h,crtc->height);
                    XRRCrtcTransformAttributes *transform=NULL;
                    if(XRRGetCrtcTransform(s->display,output->crtc,&transform)&&transform){
                        for(int a=0;a<3;a++)for(int b=0;b<3;b++)h=mix(h,transform->currentTransform.matrix[a][b]);
                        XFree(transform);
                    }
                }
                if(crtc)XRRFreeCrtcInfo(crtc);
            }
            if(output)XRRFreeOutputInfo(output);
            for(int j=0;j<n;j++)if(g->outputs[j]==id)ok=false;
            g->outputs[n]=id;
        }
        XRRFreeScreenResources(resources);
    }
    g->output_count=m->noutput;
    XRRFreeMonitors(monitors);
    XSync(s->display,False);
    *fingerprint=h;
    return ok&&!server_error;
}
static bool check(X11Session *s){
    if(!s||!s->display||s->error||!s->bound)return false;
    X11Geometry geometry;uint64_t fingerprint=0;
    server_error=0;
    if(!scan(s,&geometry,&fingerprint)||fingerprint!=s->fingerprint||memcmp(&geometry,&s->geometry,sizeof(geometry))){
        if(!s->error)s->error="X11_GEOMETRY_CHANGED";
        return false;
    }
    return true;
}
X11Session *x11_open(size_t max_bytes){
    X11Session *s=calloc(1,sizeof(*s));if(!s)return NULL;
    s->max_bytes=max_bytes;
    if(desktop_backend()!=DESKTOP_X11){s->error="X11_SESSION_NOT_SELECTED";return s;}
    s->display=XOpenDisplay(NULL);
    if(!s->display){s->error="X11_DISPLAY_UNAVAILABLE";return s;}
    s->old_handler=XSetErrorHandler(catch_error);server_error=0;
    int opcode,event,error,major=1,minor=5;
    if(XQueryExtension(s->display,"XWAYLAND",&opcode,&event,&error)){s->error="X11_XWAYLAND_NOT_SUPPORTED";return s;}
    if(!XRRQueryExtension(s->display,&s->event_base,&error)||!XRRQueryVersion(s->display,&major,&minor)||major<1||(major==1&&minor<5)){
        s->error="X11_RANDR_UNAVAILABLE";return s;
    }
    if(!XTestQueryExtension(s->display,&event,&error,&major,&minor)){s->error="X11_XTEST_UNAVAILABLE";return s;}
    s->screen=DefaultScreen(s->display);
    XGrabServer(s->display);
    bool mapped=scan(s,&s->geometry,&s->fingerprint);
    XRRSelectInput(s->display,RootWindow(s->display,s->screen),RRScreenChangeNotifyMask|RRCrtcChangeNotifyMask|RROutputChangeNotifyMask|RRResourceChangeNotifyMask);
    XUngrabServer(s->display);XSync(s->display,False);
    if(!mapped){if(!s->error)s->error="X11_MONITOR_MAPPING_UNAVAILABLE";return s;}
    if((size_t)s->geometry.width*s->geometry.height*3>max_bytes){s->error="FRAME_TOO_LARGE";return s;}
    s->bound=true;
    return s;
}
void x11_pump(X11Session *s){
    if(!s||!s->display||s->error)return;
    bool changed=false;
    while(XPending(s->display)){
        XEvent event;XNextEvent(s->display,&event);
        if(event.type==s->event_base+RRScreenChangeNotify||event.type==s->event_base+RRNotify)changed=true;
    }
    if(changed)check(s);
}
bool x11_geometry(X11Session *s,X11Geometry *g){if(!g||!check(s))return false;*g=s->geometry;return true;}
const char *x11_error(X11Session *s){return s?s->error:"X11_UNAVAILABLE";}
static uint8_t channel(unsigned long pixel,unsigned long mask){
    if(!mask)return 0;
    unsigned shift=0;while(!(mask&1)){mask>>=1;shift++;}
    return (uint8_t)((((pixel>>shift)&mask)*255+mask/2)/mask);
}
bool x11_snapshot(X11Session *s,CaptureFrame *f){
    if(!f||!s||s->error)return false;
    XGrabServer(s->display);
    bool valid=check(s);
    XImage *image=valid?XGetImage(s->display,s->geometry.root,s->geometry.x,s->geometry.y,
                                s->geometry.width,s->geometry.height,AllPlanes,ZPixmap):NULL;
    XSync(s->display,False);
    double captured=monotonic();
    XUngrabServer(s->display);XFlush(s->display);
    if(!image||server_error){if(image)XDestroyImage(image);if(!s->error)s->error="X11_CAPTURE_FAILED";return false;}
    size_t bytes=(size_t)image->width*image->height*3;
    uint8_t *rgb=malloc(bytes);
    if(!rgb){XDestroyImage(image);s->error="FRAME_ALLOCATION_FAILED";return false;}
    for(int y=0;y<image->height;y++)for(int x=0;x<image->width;x++){
        unsigned long pixel=XGetPixel(image,x,y);
        size_t index=((size_t)y*image->width+x)*3;
        rgb[index]=channel(pixel,image->red_mask);rgb[index+1]=channel(pixel,image->green_mask);rgb[index+2]=channel(pixel,image->blue_mask);
    }
    *f=(CaptureFrame){.rgb=rgb,.bytes=bytes,.width=image->width,.height=image->height,
        .sequence=++s->sequence,.geometry=1,.timestamp=captured};
    XDestroyImage(image);return true;
}
const char *x11_send(X11Session *s,const char *kind,double x,double y,const char *button,int units,double deadline){
    if(!s||!s->bound)return "INPUT_BINDING_UNCOMMITTED";
    bool motion=!strcmp(kind,"motion"),wheel=!strcmp(kind,"wheel_step"),down=!strcmp(kind,"button_down"),up=!strcmp(kind,"button_up")||!strcmp(kind,"release_cleanup");
    X11Geometry *g=&s->geometry;
    if((motion&&(!isfinite(x)||!isfinite(y)||x<g->x||y<g->y||x>(double)g->x+g->width-1||y>(double)g->y+g->height-1))||
       (wheel&&units!=1&&units!=-1)||((down||up)&&(!button||(strcmp(button,"left")&&strcmp(button,"right"))))||(!motion&&!wheel&&!down&&!up))return "INVALID_EVENT";
    XGrabServer(s->display);
    const char *failure=NULL;
    if(!check(s))failure=x11_error(s);
    else if(!isfinite(deadline)||monotonic()>=deadline)failure="DISPATCH_DEADLINE_EXCEEDED";
    else{
        bool submitted=true;
        bool *button_held=NULL;
        if(motion)submitted=XTestFakeMotionEvent(s->display,s->screen,(int)llround(x),(int)llround(y),CurrentTime);
        else if(wheel){
            unsigned b=units>0?4:5;
            submitted=XTestFakeButtonEvent(s->display,b,True,CurrentTime);
            if(submitted)s->held_wheel=b;
            bool released=XTestFakeButtonEvent(s->display,b,False,CurrentTime);
            submitted=released&&submitted;
        }
        else{
            button_held=!strcmp(button,"left")?&s->held_left:&s->held_right;
            if(*button_held!=down){
                submitted=XTestFakeButtonEvent(s->display,!strcmp(button,"left")?1:3,down,CurrentTime);
                if(submitted&&down)*button_held=true;
            }
        }
        XSync(s->display,False);
        if(!submitted||server_error){s->error="ACTION_DELIVERY_UNKNOWN";failure=s->error;}
        else {if(wheel)s->held_wheel=0;if(button_held&&!down)*button_held=false;}
    }
    XUngrabServer(s->display);XFlush(s->display);
    return failure;
}
void x11_close(X11Session *s){
    if(!s)return;
    if(s->display){
        /* Only release buttons this helper pressed on its still-live server.
         * Geometry invalidation never authorizes further motion or presses. */
        if(s->held_left)XTestFakeButtonEvent(s->display,1,False,CurrentTime);
        if(s->held_right)XTestFakeButtonEvent(s->display,3,False,CurrentTime);
        if(s->held_wheel)XTestFakeButtonEvent(s->display,s->held_wheel,False,CurrentTime);
        XSync(s->display,False);
        XCloseDisplay(s->display);XSetErrorHandler(s->old_handler);
    }
    free(s);
}
const char *x11_probe_code(void){
    if(desktop_backend()!=DESKTOP_X11)return "X11_SESSION_NOT_SELECTED";
    X11Session *s=x11_open(SIZE_MAX);
    const char *code=x11_error(s);if(!code)code="X11_INTERFACES_SUPPORTED";
    x11_close(s);return code;
}
#else
struct X11Session{int unused;};
X11Session *x11_open(size_t max){(void)max;return NULL;}
void x11_close(X11Session *s){(void)s;}
void x11_pump(X11Session *s){(void)s;}
bool x11_geometry(X11Session *s,X11Geometry *g){(void)s;(void)g;return false;}
const char *x11_error(X11Session *s){(void)s;return "X11_DEVELOPMENT_DEPS_MISSING";}
bool x11_snapshot(X11Session *s,CaptureFrame *f){(void)s;(void)f;return false;}
const char *x11_send(X11Session *s,const char *kind,double x,double y,const char *button,int units,double deadline){
    (void)s;(void)kind;(void)x;(void)y;(void)button;(void)units;(void)deadline;return "X11_DEVELOPMENT_DEPS_MISSING";
}
const char *x11_probe_code(void){return desktop_backend()==DESKTOP_X11?"X11_DEVELOPMENT_DEPS_MISSING":"X11_SESSION_NOT_SELECTED";}
#endif
