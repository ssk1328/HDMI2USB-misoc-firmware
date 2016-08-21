#include <generated/csr.h>
#include <generated/mem.h>
#include <hw/flags.h>
#include <system.h>
#include <time.h>
#include <stdbool.h>

#include "processor.h"
#include "hdmi_in0.h"
#include "hdmi_in1.h"
#include "pattern.h"
#include "mix.h"

static const unsigned int mult_bar[20] = {
	0 	  ,
	10854 ,
	11878 ,
	12493 ,
	12902 ,
	13312 ,
	13517 ,
	13722 ,
	13926 ,
	14131 ,
	14336 ,
	14438 ,
	14541 ,
	14643 ,
	14746 ,
	14848 ,
	14950 ,
	15053 ,
	15155 ,
	15258 
	
};

#define FILL_RATE 20 			// In Hertz, double the standard frame rate

static int fade = 0;

void set_fade(int fade_val)
{
	fade = fade_val;
	printf("Fade is set %d \n" , fade);

}

void mult_service( void )
{
	static int last_event;
	static int counter;

//	if (mix_status) {
		if(elapsed(&last_event, identifier_frequency_read()/FILL_RATE)) {

			if (fade==UP) {
				counter = counter+1;
				if(counter >= (FILL_RATE-1)) {
					counter = 19;
				}
			}

			else if (fade==DOWN) {
				counter = counter-1;
				if(counter <= 0) {
					counter = 0;
				}				
			}
			else if (fade==OFF) {
				fade=fade;
			}
			
		}
//	}
	hdmi_out0_driver_mix_mult_factor_source0_write(mult_bar[counter]);
	hdmi_out0_driver_mix_mult_factor_source1_write(mult_bar[20-1-counter]);

}