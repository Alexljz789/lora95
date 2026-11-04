import spidev, lgpio, time
h=lgpio.gpiochip_open(0); lgpio.gpio_claim_input(h,24); lgpio.gpio_claim_output(h,25,1)
spi=spidev.SpiDev(); spi.open(1,0); spi.max_speed_hz=4_000_000; spi.mode=0
def busy(): return lgpio.gpio_read(h,24)
def wb(t=1):
    s=time.time()
    while busy():
        if time.time()-s>t: raise TimeoutError
        time.sleep(0.0002)
def x(d):
    wb(); r=spi.xfer2(list(d)); wb(); return bytes(r)
lgpio.gpio_write(h,25,0); time.sleep(0.002); lgpio.gpio_write(h,25,1); time.sleep(0.02); wb()
print("status C0 x2:", x([0xC0,0xC0]).hex())
print("status C0 x4:", x([0xC0,0,0,0]).hex())
print("standby:", x([0x80,0]).hex())
time.sleep(0.01)
print("clear irq:", x([0x02,0xFF,0xFF]).hex())
time.sleep(0.01)
for n in (3,4,5,6,8):
    print("irq len",n, x([0x12]+[0]*(n-1)).hex())
print("deverr 5:", x([0x17,0,0,0,0]).hex())
print("pkttype 4:", x([0x11,0,0,0]).hex())
print("rxbuf 5:", x([0x13,0,0,0,0]).hex())
print("pktstatus 6:", x([0x14,0,0,0,0,0]).hex())
# set packet type lora + rx
print("settype", x([0x8A,0x01]).hex())
print("setfreq", x([0x86,0x38,0xAE,0x00,0x00]).hex())
print("mod", x([0x8B,11,0x05,1,1,0,0,0,0]).hex())
print("pkt", x([0x8C,0,16,0,255,1,0,0,0,0]).hex())
print("irqmask", x([0x08,0x03,0xFF,0x03,0xFF,0,0,0,0]).hex())
print("clear", x([0x02,0xFF,0xFF]).hex())
print("irq before rx:", x([0x12,0,0,0,0]).hex())
print("rx", x([0x82,0xFF,0xFF,0xFF]).hex())
for i in range(8):
    time.sleep(0.1)
    print("t",i,"st",x([0xC0]).hex(),"irq",x([0x12,0,0,0,0]).hex(),"derr",x([0x17,0,0,0,0]).hex())
print("standby", x([0x80,0]).hex())
