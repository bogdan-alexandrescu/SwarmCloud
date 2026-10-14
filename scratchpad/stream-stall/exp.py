import asyncio, sys, time, struct
sys.path.insert(0, "tests/unit/worker")
from pathlib import Path
import tempfile
import test_live_stream_smooth as t
from agent_worker.livegate import rfb
from agent_worker.livegate.capture import Frame

class FB:
    def __init__(self, w, h):
        self.w, self.h, self.n = w, h, 0
    def grab(self):
        self.n += 1
        f = Frame.blank(self.w, self.h, (255, 255, 255))
        # a moving block plus a counter band
        x = (self.n * 13) % (self.w - 40)
        f.fill(x, 100, x + 40, 140, (200, 0, 0))
        return f
    def close(self): pass

class SlowInj(t.Injector):
    def __init__(self, delay): super().__init__(); self.delay = delay; self.ptr = 0
    async def pointer_event(self, b, x, y):
        await asyncio.sleep(self.delay); self.ptr += 1

async def main(delay):
    tmp = Path(tempfile.mkdtemp())
    gate = t._gate(tmp)
    gate.framebuffer = FB(1280, 800)
    gate.mask_state = t._clean()
    inj = SlowInj(delay)
    gate.control = rfb.ControlState(inj)
    hub = gate.hub
    (sr, sw), (vr, vw) = await t._pair()
    sess = rfb.RfbSession("v1", sr, sw, hub, gate.control, inj)
    gate.viewers = 1; gate._watching.set()
    st = asyncio.create_task(sess.serve())
    cap = asyncio.create_task(gate._capture())
    v = t.Viewer(vr, vw)
    await v.handshake()
    await gate.control.grant("v1")
    v.request(False); await v.flush()
    t0 = time.monotonic(); n = 0
    async def inputs():
        i = 0
        while True:
            vw.write(struct.pack(">BBHH", 5, 0, 10 + i % 300, 10)); i += 1
            if i % 5 == 0: v.key(0x61, True); v.key(0x61, False)
            await vw.drain(); await asyncio.sleep(0.02)
    it = asyncio.create_task(inputs())
    try:
        while time.monotonic() - t0 < 3:
            await v.read_update(10); n += 1
            v.request(True); await v.flush()
    except asyncio.TimeoutError:
        print("timeout")
    el = time.monotonic() - t0
    c=sess.counts; print(f"p50={c.latency(50)} p95={c.latency(95)} dropped={c.dropped} stalls={c.stalls}"); print(f"delay={delay} updates={n} in {el:.1f}s -> {n/el:.1f}/s keys={sess.counts.key_events} ptr={sess.counts.pointer_events} grabs={gate.framebuffer.n}")
    it.cancel(); cap.cancel(); st.cancel()

for d in (0.0, 0.5):
    asyncio.run(main(d))
