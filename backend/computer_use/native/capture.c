#include "capture.h"
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
bool capture_copy_rgb(uint8_t *dst,size_t capacity,const uint8_t *src,size_t maxsize,size_t offset,size_t size,int32_t stride,uint32_t width,uint32_t height,bool bgr) {
    if(!dst||!src||!width||!height||width>16384||height>16384||stride<0)return false;
    size_t row=(size_t)width*4,need=(size_t)stride*(height-1)+row;
    if((size_t)stride<row||offset>maxsize||size>maxsize-offset||need>size||(size_t)width*height*3>capacity)return false;
    for(uint32_t y=0;y<height;y++)for(uint32_t x=0;x<width;x++) {
        const uint8_t *p=src+offset+(size_t)y*stride+x*4;
        uint8_t *q=dst+((size_t)y*width+x)*3;
        q[0]=p[bgr?2:0];
        q[1]=p[1];
        q[2]=p[bgr?0:2];
    }
    return true;
}
/* GNOME Mutter emits SPA Header PTS from g_get_monotonic_time (46/48
 * meta-screen-cast-stream-src.c). This backend admits that domain only: no
 * arrival-offset rebasing, absent/old/future PTS fails closed. Mutter 46 leaves
 * seq zero; newer versions supply seq, which must also advance when present. */
