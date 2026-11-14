import asyncio
import math
import time

import numpy as np
import pytest

from crossgate import audio
from crossgate.bus import Bus, Channel, FloorDenied
from crossgate.endpoints.base import Endpoint
from crossgate.events import EventLog


# ---------------------------------------------------------------- audio


def _tone(freq, rate, seconds=0.5, amp=0.5):
    t = np.arange(int(rate * seconds)) / rate
    return (np.sin(2 * np.pi * freq * t) * amp * 32767).astype(np.int16)


def _dominant_freq(samples, rate):
    spec = np.abs(np.fft.rfft(samples.astype(float) * np.hanning(len(samples))))
    return np.fft.rfftfreq(len(samples), 1 / rate)[np.argmax(spec)]


def test_resampler_down_keeps_voice_band():
    r = audio.Resampler(48000, 8000)
    src = _tone(1000, 48000)
    out = np.concatenate([r.process(src[i:i + 960]) for i in range(0, len(src), 960)])  # blocs de 20 ms
    assert len(out) == len(src) // 6
    assert abs(_dominant_freq(out[200:], 8000) - 1000) < 20
    rms_in = np.sqrt(np.mean(src.astype(float) ** 2))
    rms_out = np.sqrt(np.mean(out[200:].astype(float) ** 2))
    assert 0.85 < rms_out / rms_in < 1.15


def test_resampler_down_rejects_aliasing():
    r = audio.Resampler(48000, 8000)
    out = r.process(_tone(10000, 48000))  # au-delà de 4 kHz : doit être éliminé
    assert audio.level_dbfs(out[100:]) < -35


def test_resampler_up():
    r = audio.Resampler(8000, 48000)
    src = _tone(800, 8000)
    out = np.concatenate([r.process(src[i:i + 160]) for i in range(0, len(src), 160)])
    assert len(out) == len(src) * 6
    assert abs(_dominant_freq(out[1000:], 48000) - 800) < 20


def test_resampler_rejects_non_integer_ratio():
    with pytest.raises(ValueError):
        audio.Resampler(44100, 8000)


def test_framer_and_bytes():
    f = audio.Framer()
    frames = f.push(np.arange(400, dtype=np.int16))
    assert len(frames) == 2 and len(f.buf) == 80
    b = audio.to_bytes(frames[0])
    assert len(b) == audio.FRAME_BYTES
    assert np.array_equal(audio.from_bytes(b), frames[0])
    assert audio.level_dbfs(audio.silence()) == -math.inf


# ---------------------------------------------------------------- bus


class Probe(Endpoint):
    def __init__(self, id, channel, priority=0):
        super().__init__(id, id.upper(), channel, priority)
        self.got = []
        self.floor_events = []
        self.rx = []

    def deliver(self, frame):
        self.got.append(frame)

    def on_floor(self, state, info):
        self.floor_events.append(state)

    def on_rx_start(self, talker, channel):
        self.rx.append(("start", talker))

    def on_rx_end(self):
        self.rx.append(("end",))


@pytest.fixture
def bus():
    b = Bus(EventLog(), hang_ms=200, tot_s=5)
    for c in ("hf", "vhf", "lte"):
        b.add_channel(Channel(c, c.upper()))
    return b


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def add(bus, *eps):
    async def go():
        for e in eps:
            await bus.add_endpoint(e)
    run(go())


def test_audio_stays_in_channel_until_patched(bus):
    a, b, c = Probe("a", "hf"), Probe("b", "hf"), Probe("c", "vhf")
    add(bus, a, b, c)
    frame = audio.tone(1000, 1)[0]
    assert bus.push_audio(a, frame, implicit=True)
    assert len(b.got) == 1 and len(c.got) == 0 and len(a.got) == 0
    bus.release_floor(a)
    bus.patch(["hf", "vhf"], by="test")
    assert bus.push_audio(a, frame, implicit=True)
    assert len(b.got) == 2 and len(c.got) == 1
    assert ("start", "A") in c.rx


def test_single_talker_and_denial(bus):
    a, b = Probe("a", "hf"), Probe("b", "hf")
    add(bus, a, b)
    bus.request_floor(a)
    with pytest.raises(FloorDenied) as e:
        bus.request_floor(b)
    assert e.value.holder is a
    assert not bus.push_audio(b, audio.silence(), implicit=True)  # radio occupée : rejet
    assert not bus.push_audio(b, audio.silence())  # sans parole : rejet
    bus.release_floor(a)
    bus.request_floor(b)
    assert "granted" in b.floor_events


def test_priority_preemption(bus):
    soldier, command = Probe("s", "hf", priority=0), Probe("pc", "hf", priority=10)
    add(bus, soldier, command)
    bus.request_floor(soldier)
    bus.request_floor(command)
    assert "preempted" in soldier.floor_events
    assert bus.floor_of(command).talker == "pc"


def test_hang_time_releases_implicit_talker(bus):
    radio, phone = Probe("radio", "hf"), Probe("tel", "hf")
    add(bus, radio, phone)
    bus.push_audio(radio, audio.silence(), implicit=True)
    bus.tick()
    assert bus.floor_of(radio) is not None
    bus.floors[frozenset(["hf"])].last_audio = time.monotonic() - 1
    bus.tick()
    assert bus.floor_of(radio) is None
    assert ("end",) in phone.rx


def test_timeout_cuts_long_transmission(bus):
    a = Probe("a", "hf")
    add(bus, a)
    bus.request_floor(a)
    bus.floors[frozenset(["hf"])].started = time.monotonic() - 10
    bus.tick()
    assert "timeout" in a.floor_events


def test_patch_merge_and_unpatch(bus):
    p1 = bus.patch(["hf", "vhf"])
    p2 = bus.patch(["vhf", "lte"])
    assert p2.channels == frozenset({"hf", "vhf", "lte"}) and p1.id not in bus.patches
    bus.unpatch(p2.id)
    assert bus.group_of("hf") == frozenset({"hf"})
    with pytest.raises(ValueError):
        bus.patch(["hf"])
    with pytest.raises(ValueError):
        bus.patch(["hf", "inconnu"])


def test_move_releases_floor(bus):
    a, b = Probe("a", "hf"), Probe("b", "hf")
    add(bus, a, b)
    bus.request_floor(a)
    bus.move("a", "vhf")
    assert bus.floor_of(b) is None
    assert a.channel == "vhf"


def test_recorder_writes_wav(tmp_path, bus):
    from crossgate.recorder import Recorder

    rec = Recorder(tmp_path)
    bus.tx_listeners.append(rec)
    a, b = Probe("a", "hf"), Probe("b", "hf")
    add(bus, a, b)
    for f in audio.tone(440, 50):
        bus.push_audio(a, f, implicit=True)
    bus.release_floor(a)
    files = rec.list()
    assert len(files) == 1 and abs(files[0]["seconds"] - 1.0) < 0.05
    assert rec.resolve(files[0]["path"]) is not None
    assert rec.resolve("../../etc/passwd") is None


def test_radio_profiles_fill_defaults_without_overriding():
    from crossgate.config import SoundcardRadio

    r = SoundcardRadio(type="audio", id="ev", name="EV751", channel="hf", profil="huawei-elte-vehicule", ptt={"type": "aucun"})
    assert (r.ptt.pre_ms, r.ptt.tail_ms, r.cos.threshold_dbfs) == (400, 300, -40)
    r2 = SoundcardRadio(type="audio", id="b", name="Barrett", channel="hf", profil="hf", ptt={"type": "aucun", "pre_ms": 500})
    assert r2.ptt.pre_ms == 500 and r2.ptt.tail_ms == 400
