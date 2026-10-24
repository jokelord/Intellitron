#pragma once
#ifdef HAVE_JSON_GLIB
#include <json-glib/json-glib.h>
typedef struct Live Live;
Live *live_new(void);
void live_free(Live *live);
void live_close(Live *live);
void live_pump(Live *live);
/* Returns payload owned by caller; one sealed descriptor only for frame. */
JsonNode *live_command(Live *live,const char *command,JsonObject *payload,const char **kind,int *fd);
#endif
