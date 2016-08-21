from migen.fhdl.std import *
from migen.genlib.fifo import AsyncFIFO
from migen.genlib.cdc import MultiReg
from migen.bank.description import *
from migen.flow.actor import *

from gateware.hdmi_out.format import bpc_phy, phy_layout_c
from gateware.hdmi_out import hdmi

from gateware.csc.ycbcr2rgb import YCbCr2RGB
from gateware.csc.ycbcr422to444 import YCbCr422to444
from gateware.csc.ymodulator import YModulator
from gateware.csc.rgb2rgb16f import RGB2RGB16f
from gateware.csc.rgb16f2rgb import RGB16f2RGB
from gateware.float_arithmetic.floatmult import FloatMultRGB
from gateware.float_arithmetic.floatadd import FloatAddRGB


class _FIFO(Module):
    def __init__(self, pack_factor, ndmas):
        self.phy = Sink(phy_layout_c(pack_factor, ndmas))
        self.busy = Signal()

        self.pix_hsync = Signal()
        self.pix_vsync = Signal()
        self.pix_de = Signal()

        ###

        fifo = RenameClockDomains(AsyncFIFO(phy_layout_c(pack_factor, ndmas), 512),
            {"write": "sys", "read": "pix"})
        self.submodules += fifo
        self.comb += [
            self.phy.ack.eq(fifo.writable),
            fifo.we.eq(self.phy.stb),
            fifo.din.eq(self.phy.payload),
            self.busy.eq(0)
        ]

        if pack_factor == 1:
            unpack_counter = Signal(reset=0)
        else:
            unpack_counter = Signal(max=pack_factor)
            self.sync.pix += [
                unpack_counter.eq(unpack_counter + 1),
            ]

        assert(pack_factor & (pack_factor - 1) == 0)  # only support powers of 2
        self.sync.pix += [
            self.pix_hsync.eq(fifo.dout.hsync),
            self.pix_vsync.eq(fifo.dout.vsync),
            self.pix_de.eq(fifo.dout.de)
        ]

        for j in range(ndmas):
            setattr(self,'pix_y_n'    +str(j) , Signal(bpc_phy))
            setattr(self,'pix_cb_cr_n'+str(j) , Signal(bpc_phy))

            for i in range(pack_factor):
                pixel = getattr(getattr(fifo.dout, "n"+str(j)), "p"+str(i))
                self.sync.pix += If(unpack_counter == i,
                    getattr(self, 'pix_y_n'+str(j)).eq(pixel.y),
                    getattr(self, 'pix_cb_cr_n'+str(j)).eq(pixel.cb_cr),
                )

        self.comb += fifo.re.eq(unpack_counter == (pack_factor - 1))


