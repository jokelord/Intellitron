/* Exercise actual SPA format parsing and the capture publication boundary.
 * No PipeWire connection or desktop capture is created. */
#include <assert.h>
#include "capture.c"
static struct pw_buffer *pending_buffer;
static int queue_result;
struct pw_buffer *pw_stream_dequeue_buffer(struct pw_stream *stream) {
    (void)stream;
    struct pw_buffer *buffer=pending_buffer;
    pending_buffer=NULL;
    return buffer;
}
int pw_stream_queue_buffer(struct pw_stream *stream,struct pw_buffer *buffer) {
    (void)stream;(void)buffer;
    return queue_result;
}
int pw_stream_update_params(struct pw_stream *stream,const struct spa_pod **params,uint32_t count) {
    (void)stream;assert(params&&count==4);
    int32_t size=0,stride=0;
    assert(spa_pod_parse_object(params[0],SPA_TYPE_OBJECT_ParamBuffers,NULL,
        SPA_PARAM_BUFFERS_size,SPA_POD_Int(&size),
        SPA_PARAM_BUFFERS_stride,SPA_POD_Int(&stride))>=0);
    const struct spa_pod_prop *counts=spa_pod_find_prop(params[0],NULL,SPA_PARAM_BUFFERS_buffers);
    assert(counts&&spa_pod_is_choice(&counts->value));
    assert(SPA_POD_CHOICE_TYPE(&counts->value)==SPA_CHOICE_Range);
    const int32_t *values=SPA_POD_CHOICE_VALUES(&counts->value);
    assert(values[0]==8&&values[1]==1&&values[2]==32);
    assert(size>0&&stride>0&&size%stride==0);
    return 0;
}
static void negotiate(Capture *capture,uint32_t width,uint32_t height) {
    uint8_t bytes[256];
    struct spa_pod_builder builder=SPA_POD_BUILDER_INIT(bytes,sizeof(bytes));
    struct spa_rectangle size=SPA_RECTANGLE(width,height);
    const struct spa_pod *format=spa_pod_builder_add_object(&builder,SPA_TYPE_OBJECT_Format,SPA_PARAM_Format,
        SPA_FORMAT_mediaType,SPA_POD_Id(SPA_MEDIA_TYPE_video),
        SPA_FORMAT_mediaSubtype,SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
        SPA_FORMAT_VIDEO_format,SPA_POD_Id(SPA_VIDEO_FORMAT_BGRx),
        SPA_FORMAT_VIDEO_size,SPA_POD_Rectangle(&size));
    format_changed(capture,SPA_PARAM_Format,format);
}
static bool admission(void *value){return *(bool *)value;}
static CaptureFrame sample(Capture *capture){
    capture->frame=(CaptureFrame){.rgb=malloc(1),.width=capture->info.size.width,
        .height=capture->info.size.height,.sequence=1,.timestamp=12.0};
    return capture->frame;
}
int main(void) {
    Capture early={.max_bytes=1024,.streaming_seen=true};
    struct pw_buffer early_buffer={0};
    pending_buffer=&early_buffer;
    process(&early);
    assert(early.buffer_seen);
    Capture requeue={0};
    pending_buffer=&early_buffer;
    queue_result=-EIO;
    process(&requeue);
    assert(!strcmp(requeue.error,"PIPEWIRE_BUFFER_REQUEUE_FAILED"));
    queue_result=0;
    negotiate(&early,2,1);
    assert(!strcmp(capture_startup_status(&early),"PIPEWIRE_BUFFER_BEFORE_FORMAT"));
    Capture revised={.max_bytes=1024,.streaming_seen=true};
    negotiate(&revised,2,1);
    sample(&revised);
    revised.frame_accepted_once=true;
    negotiate(&revised,3,1);
    assert(!strcmp(capture_startup_status(&revised),"PIPEWIRE_FRAME_LOST_ON_FORMAT_CHANGE"));
    capture_frame_clear(&revised.frame);
    Capture startup={0};
    assert(!strcmp(capture_startup_status(&startup),"PIPEWIRE_STREAM_NOT_STREAMING"));
    startup.streaming_seen=true;
    assert(!strcmp(capture_startup_status(&startup),"PIPEWIRE_FORMAT_NOT_CONFIGURED"));
    startup.configured=true;
    assert(!strcmp(capture_startup_status(&startup),"PIPEWIRE_NO_BUFFER"));
    startup.buffer_seen=true;
    startup.buffer_after_format_seen=true;
    startup.last_rejection="PIPEWIRE_PTS_BEFORE_CAPTURE_OPEN";
    assert(!strcmp(capture_startup_status(&startup),"PIPEWIRE_PTS_BEFORE_CAPTURE_OPEN"));
    startup.frame.rgb=(uint8_t *)"x";
    assert(capture_startup_status(&startup)==NULL);
    assert(!strcmp(capture_timestamp_rejection(&(CaptureClock){0},99,0,100,200),"PIPEWIRE_PTS_BEFORE_CAPTURE_OPEN"));
    assert(!strcmp(capture_timestamp_rejection(&(CaptureClock){0},201,0,100,200),"PIPEWIRE_PTS_FUTURE"));
    assert(capture_timestamp_rejection(&(CaptureClock){0},150,0,100,200)==NULL);
    uint8_t offer_bytes[2048];
    struct spa_pod_builder offer_builder=SPA_POD_BUILDER_INIT(offer_bytes,sizeof(offer_bytes));
    const struct spa_pod *offers[4];
    assert(capture_build_formats(&offer_builder,offers)==4);
    const uint32_t expected[]={SPA_VIDEO_FORMAT_BGRA,SPA_VIDEO_FORMAT_RGBA,
        SPA_VIDEO_FORMAT_BGRx,SPA_VIDEO_FORMAT_RGBx};
    for(size_t i=0;i<4;i++){
        struct spa_video_info_raw info={0};
        assert(offers[i]&&spa_format_video_raw_parse(offers[i],&info)>=0);
        assert(info.format==expected[i]&&!(info.flags&SPA_VIDEO_FLAG_MODIFIER));
        const struct spa_pod_prop *format=spa_pod_find_prop(offers[i],NULL,SPA_FORMAT_VIDEO_format);
        assert(format&&SPA_POD_TYPE(&format->value)==SPA_TYPE_Id);
        const struct spa_pod_prop *size=spa_pod_find_prop(offers[i],NULL,SPA_FORMAT_VIDEO_size);
        assert(size&&spa_pod_is_choice(&size->value));
        assert(SPA_POD_CHOICE_TYPE(&size->value)==SPA_CHOICE_Range);
        const struct spa_rectangle *range=SPA_POD_CHOICE_VALUES(&size->value);
        assert(range[1].width==1&&range[1].height==1);
        assert(range[2].width==16384&&range[2].height==16384);
    }
    Capture bgra={.max_bytes=1024};
    uint8_t selected_bytes[256];
    struct spa_pod_builder selected_builder=SPA_POD_BUILDER_INIT(selected_bytes,sizeof(selected_bytes));
    struct spa_rectangle selected_size=SPA_RECTANGLE(2,1);
    const struct spa_pod *selected=spa_pod_builder_add_object(&selected_builder,
        SPA_TYPE_OBJECT_Format,SPA_PARAM_Format,
        SPA_FORMAT_mediaType,SPA_POD_Id(SPA_MEDIA_TYPE_video),
        SPA_FORMAT_mediaSubtype,SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
        SPA_FORMAT_VIDEO_format,SPA_POD_Id(SPA_VIDEO_FORMAT_BGRA),
        SPA_FORMAT_VIDEO_size,SPA_POD_Rectangle(&selected_size));
    format_changed(&bgra,SPA_PARAM_Format,selected);
    assert(!bgra.error&&bgra.info.format==SPA_VIDEO_FORMAT_BGRA);
    uint8_t bgra_pixels[]={3,2,1,255,6,5,4,127},rgb[6];
    assert(capture_copy_rgb(rgb,sizeof(rgb),bgra_pixels,sizeof(bgra_pixels),0,8,8,2,1,
        bgra.info.format==SPA_VIDEO_FORMAT_BGRx||bgra.info.format==SPA_VIDEO_FORMAT_BGRA));
    assert(!memcmp(rgb,(uint8_t[]){1,2,3,4,5,6},6));
    Capture negotiation={0};
    state_changed(&negotiation,PW_STREAM_STATE_PAUSED,PW_STREAM_STATE_ERROR,
        "error alloc buffers: private server detail");
    assert(!strcmp(negotiation.error,"PIPEWIRE_STREAM_ERROR_BUFFER_ALLOCATION"));
    Capture formats={0};
    state_changed(&formats,PW_STREAM_STATE_PAUSED,PW_STREAM_STATE_ERROR,"no more input formats");
    assert(!strcmp(formats.error,"PIPEWIRE_STREAM_ERROR_NO_INPUT_FORMAT"));
    Capture core={0};
    core_error(&core,PW_ID_CORE,1,-EPIPE,"private server text");
    assert(!strcmp(core.error,"PIPEWIRE_CORE_ERROR_EPIPE"));
    state_changed(&core,PW_STREAM_STATE_CONNECTING,PW_STREAM_STATE_UNCONNECTED,NULL);
    core_error(&core,PW_ID_CORE,2,-EIO,"later failure");
    assert(!strcmp(core.error,"PIPEWIRE_CORE_ERROR_EPIPE"));
    Capture unknown={0};
    core_error(&unknown,99,0,-12345,"private detail");
    assert(!strcmp(unknown.error,"PIPEWIRE_CORE_ERROR_OTHER"));
    Capture states={0};
    state_changed(&states,PW_STREAM_STATE_UNCONNECTED,PW_STREAM_STATE_UNCONNECTED,NULL);
    assert(!states.error);
    state_changed(&states,PW_STREAM_STATE_CONNECTING,PW_STREAM_STATE_ERROR,"private detail");
    assert(!strcmp(states.error,"PIPEWIRE_STREAM_ERROR"));
    state_changed(&states,PW_STREAM_STATE_ERROR,PW_STREAM_STATE_UNCONNECTED,NULL);
    assert(!strcmp(states.error,"PIPEWIRE_STREAM_ERROR"));
    Capture disconnected={0};
    state_changed(&disconnected,PW_STREAM_STATE_CONNECTING,PW_STREAM_STATE_UNCONNECTED,NULL);
    assert(!strcmp(disconnected.error,"PIPEWIRE_DISCONNECTED_BEFORE_STREAMING"));
    Capture running={0};
    state_changed(&running,PW_STREAM_STATE_PAUSED,PW_STREAM_STATE_STREAMING,NULL);
    state_changed(&running,PW_STREAM_STATE_STREAMING,PW_STREAM_STATE_UNCONNECTED,NULL);
    assert(!strcmp(running.error,"PIPEWIRE_DISCONNECTED_AFTER_STREAMING"));
    Capture capture={.max_bytes=32*1024*1024};
    negotiate(&capture,1280,720);
    assert(capture.configured&&!capture.error);
    negotiate(&capture,1920,1080);
    assert(!capture.error&&capture.info.size.width==1920);
    pw_init(NULL,NULL);
    capture.loop=pw_thread_loop_new("capture-fixture",NULL);assert(capture.loop);
    Capture diagnostic={.loop=capture.loop,.configured=true};
    CaptureProgress progress=capture_progress(&diagnostic);
    assert(!strcmp(capture_freeze_timeout_status(&diagnostic,progress,0),
        "PIPEWIRE_NO_PROCESS_CALLBACK"));
    state_changed(&diagnostic,PW_STREAM_STATE_STREAMING,PW_STREAM_STATE_PAUSED,NULL);
    assert(!strcmp(capture_freeze_timeout_status(&diagnostic,progress,0),
        "PIPEWIRE_STREAM_PAUSED"));
    state_changed(&diagnostic,PW_STREAM_STATE_PAUSED,PW_STREAM_STATE_STREAMING,NULL);
    assert(!strcmp(capture_freeze_timeout_status(&diagnostic,progress,0),
        "PIPEWIRE_NO_PROCESS_CALLBACK"));
    process(&diagnostic);
    assert(!strcmp(capture_freeze_timeout_status(&diagnostic,progress,0),
        "PIPEWIRE_PROCESS_CALLBACK_NO_BUFFER"));
    progress=capture_progress(&diagnostic);
    struct spa_buffer invalid_plane={0};
    struct pw_buffer invalid_packet={.buffer=&invalid_plane};
    pending_buffer=&invalid_packet;
    process(&diagnostic);
    assert(!strcmp(capture_freeze_timeout_status(&diagnostic,progress,0),
        "PIPEWIRE_INVALID_PLANE_COUNT"));
    progress=capture_progress(&diagnostic);
    struct spa_chunk empty_chunk={0};
    struct spa_data empty_data={.chunk=&empty_chunk};
    struct spa_buffer empty_plane={.n_datas=1,.datas=&empty_data};
    struct pw_buffer empty_packet={.buffer=&empty_plane};
    pending_buffer=&empty_packet;
    process(&diagnostic);
    assert(!strcmp(capture_freeze_timeout_status(&diagnostic,progress,0),
        "PIPEWIRE_ONLY_EMPTY_BUFFERS"));
    diagnostic.frame.rgb=(uint8_t *)"x";
    diagnostic.frame.sequence=2;
    assert(!strcmp(capture_freeze_timeout_status(&diagnostic,progress,1),
        "PIPEWIRE_NEW_FRAME_COPY_FAILED"));
    CaptureFrame first=sample(&capture);
    bool admitted=false;
    assert(!capture_commit(&capture,&first,admission,&admitted));
    assert(!capture.committed);
    /* EIS admission failed: a subsequent startup format change is still legal. */
    negotiate(&capture,1280,720);assert(!capture.error&&!capture.frame.rgb);
    negotiate(&capture,1920,1080);assert(!capture.error);
    CaptureFrame current=sample(&capture);
    capture.frame.bytes=1;
    capture.frame.rgb[0]=0x5a;
    CaptureFrame snapshot={0};
    assert(!capture_snapshot_newer(&capture,current.sequence,&snapshot));
    assert(snapshot.rgb==NULL);
    assert(capture_snapshot_newer(&capture,current.sequence-1,&snapshot));
    assert(snapshot.sequence==current.sequence&&snapshot.bytes==1);
    assert(snapshot.rgb!=capture.frame.rgb&&snapshot.rgb[0]==0x5a);
    capture_frame_clear(&snapshot);
    assert(capture_snapshot(&capture,&snapshot));
    assert(snapshot.rgb!=capture.frame.rgb&&snapshot.rgb[0]==0x5a);
    capture_frame_clear(&snapshot);
    admitted=true;
    CaptureFrame stale=current;stale.sequence=0;
    assert(!capture_commit(&capture,&stale,admission,&admitted));
    stale=current;stale.timestamp=11.0;
    assert(!capture_commit(&capture,&stale,admission,&admitted));
    assert(!capture.committed);
    assert(capture_commit(&capture,&current,admission,&admitted));
    /* Publication pins geometry. Runtime changes must still invalidate. */
    negotiate(&capture,1920,1080);
    assert(!capture.error);
    negotiate(&capture,1280,720);
    assert(capture.error&&!strcmp(capture.error,"PIPEWIRE_GEOMETRY_CHANGED"));
    capture_frame_clear(&capture.frame);
    pw_thread_loop_destroy(capture.loop);
    return 0;
}
