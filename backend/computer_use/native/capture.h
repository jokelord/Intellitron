#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
typedef struct Capture Capture;
typedef struct {uint8_t *rgb;size_t bytes;uint32_t width,height;uint64_t sequence,geometry;double timestamp;} CaptureFrame;
typedef struct {uint64_t process_callbacks,rejected_buffers;} CaptureProgress;
bool capture_copy_rgb(uint8_t *dst,size_t capacity,const uint8_t *src,size_t maxsize,size_t offset,size_t size,int32_t stride,uint32_t width,uint32_t height,bool bgr);
Capture *capture_open(int fd,uint32_t node,uint64_t serial,size_t max_bytes);
void capture_close(Capture *capture);
bool capture_snapshot(Capture *capture,CaptureFrame *frame);
bool capture_snapshot_newer(Capture *capture,uint64_t last_sequence,CaptureFrame *frame);
CaptureProgress capture_progress(Capture *capture);
const char *capture_freeze_timeout_status(Capture *capture,CaptureProgress start,uint64_t last_sequence);
bool capture_commit(Capture *capture,const CaptureFrame *frame,
                    bool (*commit_binding)(void *),void *binding);
void capture_frame_clear(CaptureFrame *frame);
const char *capture_error(Capture *capture);
const char *capture_startup_status(Capture *capture);
bool capture_freeze(void);

typedef struct {int64_t pts;uint64_t sequence;} CaptureClock;
bool capture_accept_timestamp(CaptureClock *clock,int64_t pts,uint64_t sequence,int64_t opened_ns,int64_t now_ns,double *timestamp);
