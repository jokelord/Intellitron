#include <assert.h>
#include "portal.h"
#include "capture.h"
#include "ei_input.h"
#include "x11.h"
int main(void) {
  PortalState portal = {0};
  InputState input = {0};
  assert(!portal_start(&portal));
  assert(!portal.ready);
  assert(!capture_freeze());
  assert(!input_can_emit(&input));
  input.resumed = input.emulating = input.absolute = input.button = input.scroll = 1;
  assert(!input_can_emit(&input)); /* Mapping/connection evidence still absent. */
  assert(!x11_ready());
  bool held=false;
  assert(!input_button_transition(&held,false));
  assert(input_button_transition(&held,true) && held);
  assert(!input_button_transition(&held,true));
  assert(input_button_transition(&held,false) && !held);
  assert(!input_button_transition(&held,false));
  portal_close(&portal);
  return 0;
}