# This assumes a 50MHz base clock
class _Clocking(Module, AutoCSR):
    def __init__(self, pads, external_clocking):
        if external_clocking is None:
            self._cmd_data = CSRStorage(10)
            self._send_cmd_data = CSR()
            self._send_go = CSR()
            self._status = CSRStatus(4)

            self.clock_domains.cd_pix = ClockDomain(reset_less=True)
            self._pll_reset = CSRStorage()
            self._pll_adr = CSRStorage(5)
            self._pll_dat_r = CSRStatus(16)
            self._pll_dat_w = CSRStorage(16)
            self._pll_read = CSR()
            self._pll_write = CSR()
            self._pll_drdy = CSRStatus()

            self.clock_domains.cd_pix2x = ClockDomain(reset_less=True)
            self.clock_domains.cd_pix10x = ClockDomain(reset_less=True)
            self.serdesstrobe = Signal()

            ###

            # Generate 1x pixel clock
            clk_pix_unbuffered = Signal()
            pix_progdata = Signal()
            pix_progen = Signal()
            pix_progdone = Signal()
            pix_locked = Signal()
            self.specials += Instance("DCM_CLKGEN",
                                      p_CLKFXDV_DIVIDE=2, p_CLKFX_DIVIDE=4, p_CLKFX_MD_MAX=1.0, p_CLKFX_MULTIPLY=2,
                                      p_CLKIN_PERIOD=20.0, p_SPREAD_SPECTRUM="NONE", p_STARTUP_WAIT="FALSE",

                                      i_CLKIN=ClockSignal("base50"), o_CLKFX=clk_pix_unbuffered,
                                      i_PROGCLK=ClockSignal(), i_PROGDATA=pix_progdata, i_PROGEN=pix_progen,
                                      o_PROGDONE=pix_progdone, o_LOCKED=pix_locked,
                                      i_FREEZEDCM=0, i_RST=ResetSignal())

            remaining_bits = Signal(max=11)
            transmitting = Signal()
            self.comb += transmitting.eq(remaining_bits != 0)
            sr = Signal(10)
            self.sync += [
                If(self._send_cmd_data.re,
                    remaining_bits.eq(10),
                    sr.eq(self._cmd_data.storage)
                ).Elif(transmitting,
                    remaining_bits.eq(remaining_bits - 1),
                    sr.eq(sr[1:])
                )
            ]
            self.comb += [
                pix_progdata.eq(transmitting & sr[0]),
                pix_progen.eq(transmitting | self._send_go.re)
            ]

            # enforce gap between commands
            busy_counter = Signal(max=14)
            busy = Signal()
            self.comb += busy.eq(busy_counter != 0)
            self.sync += If(self._send_cmd_data.re,
                    busy_counter.eq(13)
                ).Elif(busy,
                    busy_counter.eq(busy_counter - 1)
                )

            mult_locked = Signal()
            self.comb += self._status.status.eq(Cat(busy, pix_progdone, pix_locked, mult_locked))

            # Clock multiplication and buffering
            # Route unbuffered 1x pixel clock to PLL
            # Generate 1x, 2x and 10x IO pixel clocks
            clkfbout = Signal()
            pll_locked = Signal()
            pll_clk0 = Signal()
            pll_clk1 = Signal()
            pll_clk2 = Signal()
            locked_async = Signal()
            pll_drdy = Signal()
            self.sync += If(self._pll_read.re | self._pll_write.re,
                self._pll_drdy.status.eq(0)
            ).Elif(pll_drdy,
                self._pll_drdy.status.eq(1)
            )
            self.specials += [
                Instance("PLL_ADV",
                         p_CLKFBOUT_MULT=10,
                         p_CLKOUT0_DIVIDE=1,   # pix10x
                         p_CLKOUT1_DIVIDE=5,   # pix2x
                         p_CLKOUT2_DIVIDE=10,  # pix
                         p_COMPENSATION="INTERNAL",

                         i_CLKINSEL=1,
                         i_CLKIN1=clk_pix_unbuffered,
                         o_CLKOUT0=pll_clk0, o_CLKOUT1=pll_clk1, o_CLKOUT2=pll_clk2,
                         o_CLKFBOUT=clkfbout, i_CLKFBIN=clkfbout,
                         o_LOCKED=pll_locked,
                         i_RST=~pix_locked | self._pll_reset.storage,

                         i_DADDR=self._pll_adr.storage,
                         o_DO=self._pll_dat_r.status,
                         i_DI=self._pll_dat_w.storage,
                         i_DEN=self._pll_read.re | self._pll_write.re,
                         i_DWE=self._pll_write.re,
                         o_DRDY=pll_drdy,
                         i_DCLK=ClockSignal()),
                Instance("BUFPLL", p_DIVIDE=5,
                         i_PLLIN=pll_clk0, i_GCLK=ClockSignal("pix2x"), i_LOCKED=pll_locked,
                         o_IOCLK=self.cd_pix10x.clk, o_LOCK=locked_async, o_SERDESSTROBE=self.serdesstrobe),
                Instance("BUFG", i_I=pll_clk1, o_O=self.cd_pix2x.clk),
                Instance("BUFG", name="hdmi_out_pix_bufg", i_I=pll_clk2, o_O=self.cd_pix.clk),
                MultiReg(locked_async, mult_locked, "sys")
            ]

            self.pll_clk0 = pll_clk0
            self.pll_clk1 = pll_clk1
            self.pll_clk2 = pll_clk2
            self.pll_locked = pll_locked

        else:
            self.clock_domains.cd_pix = ClockDomain(reset_less=True)
            self.specials +=  Instance("BUFG", name="hdmi_out_pix_bufg", i_I=external_clocking.pll_clk2, o_O=self.cd_pix.clk)
            self.clock_domains.cd_pix2x = ClockDomain(reset_less=True)
            self.clock_domains.cd_pix10x = ClockDomain(reset_less=True)
            self.serdesstrobe = Signal()
            self.specials += [
                Instance("BUFG", i_I=external_clocking.pll_clk1, o_O=self.cd_pix2x.clk),
                Instance("BUFPLL", p_DIVIDE=5,
                         i_PLLIN=external_clocking.pll_clk0, i_GCLK=self.cd_pix2x.clk, i_LOCKED=external_clocking.pll_locked,
                         o_IOCLK=self.cd_pix10x.clk, o_SERDESSTROBE=self.serdesstrobe),
            ]

        # Drive HDMI clock pads
        hdmi_clk_se = Signal()
        self.specials += Instance("ODDR2",
                                  p_DDR_ALIGNMENT="NONE", p_INIT=0, p_SRTYPE="SYNC",
                                  o_Q=hdmi_clk_se,
                                  i_C0=ClockSignal("pix"),
                                  i_C1=~ClockSignal("pix"),
                                  i_CE=1, i_D0=1, i_D1=0,
                                  i_R=0, i_S=0)
        self.specials += Instance("OBUFDS", i_I=hdmi_clk_se,
                                  o_O=pads.clk_p, o_OB=pads.clk_n)