static const char *capture_timestamp_rejection(const CaptureClock *clock,int64_t pts,uint64_t sequence,int64_t opened_ns,int64_t now_ns) {
    if(!clock||pts<=0)return "PIPEWIRE_PTS_MISSING";
    if(pts<opened_ns)return "PIPEWIRE_PTS_BEFORE_CAPTURE_OPEN";
    if(pts>now_ns)return "PIPEWIRE_PTS_FUTURE";
    if(pts<=clock->pts)return "PIPEWIRE_PTS_NONADVANCING";
    if(clock->sequence&&sequence<=clock->sequence)return "PIPEWIRE_SEQUENCE_NONADVANCING";
    return NULL;
}
bool capture_accept_timestamp(CaptureClock *clock,int64_t pts,uint64_t sequence,int64_t opened_ns,int64_t now_ns,double *timestamp) {
    if(!timestamp||capture_timestamp_rejection(clock,pts,sequence,opened_ns,now_ns))return false;
    clock->pts=pts;
    clock->sequence=sequence;
    *timestamp=pts/1e9;
    return true;
}
#ifdef HAVE_GIO_PIPEWIRE_DEPS
#include <pipewire/pipewire.h>
#include <spa/param/video/format-utils.h>
#include <spa/buffer/meta.h>
#include <time.h>
struct Capture {
    struct pw_thread_loop *loop;
    struct pw_context *context;
    struct pw_core *core;
    struct pw_stream *stream;
    struct spa_hook listener;
    struct spa_hook core_listener;
    struct spa_video_info_raw info;
    CaptureFrame frame;
    size_t max_bytes;
    const char *error;
    bool configured,committed,streaming_seen,buffer_seen,buffer_after_format_seen;
    bool frame_accepted_once;
    const char *last_rejection;
    uint64_t process_callbacks,rejected_buffers;
    const char *last_runtime_rejection;
    enum pw_stream_state observed_stream_state;
    bool stream_state_seen;
    CaptureClock source_clock;
    int64_t opened_ns;
};
static int64_t monotonic_ns(void){
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC,&t);
    return (int64_t)t.tv_sec*1000000000LL+t.tv_nsec;
}
static void core_error(void *data,uint32_t id,int seq,int res,const char *message){
    (void)id;
    (void)seq;
    (void)message;
    Capture *c=data;
    if(c->error)return;
    /* Only stable allowlisted codes cross IPC. Core diagnostics may otherwise
     * include arbitrary server text. Literals remain valid after teardown. */
    switch(res){
    case -EPIPE:c->error="PIPEWIRE_CORE_ERROR_EPIPE";break;
    case -EACCES:c->error="PIPEWIRE_CORE_ERROR_EACCES";break;
    case -EPERM:c->error="PIPEWIRE_CORE_ERROR_EPERM";break;
    case -ENOENT:c->error="PIPEWIRE_CORE_ERROR_ENOENT";break;
    case -EINVAL:c->error="PIPEWIRE_CORE_ERROR_EINVAL";break;
    case -EIO:c->error="PIPEWIRE_CORE_ERROR_EIO";break;
    case -ENOTSUP:c->error="PIPEWIRE_CORE_ERROR_ENOTSUP";break;
    case -ENOMEM:c->error="PIPEWIRE_CORE_ERROR_ENOMEM";break;
    default:c->error="PIPEWIRE_CORE_ERROR_OTHER";break;
    }
}
static const struct pw_core_events core_events={
    PW_VERSION_CORE_EVENTS,.error=core_error
};
static const char *stream_error_code(const char *message){
    /* Match fixed upstream PipeWire context/impl-link diagnostic prefixes.
     * Never copy arbitrary text, paths, IDs, or suffixes into the protocol. */
    static const struct {const char *prefix,*code;} known[]={
        {"no more input formats","PIPEWIRE_STREAM_ERROR_NO_INPUT_FORMAT"},
        {"no input format","PIPEWIRE_STREAM_ERROR_NO_INPUT_FORMAT"},
        {"no output formats","PIPEWIRE_STREAM_ERROR_NO_OUTPUT_FORMAT"},
        {"error input enum formats:","PIPEWIRE_STREAM_ERROR_INPUT_FORMAT_ENUM"},
        {"error output enum formats:","PIPEWIRE_STREAM_ERROR_OUTPUT_FORMAT_ENUM"},
        {"error get input format:","PIPEWIRE_STREAM_ERROR_INPUT_FORMAT_GET"},
        {"error get output format:","PIPEWIRE_STREAM_ERROR_OUTPUT_FORMAT_GET"},
        {"error set input format:","PIPEWIRE_STREAM_ERROR_INPUT_FORMAT_SET"},
        {"error set output format:","PIPEWIRE_STREAM_ERROR_OUTPUT_FORMAT_SET"},
        {"error alloc buffers:","PIPEWIRE_STREAM_ERROR_BUFFER_ALLOCATION"},
        {"error use input buffers:","PIPEWIRE_STREAM_ERROR_INPUT_BUFFERS"},
        {"error use output buffers:","PIPEWIRE_STREAM_ERROR_OUTPUT_BUFFERS"},
        {"error bad node state","PIPEWIRE_STREAM_ERROR_BAD_NODE_STATE"},
        {"Permission denied","PIPEWIRE_STREAM_ERROR_PERMISSION"},
        {"Operation not permitted","PIPEWIRE_STREAM_ERROR_PERMISSION"}
    };
    if(message)for(size_t i=0;i<sizeof(known)/sizeof(known[0]);i++)
        if(!strncmp(message,known[i].prefix,strlen(known[i].prefix)))return known[i].code;
    return "PIPEWIRE_STREAM_ERROR";
}
static void state_changed(void *data,enum pw_stream_state old,enum pw_stream_state state,const char *error) {
    Capture *c=data;
    c->observed_stream_state=state;
    c->stream_state_seen=true;
    if(state==PW_STREAM_STATE_STREAMING)c->streaming_seen=true;
    /* Retain the first terminal reason: proxy destruction can report
     * UNCONNECTED after the actual error. Never expose arbitrary server text. */
    if(c->error)return;
    if(state==PW_STREAM_STATE_ERROR)c->error=stream_error_code(error);
    else if(state==PW_STREAM_STATE_UNCONNECTED&&old!=PW_STREAM_STATE_UNCONNECTED)
        c->error=c->streaming_seen?"PIPEWIRE_DISCONNECTED_AFTER_STREAMING":
                                  "PIPEWIRE_DISCONNECTED_BEFORE_STREAMING";
}
static void format_changed(void *data,uint32_t id,const struct spa_pod *param) {
    Capture *c=data;
    if(c->error||id!=SPA_PARAM_Format||!param)return;
    struct spa_video_info_raw info={
        0
    };
    if(spa_format_video_raw_parse(param,&info)<0||!info.size.width||!info.size.height||info.size.width>16384||info.size.height>16384||        (info.format!=SPA_VIDEO_FORMAT_BGRx&&info.format!=SPA_VIDEO_FORMAT_BGRA&&info.format!=SPA_VIDEO_FORMAT_RGBx&&info.format!=SPA_VIDEO_FORMAT_RGBA)||        (info.flags&SPA_VIDEO_FLAG_MODIFIER)||(size_t)info.size.width*info.size.height*3>c->max_bytes){
        c->error="PIPEWIRE_FORMAT_UNSUPPORTED";
        return;
    }
    if(c->configured&&(info.size.width!=c->info.size.width||info.size.height!=c->info.size.height||info.format!=c->info.format)) {
        if(c->committed){
            c->error="PIPEWIRE_GEOMETRY_CHANGED";
            c->frame.geometry++;
            return;
        }
        /* Initial SPA negotiation can revise the default format before a
         * binding is published. Never retain pixels decoded for that format. */
        capture_frame_clear(&c->frame);
    }
    c->info=info;
    c->configured=true;
    c->last_rejection=NULL;
    c->buffer_after_format_seen=false;
    uint8_t buffer[1024];
    struct spa_pod_builder b=SPA_POD_BUILDER_INIT(buffer,sizeof(buffer));
    const struct spa_pod *params[]={
        spa_pod_builder_add_object(&b,SPA_TYPE_OBJECT_ParamBuffers,SPA_PARAM_Buffers,
            SPA_PARAM_BUFFERS_buffers,SPA_POD_CHOICE_RANGE_Int(8,1,32),
            SPA_PARAM_BUFFERS_blocks,SPA_POD_Int(1),
            SPA_PARAM_BUFFERS_size,SPA_POD_Int((int32_t)(info.size.width*info.size.height*4)),
            SPA_PARAM_BUFFERS_stride,SPA_POD_Int((int32_t)(info.size.width*4)),
            SPA_PARAM_BUFFERS_dataType,SPA_POD_CHOICE_FLAGS_Int((1<<SPA_DATA_MemPtr)|(1<<SPA_DATA_MemFd))),
        spa_pod_builder_add_object(&b,SPA_TYPE_OBJECT_ParamMeta,SPA_PARAM_Meta,
            SPA_PARAM_META_type,SPA_POD_Id(SPA_META_Header),SPA_PARAM_META_size,SPA_POD_Int(sizeof(struct spa_meta_header))),
        spa_pod_builder_add_object(&b,SPA_TYPE_OBJECT_ParamMeta,SPA_PARAM_Meta,
            SPA_PARAM_META_type,SPA_POD_Id(SPA_META_VideoCrop),SPA_PARAM_META_size,SPA_POD_Int(sizeof(struct spa_meta_region))),
        spa_pod_builder_add_object(&b,SPA_TYPE_OBJECT_ParamMeta,SPA_PARAM_Meta,
            SPA_PARAM_META_type,SPA_POD_Id(SPA_META_VideoTransform),SPA_PARAM_META_size,SPA_POD_Int(sizeof(struct spa_meta_videotransform)))
    };
    pw_stream_update_params(c->stream,params,4);
}
static void record_rejection(Capture *c,const char *code){
    c->rejected_buffers++;
    c->last_runtime_rejection=code;
}
static void process(void *data) {
    Capture *c=data;
    c->process_callbacks++;
    struct pw_buffer *p;
    while((p=pw_stream_dequeue_buffer(c->stream))) {
        c->buffer_seen=true;
        struct spa_buffer *b=p->buffer;
        if(c->error||!c->configured){
            if(!c->error)record_rejection(c,"PIPEWIRE_BUFFER_BEFORE_FORMAT");
            goto next;
        }
        c->buffer_after_format_seen=true;
        if(!b||b->n_datas!=1){
            if(!c->last_rejection)c->last_rejection="PIPEWIRE_INVALID_PLANE_COUNT";
            record_rejection(c,"PIPEWIRE_INVALID_PLANE_COUNT");
            goto next;
        }
        struct spa_meta_header *header=spa_buffer_find_meta_data(b,SPA_META_Header,sizeof(*header));
        struct spa_meta_region *crop=spa_buffer_find_meta_data(b,SPA_META_VideoCrop,sizeof(*crop));
        struct spa_meta_videotransform *transform=spa_buffer_find_meta_data(b,SPA_META_VideoTransform,sizeof(*transform));
        /* Cursor-only, empty or corrupt packets carry no new desktop image.
         * Requeue them without changing the retained image or its clock. */
        struct spa_data *d=&b->datas[0];
        if(!d->chunk){c->error="PIPEWIRE_BUFFER_INVALID";goto next;}
        if(!d->chunk->size || (d->chunk->flags&(SPA_CHUNK_FLAG_CORRUPTED|SPA_CHUNK_FLAG_EMPTY)) ||
           (header&&(header->flags&(SPA_META_HEADER_FLAG_CORRUPTED|SPA_META_HEADER_FLAG_GAP)))){
            if(!c->last_rejection)c->last_rejection="PIPEWIRE_ONLY_EMPTY_BUFFERS";
            record_rejection(c,"PIPEWIRE_ONLY_EMPTY_BUFFERS");
            goto next;
        }
        if((crop&&spa_meta_region_is_valid(crop)&&(crop->region.position.x||crop->region.position.y||crop->region.size.width!=c->info.size.width||crop->region.size.height!=c->info.size.height))||
           (transform&&transform->transform!=SPA_META_TRANSFORMATION_None)){
            c->error="PIPEWIRE_COVERAGE_UNPROVEN";goto next;
        }
        if(!d->data||(d->type!=SPA_DATA_MemPtr&&d->type!=SPA_DATA_MemFd)) {
            c->error="PIPEWIRE_BUFFER_UNSUPPORTED";goto next;
        }
        double source_time;
        if(!header){
            c->last_rejection="PIPEWIRE_HEADER_MISSING";
            record_rejection(c,c->last_rejection);
            goto next;
        }
        int64_t now_ns=monotonic_ns();
        c->last_rejection=capture_timestamp_rejection(&c->source_clock,header->pts,header->seq,
                                                      c->opened_ns,now_ns);
        if(c->last_rejection||!capture_accept_timestamp(&c->source_clock,header->pts,header->seq,
                                                        c->opened_ns,now_ns,&source_time)){
            record_rejection(c,c->last_rejection?c->last_rejection:"PIPEWIRE_TIMESTAMP_REJECTED");
            goto next;
        }
        size_t bytes=(size_t)c->info.size.width*c->info.size.height*3;
        uint8_t *rgb=malloc(bytes);
        if(!rgb){
            c->error="FRAME_ALLOCATION_FAILED";
            goto next;
        }
        if(!capture_copy_rgb(rgb,bytes,d->data,d->maxsize,d->chunk->offset,d->chunk->size,d->chunk->stride,c->info.size.width,c->info.size.height,(c->info.format==SPA_VIDEO_FORMAT_BGRx||c->info.format==SPA_VIDEO_FORMAT_BGRA))) {
            free(rgb);
            c->error="PIPEWIRE_BUFFER_INVALID";
            goto next;
        }
        free(c->frame.rgb);
        c->frame.rgb=rgb;
        c->frame.bytes=bytes;
        c->frame.width=c->info.size.width;
        c->frame.height=c->info.size.height;
        c->frame.sequence++;
        c->frame.timestamp=source_time;
        c->frame.geometry=1;
        c->frame_accepted_once=true;
        c->last_rejection=NULL;
        next: if(pw_stream_queue_buffer(c->stream,p)<0&&!c->error)
            c->error="PIPEWIRE_BUFFER_REQUEUE_FAILED";
    }
}
static const struct pw_stream_events events={
    PW_VERSION_STREAM_EVENTS,.state_changed=state_changed,.param_changed=format_changed,.process=process
};
/* Offer independent packed CPU formats, matching established Portal
 * capturers. Geometry is negotiated from the producer, never Portal logical
 * dimensions. A modifier is deliberately absent: no DMA-BUF support. */
