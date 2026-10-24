#include <assert.h>
#include <stdint.h>
#include <string.h>
#include "capture.h"
/* Bounds, channel conversion and padded rows must be handled by the same
 * function used by the real PipeWire callback. */
int main(void) {
    uint8_t source[]={99,99,3,2,1,0,6,5,4,0,88,88};
    uint8_t rgb[6]={0};
    assert(capture_copy_rgb(rgb, sizeof(rgb), source, sizeof(source), 2, 8, 8, 2, 1, true));
    assert(memcmp(rgb,(uint8_t[]){1,2,3,4,5,6},6)==0);
    assert(!capture_copy_rgb(rgb,sizeof(rgb),source,sizeof(source),2,7,8,2,1,true));
    assert(!capture_copy_rgb(rgb,sizeof(rgb),source,sizeof(source),8,8,8,2,1,true));
    assert(!capture_copy_rgb(rgb,sizeof(rgb),source,sizeof(source),2,8,-8,2,1,true));
    assert(!capture_copy_rgb(rgb,sizeof(rgb),source,sizeof(source),2,8,8,0,1,true));
    CaptureClock clock={0};
    double timestamp=0;
    assert(capture_accept_timestamp(&clock,12000000000LL,0,10000000000LL,15000000000LL,&timestamp));
    assert(timestamp==12.0);
    assert(!capture_accept_timestamp(&clock,12000000000LL,0,10000000000LL,16000000000LL,&timestamp));
    assert(!capture_accept_timestamp(&clock,11000000000LL,0,10000000000LL,16000000000LL,&timestamp));
    assert(!capture_accept_timestamp(&clock,17000000000LL,0,10000000000LL,16000000000LL,&timestamp));
    assert(!capture_accept_timestamp(&clock,-1,0,10000000000LL,16000000000LL,&timestamp));
    assert(capture_accept_timestamp(&clock,13000000000LL,9,10000000000LL,16000000000LL,&timestamp));
    assert(!capture_accept_timestamp(&clock,14000000000LL,9,10000000000LL,16000000000LL,&timestamp));
    assert(capture_accept_timestamp(&clock,14000000000LL,10,10000000000LL,16000000000LL,&timestamp));
    CaptureClock fresh={0};
    assert(!capture_accept_timestamp(&fresh,9000000000LL,1,10000000000LL,16000000000LL,&timestamp));
    return 0;
}