class Driver(Module, AutoCSR):
    def __init__(self, pack_factor, ndmas, pads, external_clocking):
        fifo = _FIFO(pack_factor, ndmas)
        self.submodules += fifo
        self.phy = fifo.phy
        self.busy = fifo.busy

        self.submodules.clocking = _Clocking(pads, external_clocking)

        self.mix = Mixer(ndmas, fifo)
        self.submodules += self.mix

        # XXX need clean up
        de = fifo.pix_de
        hsync = fifo.pix_hsync
        vsync = fifo.pix_vsync
        for i in range(
                    self.mix.chroma_upsampler.latency +
                    self.mix.ycbcr2rgb.latency + 
                    self.mix.rgb2rgb16f.latency +
                    self.mix.floatadd.latency +
                    self.mix.floatmult.latency +
                    self.mix.rgb16f2rgb.latency):

            next_de = Signal()
            next_vsync = Signal()
            next_hsync = Signal()
            self.sync.pix += [
                next_de.eq(de),
                next_vsync.eq(vsync),
                next_hsync.eq(hsync),
            ]
            de = next_de
            vsync = next_vsync
            hsync = next_hsync

        self.submodules.hdmi_phy = hdmi.PHY(self.clocking.serdesstrobe, pads)
        self.comb += [
            self.hdmi_phy.hsync.eq(hsync),
            self.hdmi_phy.vsync.eq(vsync),
            self.hdmi_phy.de.eq(de),
            self.hdmi_phy.r.eq(self.mix.rgb16f2rgb.source.r),
            self.hdmi_phy.g.eq(self.mix.rgb16f2rgb.source.g),
            self.hdmi_phy.b.eq(self.mix.rgb16f2rgb.source.b)
        ]