static uint32_t capture_build_formats(struct spa_pod_builder *builder,const struct spa_pod **params){
    const uint32_t formats[]={SPA_VIDEO_FORMAT_BGRA,SPA_VIDEO_FORMAT_RGBA,
        SPA_VIDEO_FORMAT_BGRx,SPA_VIDEO_FORMAT_RGBx};
    struct spa_rectangle initial=SPA_RECTANGLE(1,1);
    struct spa_rectangle minimum=SPA_RECTANGLE(1,1);
    struct spa_rectangle maximum=SPA_RECTANGLE(16384,16384);
    for(uint32_t i=0;i<4;i++){
        params[i]=spa_pod_builder_add_object(builder,SPA_TYPE_OBJECT_Format,SPA_PARAM_EnumFormat,
            SPA_FORMAT_mediaType,SPA_POD_Id(SPA_MEDIA_TYPE_video),
            SPA_FORMAT_mediaSubtype,SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
            SPA_FORMAT_VIDEO_format,SPA_POD_Id(formats[i]),
            SPA_FORMAT_VIDEO_size,SPA_POD_CHOICE_RANGE_Rectangle(&initial,&minimum,&maximum));
        if(!params[i])return i;
    }
    return 4;
}
Capture *capture_open(int fd,uint32_t node,uint64_t serial,size_t max_bytes) {
    pw_init(NULL,NULL);
    Capture *c=calloc(1,sizeof(*c));
    if(!c){
        close(fd);
        return NULL;
    }
    c->max_bytes=max_bytes;
    c->opened_ns=monotonic_ns();
    c->loop=pw_thread_loop_new("computer-use-capture",NULL);
    if(!c->loop){
        close(fd);
        goto fail;
    }
    c->context=pw_context_new(pw_thread_loop_get_loop(c->loop),NULL,0);
    if(!c->context){
        close(fd);
        goto fail;
    }
    c->core=pw_context_connect_fd(c->context,fd,NULL,0);
    if(!c->core)goto fail;
    /* Subscribe before the stream so the originating errno survives any
     * subsequent stream UNCONNECTED notification for the same core error. */
    pw_core_add_listener(c->core,&c->core_listener,&core_events,c);
    struct pw_properties *props=pw_properties_new(PW_KEY_MEDIA_TYPE,"Video",PW_KEY_MEDIA_CATEGORY,"Capture",PW_KEY_MEDIA_ROLE,"Screen",NULL);
    if(serial)pw_properties_setf(props,PW_KEY_TARGET_OBJECT,"%llu",(unsigned long long)serial);
    c->stream=pw_stream_new(c->core,"computer-use-monitor",props);
    if(!c->stream)goto fail;
    pw_stream_add_listener(c->stream,&c->listener,&events,c);
    uint8_t buffer[2048];
    struct spa_pod_builder builder=SPA_POD_BUILDER_INIT(buffer,sizeof(buffer));
    const struct spa_pod *params[4];
    uint32_t count=capture_build_formats(&builder,params);
    if(count!=4)goto fail;
    if(pw_stream_connect(c->stream,PW_DIRECTION_INPUT,serial?PW_ID_ANY:node,
                        PW_STREAM_FLAG_AUTOCONNECT|PW_STREAM_FLAG_MAP_BUFFERS,params,count)<0)goto fail;
    if(pw_thread_loop_start(c->loop)<0)goto fail;
    return c;
    fail:capture_close(c);
    return NULL;
}
void capture_close(Capture *c){
    if(!c)return;
    if(c->loop)pw_thread_loop_stop(c->loop);
    if(c->stream)pw_stream_destroy(c->stream);
    if(c->core){
        spa_hook_remove(&c->core_listener);
        pw_core_disconnect(c->core);
    }
    if(c->context)pw_context_destroy(c->context);
    if(c->loop)pw_thread_loop_destroy(c->loop);
    free(c->frame.rgb);
    free(c);
}
static bool snapshot(Capture *c,uint64_t last_sequence,bool require_newer,CaptureFrame *out){
    if(!c||!out)return false;
    bool ok=false;
    pw_thread_loop_lock(c->loop);
    if(!c->error&&c->frame.rgb&&(!require_newer||c->frame.sequence>last_sequence)){
        CaptureFrame copy=c->frame;
        copy.rgb=malloc(copy.bytes);
        if(copy.rgb){
            memcpy(copy.rgb,c->frame.rgb,copy.bytes);
            *out=copy;
            ok=true;
        }
    }pw_thread_loop_unlock(c->loop);
    return ok;
}
bool capture_snapshot(Capture *c,CaptureFrame *out){
    return snapshot(c,0,false,out);
}
bool capture_snapshot_newer(Capture *c,uint64_t last_sequence,CaptureFrame *out){
    return snapshot(c,last_sequence,true,out);
}
CaptureProgress capture_progress(Capture *c){
    CaptureProgress progress={0};
    if(!c)return progress;
    pw_thread_loop_lock(c->loop);
    progress=(CaptureProgress){c->process_callbacks,c->rejected_buffers};
    pw_thread_loop_unlock(c->loop);
    return progress;
}
const char *capture_freeze_timeout_status(Capture *c,CaptureProgress start,uint64_t last_sequence){
    if(!c)return "PIPEWIRE_UNAVAILABLE";
    pw_thread_loop_lock(c->loop);
    const char *status;
    if(c->error)status=c->error;
    else if(c->frame.rgb&&c->frame.sequence>last_sequence)status="PIPEWIRE_NEW_FRAME_COPY_FAILED";
    else if(c->process_callbacks==start.process_callbacks){
        if(c->stream_state_seen&&c->observed_stream_state==PW_STREAM_STATE_PAUSED)
            status="PIPEWIRE_STREAM_PAUSED";
        else if(c->stream_state_seen&&c->observed_stream_state==PW_STREAM_STATE_CONNECTING)
            status="PIPEWIRE_STREAM_CONNECTING";
        else status="PIPEWIRE_NO_PROCESS_CALLBACK";
    }
    else if(c->rejected_buffers!=start.rejected_buffers)
        status=c->last_runtime_rejection?c->last_runtime_rejection:"PIPEWIRE_BUFFER_REJECTED";
    else status="PIPEWIRE_PROCESS_CALLBACK_NO_BUFFER";
    pw_thread_loop_unlock(c->loop);
    return status;
}
bool capture_commit(Capture *c,const CaptureFrame *frame,bool (*commit_binding)(void *),void *binding){
    if(!c||!frame)return false;
    pw_thread_loop_lock(c->loop);
    bool valid=!c->error&&c->frame.rgb&&frame->width==c->info.size.width&&
        frame->height==c->info.size.height&&frame->width==c->frame.width&&
        frame->height==c->frame.height&&frame->sequence==c->frame.sequence&&
        frame->timestamp==c->frame.timestamp;
    /* Keep format callbacks excluded until both sides of the binding commit.
     * A failed EIS admission must leave capture geometry provisional. */
    if(valid&&commit_binding)valid=commit_binding(binding);
    if(valid)c->committed=true;
    pw_thread_loop_unlock(c->loop);
    return valid;
}
const char *capture_error(Capture *c){
    if(!c)return "PIPEWIRE_UNAVAILABLE";
    pw_thread_loop_lock(c->loop);
    const char *error=c->error;
    pw_thread_loop_unlock(c->loop);
    return error;
}
const char *capture_startup_status(Capture *c){
    if(!c)return "PIPEWIRE_UNAVAILABLE";
    if(c->loop)pw_thread_loop_lock(c->loop);
    const char *status=c->error;
    if(!status&&c->frame.rgb)status=NULL;
    else if(!status&&c->frame_accepted_once)status="PIPEWIRE_FRAME_LOST_ON_FORMAT_CHANGE";
    else if(!status&&!c->streaming_seen)status="PIPEWIRE_STREAM_NOT_STREAMING";
    else if(!status&&!c->configured)status="PIPEWIRE_FORMAT_NOT_CONFIGURED";
    else if(!status&&!c->buffer_seen)status="PIPEWIRE_NO_BUFFER";
    else if(!status&&!c->buffer_after_format_seen)status="PIPEWIRE_BUFFER_BEFORE_FORMAT";
    else if(!status)status=c->last_rejection?c->last_rejection:"PIPEWIRE_BUFFER_NOT_PUBLISHED";
    if(c->loop)pw_thread_loop_unlock(c->loop);
    return status;
}
#else
bool capture_commit(Capture *c,const CaptureFrame *frame,bool (*commit_binding)(void *),void *binding){(void)c;(void)frame;(void)commit_binding;(void)binding;return false;}
struct Capture{
    int unused;
};
Capture *capture_open(int fd,uint32_t node,uint64_t serial,size_t max){
    (void)node;
    (void)serial;
    (void)max;
    close(fd);
    return NULL;
}
void capture_close(Capture *c){
    (void)c;
}
bool capture_snapshot(Capture *c,CaptureFrame *out){
    (void)c;
    (void)out;
    return false;
}
bool capture_snapshot_newer(Capture *c,uint64_t last_sequence,CaptureFrame *out){
    (void)c;
    (void)last_sequence;
    (void)out;
    return false;
}
CaptureProgress capture_progress(Capture *c){
    (void)c;
    return (CaptureProgress){0};
}
const char *capture_freeze_timeout_status(Capture *c,CaptureProgress start,uint64_t last_sequence){
    (void)c;
    (void)start;
    (void)last_sequence;
    return "PIPEWIRE_UNAVAILABLE";
}
const char *capture_error(Capture *c){
    (void)c;
    return "PIPEWIRE_UNAVAILABLE";
}
const char *capture_startup_status(Capture *c){
    (void)c;
    return "PIPEWIRE_UNAVAILABLE";
}
#endif
void capture_frame_clear(CaptureFrame *f){
    if(f){
        free(f->rgb);
        memset(f,0,sizeof(*f));
    }
}
bool capture_freeze(void){
    return false;
}
