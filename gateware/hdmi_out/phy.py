from migen.fhdl.std import *
from migen.genlib.fifo import AsyncFIFO
from migen.genlib.cdc import MultiReg
from migen.bank.description import *
from migen.flow.actor import *

from gateware.hdmi_out.format import bpc_phy, phy_layout
from gateware.hdmi_out import hdmi

from gateware.csc.ycbcr2rgb import YCbCr2RGB
from gateware.csc.ycbcr422to444 import YCbCr422to444
from gateware.csc.ymodulator import YModulator
from gateware.csc.rgb2rgb16f import RGB2RGB16f
from gateware.csc.rgb16f2rgb import RGB16f2RGB
from gateware.float_arithmetic.floatmult import FloatMultRGB
from gateware.float_arithmetic.floatadd import FloatAddRGB

class _FIFO(Module):
    def __init__(self, pack_factor):
        self.phy = Sink(phy_layout(pack_factor))
        self.busy = Signal()

        self.pix_hsync = Signal()
        self.pix_vsync = Signal()
        self.pix_de = Signal()
        self.pix_y_0 = Signal(bpc_phy)
        self.pix_cb_cr_0 = Signal(bpc_phy)
        self.pix_y_1 = Signal(bpc_phy)
        self.pix_cb_cr_1 = Signal(bpc_phy)

        ###

        fifo = RenameClockDomains(AsyncFIFO(phy_layout(pack_factor), 512),
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
        for i in range(pack_factor):
            pixel0 = getattr(fifo.dout, "p"+str(i))
            pixel1 = getattr(fifo.dout, "q"+str(i))
            self.sync.pix += If(unpack_counter == i,
                self.pix_y_0.eq(pixel0.y),
                self.pix_cb_cr_0.eq(pixel0.cb_cr),
                self.pix_y_1.eq(pixel1.y),
                self.pix_cb_cr_1.eq(pixel1.cb_cr)
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
    def __init__(self, pack_factor, pads, external_clocking):
        fifo = _FIFO(pack_factor)
        self.submodules += fifo
        self.phy = fifo.phy
        self.busy = fifo.busy

        self.submodules.clocking = _Clocking(pads, external_clocking)

        de_r = Signal()
        self.sync.pix += de_r.eq(fifo.pix_de)

        chroma_upsampler0 = YCbCr422to444()
        chroma_upsampler1 = YCbCr422to444()
        self.submodules += RenameClockDomains(chroma_upsampler0, "pix")
        self.submodules += RenameClockDomains(chroma_upsampler1, "pix")

        self.comb += [
          chroma_upsampler0.sink.stb.eq(fifo.pix_de),
          chroma_upsampler0.sink.sop.eq(fifo.pix_de & ~de_r),
          chroma_upsampler0.sink.y.eq(fifo.pix_y_0),
          chroma_upsampler0.sink.cb_cr.eq(fifo.pix_cb_cr_0),

          chroma_upsampler1.sink.stb.eq(fifo.pix_de),
          chroma_upsampler1.sink.sop.eq(fifo.pix_de & ~de_r),
          chroma_upsampler1.sink.y.eq(fifo.pix_y_1),
          chroma_upsampler1.sink.cb_cr.eq(fifo.pix_cb_cr_1)
        ]

        self.mult_r = CSRStorage(16, reset=14336) # 0.5
        self.mult_g = CSRStorage(16, reset=14336)
        self.mult_b = CSRStorage(16, reset=14336)

        ycbcr2rgb0 = YCbCr2RGB()
        ycbcr2rgb1 = YCbCr2RGB()
        self.submodules += RenameClockDomains(ycbcr2rgb0, "pix")
        self.submodules += RenameClockDomains(ycbcr2rgb1, "pix")

        rgb2rgb16f0 = RGB2RGB16f()
        rgb2rgb16f1 = RGB2RGB16f()
        self.submodules += RenameClockDomains(rgb2rgb16f0, "pix")
        self.submodules += RenameClockDomains(rgb2rgb16f1, "pix")

        rgb16f2rgb0 = RGB16f2RGB()
        rgb16f2rgb1 = RGB16f2RGB()
        self.submodules += RenameClockDomains(rgb16f2rgb0, "pix")
        self.submodules += RenameClockDomains(rgb16f2rgb1, "pix")

        self.submodules.floatmult0 = FloatMultRGB()
        self.submodules.floatmult1 = FloatMultRGB()
        self.submodules += RenameClockDomains(self.floatmult0, "pix")
        self.submodules += RenameClockDomains(self.floatmult1, "pix")

        self.submodules.floatadd0 = FloatAddRGB()
        self.submodules.floatadd1 = FloatAddRGB()
        self.submodules += RenameClockDomains(self.floatadd0, "pix")
        self.submodules += RenameClockDomains(self.floatadd1, "pix")

        self.comb += [

            # Input0
            Record.connect(chroma_upsampler0.source, ycbcr2rgb0.sink),
            Record.connect(ycbcr2rgb0.source, rgb2rgb16f0.sink),

            self.floatmult0.sink.r1.eq(rgb2rgb16f0.source.rf),
            self.floatmult0.sink.g1.eq(rgb2rgb16f0.source.gf),
            self.floatmult0.sink.b1.eq(rgb2rgb16f0.source.bf),
            self.floatmult0.sink.r2.eq(self.mult_r.storage),
            self.floatmult0.sink.g2.eq(self.mult_g.storage),
            self.floatmult0.sink.b2.eq(self.mult_b.storage),

            self.floatmult0.sink.stb.eq(rgb2rgb16f0.source.stb),
            rgb2rgb16f0.source.ack.eq(self.floatmult0.sink.ack),
            self.floatmult0.sink.sop.eq(rgb2rgb16f0.source.sop),
            self.floatmult0.sink.eop.eq(rgb2rgb16f0.source.eop),

            # Input1
            Record.connect(chroma_upsampler1.source, ycbcr2rgb1.sink),
            Record.connect(ycbcr2rgb1.source, rgb2rgb16f1.sink),

            self.floatmult1.sink.r1.eq(rgb2rgb16f1.source.rf),
            self.floatmult1.sink.g1.eq(rgb2rgb16f1.source.gf),
            self.floatmult1.sink.b1.eq(rgb2rgb16f1.source.bf),
            self.floatmult1.sink.r2.eq(self.mult_r.storage),
            self.floatmult1.sink.g2.eq(self.mult_g.storage),
            self.floatmult1.sink.b2.eq(self.mult_b.storage),

            self.floatmult1.sink.stb.eq(rgb2rgb16f1.source.stb),
            rgb2rgb16f1.source.ack.eq(self.floatmult1.sink.ack),
            self.floatmult1.sink.sop.eq(rgb2rgb16f1.source.sop),
            self.floatmult1.sink.eop.eq(rgb2rgb16f1.source.eop),

            # Mult output of both inputs now connected
            self.floatadd0.sink.r1.eq(self.floatmult0.source.rf),
            self.floatadd0.sink.g1.eq(self.floatmult0.source.gf),
            self.floatadd0.sink.b1.eq(self.floatmult0.source.bf),
            self.floatadd0.sink.r2.eq(self.floatmult1.source.rf),
            self.floatadd0.sink.g2.eq(self.floatmult1.source.gf),
            self.floatadd0.sink.b2.eq(self.floatmult1.source.bf),

            self.floatadd1.sink.r1.eq(self.floatmult0.source.rf),
            self.floatadd1.sink.g1.eq(self.floatmult0.source.gf),
            self.floatadd1.sink.b1.eq(self.floatmult0.source.bf),
            self.floatadd1.sink.r2.eq(self.floatmult1.source.rf),
            self.floatadd1.sink.g2.eq(self.floatmult1.source.gf),
            self.floatadd1.sink.b2.eq(self.floatmult1.source.bf),

            self.floatadd0.sink.stb.eq(self.floatmult0.source.stb & self.floatmult1.source.stb ),
            self.floatadd0.sink.sop.eq(self.floatmult0.source.sop & self.floatmult1.source.sop ),
            self.floatadd0.sink.eop.eq(self.floatmult0.source.eop & self.floatmult1.source.eop ),

            self.floatadd1.sink.stb.eq(self.floatmult0.source.stb & self.floatmult1.source.stb ),
            self.floatadd1.sink.sop.eq(self.floatmult0.source.sop & self.floatmult1.source.sop ),
            self.floatadd1.sink.eop.eq(self.floatmult0.source.eop & self.floatmult1.source.eop ),
                
            self.floatmult0.source.ack.eq(self.floatadd0.sink.ack & self.floatadd0.sink.stb),
            self.floatmult1.source.ack.eq(self.floatadd1.sink.ack & self.floatadd1.sink.stb),

#            self.floatadd.sink.r2.eq(self.floatmult.source.rf),
#            self.floatadd.sink.g2.eq(self.floatmult.source.gf),
#            self.floatadd.sink.b2.eq(self.floatmult.source.bf),

#            self.floatadd.sink.stb.eq(self.floatmult.source.stb),
#            self.floatmult.source.ack.eq(self.floatadd.sink.ack),
#            self.floatadd.sink.sop.eq(self.floatmult.source.sop),
#            self.floatadd.sink.eop.eq(self.floatmult.source.eop),

#            Record.connect(self.floatmult.source, self.floatadd.sink1),
#            Record.connect(self.floatmult.source, self.floatadd.sink2),

#            self.floatadd.sink.r1.eq(self.floatmult.source.rf),
#            self.floatadd.sink.g1.eq(self.floatmult.source.gf),
#            self.floatadd.sink.b1.eq(self.floatmult.source.bf),

#            self.floatadd.sink.r2.eq(0),
#            self.floatadd.sink.g2.eq(0),
#            self.floatadd.sink.b2.eq(0),

#            self.floatadd.sink.stb.eq(1),
#            self.floatadd.sink.sop.eq(0),

            # Other input for floatadd setup in opsis_video.py

            Record.connect(self.floatadd0.source, rgb16f2rgb0.sink),  
            Record.connect(self.floatadd1.source, rgb16f2rgb1.sink),  
            rgb16f2rgb0.source.ack.eq(1),
            rgb16f2rgb1.source.ack.eq(1)
        ]

        # XXX need clean up
        de = fifo.pix_de
        hsync = fifo.pix_hsync
        vsync = fifo.pix_vsync
        for i in range(chroma_upsampler0.latency +
                       ycbcr2rgb0.latency +
                       rgb2rgb16f0.latency +
                       self.floatmult0.latency +
                       self.floatadd0.latency +
                       rgb16f2rgb0.latency
                       ):

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

        self.submodules.hdmi_phy0 = hdmi.PHY(self.clocking.serdesstrobe, pads)
#        self.submodules.hdmi_phy1 = hdmi.PHY(self.clocking.serdesstrobe, pads)

        self.comb += [
            self.hdmi_phy0.hsync.eq(hsync),
            self.hdmi_phy0.vsync.eq(vsync),
            self.hdmi_phy0.de.eq(de),

#            self.hdmi_phy1.hsync.eq(hsync),
#            self.hdmi_phy1.vsync.eq(vsync),
#            self.hdmi_phy1.de.eq(de),

            self.hdmi_phy0.r.eq(rgb16f2rgb0.source.r),
            self.hdmi_phy0.g.eq(rgb16f2rgb0.source.g),
            self.hdmi_phy0.b.eq(rgb16f2rgb0.source.b),
 #           self.hdmi_phy1.r.eq(rgb16f2rgb1.source.r),
 #           self.hdmi_phy1.g.eq(rgb16f2rgb1.source.g),
 #           self.hdmi_phy1.b.eq(rgb16f2rgb1.source.b)
        ]