class Mixer(Module, AutoCSR):
    """Mixer Module
    This is a dummy mixer module, it takes YCbCr 4:2:2 of each of the DMAs, 
    converts it to YCbCr 4:4:4 and then to RGB using appropriate modules and 
    outputs an Array class which contains RGB outputs from each these DMAs.
    Parameters
    ----------
    ndmas: int
        Number of DMA engines to be initiated, specified in target file. 
    
    fifo: _FIFO class
        Instantiation of _FIFO class in Driver Module
    Attributes
    ----------
    out_r : Array class (Array of Signal), out
        Red pixel output from each of the ndmas in an Array of size ndmas    
    out_g : Array class (Array of Signal), out
        Green pixel output from each of the ndmas in an Array of size ndmas    
    out_b : Array class (Array of Signal), out
        Blue pixel output from each of the ndmas in an Array of size ndmas    
    """
    def __init__(self, ndmas, fifo):
        
        fifo_y = Array([Signal(8) for i in range(ndmas)])
        fifo_cb_cr = Array([Signal(8) for i in range(ndmas)])

        for n in range(ndmas):
            self.comb += [
                fifo_y[n].eq(getattr(fifo,'pix_y_n'+str(n))),
                fifo_cb_cr[n].eq(getattr(fifo,'pix_cb_cr_n'+str(n)))
            ]

        self.source0 = CSRStorage(8, reset=0)
        self.source1 = CSRStorage(8, reset=1)
        self.mult_factor_source0 = CSRStorage(16, reset=14336)
        self.mult_factor_source1 = CSRStorage(16, reset=14336)
        floatmults = []

        de_r = Signal()
        self.sync.pix += de_r.eq(fifo.pix_de)

        ##############################################################
        
        for i in range(2):

            self.chroma_upsampler = YCbCr422to444()
            self.submodules += RenameClockDomains(self.chroma_upsampler, "pix")

            self.comb += [
                self.chroma_upsampler.sink.stb.eq(fifo.pix_de),
                self.chroma_upsampler.sink.sop.eq(fifo.pix_de & ~de_r),
                self.chroma_upsampler.sink.y.eq(fifo_y[getattr(getattr(self,'source'+str(i)),'storage')]),
                self.chroma_upsampler.sink.cb_cr.eq(fifo_cb_cr[getattr(getattr(self, 'source'+str(i)),'storage')]),
            ]

            self.ycbcr2rgb = YCbCr2RGB()
            self.submodules += RenameClockDomains(self.ycbcr2rgb, "pix")

            self.rgb2rgb16f = RGB2RGB16f()
            self.submodules += RenameClockDomains(self.rgb2rgb16f, "pix")

            self.comb += [
                Record.connect(self.chroma_upsampler.source, self.ycbcr2rgb.sink),
                Record.connect(self.ycbcr2rgb.source, self.rgb2rgb16f.sink),
            ]

            self.floatmult = FloatMultRGB()
            self.submodules += RenameClockDomains(self.floatmult, "pix")
            floatmults.append(self.floatmult)

            self.comb += [
                self.floatmult.sink.r1.eq(self.rgb2rgb16f.source.rf),
                self.floatmult.sink.g1.eq(self.rgb2rgb16f.source.gf),
                self.floatmult.sink.b1.eq(self.rgb2rgb16f.source.bf),
                self.floatmult.sink.r2.eq(getattr(getattr(self,'mult_factor_source'+str(i)),'storage')),
                self.floatmult.sink.g2.eq(getattr(getattr(self,'mult_factor_source'+str(i)),'storage')),
                self.floatmult.sink.b2.eq(getattr(getattr(self,'mult_factor_source'+str(i)),'storage')),
                self.floatmult.sink.stb.eq(self.rgb2rgb16f.source.stb),
                self.rgb2rgb16f.source.ack.eq(self.floatmult.sink.ack),
                self.floatmult.sink.sop.eq(self.rgb2rgb16f.source.sop),
                self.floatmult.sink.eop.eq(self.rgb2rgb16f.source.eop)
            ]

        self.floatadd = FloatAddRGB()
        self.submodules += RenameClockDomains(self.floatadd, "pix")

        self.comb += [
            self.floatadd.sink.r1.eq(floatmults[0].source.rf),
            self.floatadd.sink.g1.eq(floatmults[0].source.gf),
            self.floatadd.sink.b1.eq(floatmults[0].source.bf),
            self.floatadd.sink.r2.eq(floatmults[1].source.rf),
            self.floatadd.sink.g2.eq(floatmults[1].source.gf),
            self.floatadd.sink.b2.eq(floatmults[1].source.bf),
            self.floatadd.sink.stb.eq(floatmults[0].source.stb & floatmults[1].source.stb ),
            self.floatadd.sink.sop.eq(floatmults[0].source.sop & floatmults[1].source.sop ),
            self.floatadd.sink.eop.eq(floatmults[0].source.eop & floatmults[1].source.eop ),
            floatmults[0].source.ack.eq(self.floatadd.sink.ack & self.floatadd.sink.stb),
            floatmults[1].source.ack.eq(self.floatadd.sink.ack & self.floatadd.sink.stb)
        ]

        self.rgb16f2rgb = RGB16f2RGB()
        self.submodules += RenameClockDomains(self.rgb16f2rgb, "pix")
        
        self.comb += [
            Record.connect(self.floatadd.source, self.rgb16f2rgb.sink),
            self.rgb16f2rgb.source.ack.eq(1)
        ]
