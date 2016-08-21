#ifndef __MIX_H
#define __MIX_H

#include <stdbool.h>

static const unsigned int mult_bar[20];

enum {
	DOWN=0,
	UP,
	OFF
};

void mult_service(void);

#endif