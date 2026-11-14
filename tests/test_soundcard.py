import asyncio

import numpy as np

from crossgate import audio
from crossgate.bus import Bus, Channel
from crossgate.config import SoundcardRadio
from crossgate.endpoints import soundcard as sc
from crossgate.endpoints.base import Endpoint
from crossgate.events import EventLog


class FakeStream:
    def __init__(self, callback, blocksize, **kw):
        self.callback, self.blocksize, self.kw = callback, blocksize, kw
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        pass


class FakeSd:
    def __init__(self):
        self.inp = self.out = None

    def InputStream(self, **kw):
        self.inp = FakeStream(**kw)
        return self.inp

    def OutputStream(self, **kw):
        self.out = FakeStream(**kw)
        return self.out


class FakeSerial:
    def __init__(self, *a, **kw):
        self.rts = False
        self.dtr = False
        self.cts = False
        self.history = []

    def __setattr__(self, k, v):
        if k == "rts" and hasattr(self, "history"):
            self.history.append(v)
        object.__setattr__(self, k, v)

    def close(self):
        pass


class Listener(Endpoint):
    def __init__(self, channel):
        super().__init__("l", "Listener", channel)
        self.got = []
        self.ended = 0

    def deliver(self, frame):
        self.got.append(frame)

    def on_rx_end(self):
        self.ended += 1


def test_radio_rx_with_vox_and_tx_with_serial_ptt(monkeypatch):
    serials = []

    def fake_serial(port):
        s = FakeSerial()
        serials.append(s)
        return s

    import serial

    monkeypatch.setattr(serial, "Serial", fake_serial)

    async def go():
        bus = Bus(EventLog(), hang_ms=200, tot_s=30)
        bus.add_channel(Channel("hf", "HF"))
        cfg = SoundcardRadio(
            type="audio", id="barrett", name="Barrett 4090", channel="hf", sample_rate=48000,
            ptt={"type": "serie", "port": "/dev/ttyUSB0", "pre_ms": 20, "tail_ms": 100},
            cos={"type": "vox", "threshold_dbfs": -40},
        )
        sd = FakeSd()
        radio, listener = sc.SoundcardEndpoint(cfg, sd_module=sd), Listener("hf")
        await bus.add_endpoint(listener)
        await bus.add_endpoint(radio)
        bus.start_clock()
        assert radio.status == "en_ligne", radio.detail
        assert sd.inp.started and sd.inp.blocksize == 960

        # Réception : 0,4 s de voix (tonalité) à 48 kHz, puis silence → fin d'émission
        t = np.arange(48000 * 4 // 10) / 48000
        voice = (np.sin(2 * np.pi * 700 * t) * 12000).astype(np.int16)
        for i in range(0, len(voice), 960):
            sd.inp.callback(voice[i:i + 960].reshape(-1, 1), 960, None, None)
        await asyncio.sleep(0.05)
        assert len(listener.got) >= 18
        assert abs(audio.level_dbfs(listener.got[10]) - audio.level_dbfs(voice[:160])) < 2
        silence = np.zeros((960, 1), dtype=np.int16)
        for _ in range(int(0.2 * 50) + 5):
            sd.inp.callback(silence, 960, None, None)
        await asyncio.sleep(0.05)
        assert listener.ended == 1

        # Émission : un autre point parle sur le canal → alternat, audio en sortie, puis relâchement
        ptt_serial = serials[0]
        bus.request_floor(listener)
        for f in audio.tone(1000, 10):
            bus.push_audio(listener, f)
        await asyncio.sleep(0.08)
        assert ptt_serial.rts is True and radio.transmitting
        out = np.zeros((960, 1), dtype=np.int16)
        sd.out.callback(out, 960, None, None)
        assert audio.level_dbfs(out[:, 0]) > -20
        # Pendant l'émission, ce qui revient de la radio est ignoré (anti-boucle)
        before = len(listener.got)
        sd.inp.callback(voice[:960].reshape(-1, 1), 960, None, None)
        await asyncio.sleep(0.02)
        assert len(listener.got) == before
        bus.release_floor(listener)
        for _ in range(20):
            sd.out.callback(out, 960, None, None)
        await asyncio.sleep(0.3)
        assert ptt_serial.rts is False and not radio.transmitting
        assert ptt_serial.history[-2:] == [True, False]
        await bus.shutdown()

    asyncio.run(go())


def test_radio_without_audio_device_reports_error():
    class BrokenSd:
        def InputStream(self, **kw):
            raise OSError("aucune carte son")

    async def go():
        bus = Bus(EventLog())
        bus.add_channel(Channel("hf", "HF"))
        cfg = SoundcardRadio(type="audio", id="r", name="Radio", channel="hf", ptt={"type": "aucun"})
        radio = sc.SoundcardEndpoint(cfg, sd_module=BrokenSd())
        await bus.add_endpoint(radio)
        assert radio.status == "erreur" and "aucune carte son" in radio.detail
        await bus.shutdown()

    asyncio.run(go())


def test_cm108_report_format(tmp_path):
    dev = tmp_path / "hidraw0"
    dev.write_bytes(b"")
    ptt = sc.Cm108Ptt(str(dev), gpio=3)
    ptt.set(True)
    ptt.close()
    data = dev.read_bytes()
    assert data[:5] == bytes([0, 0, 4, 0, 0])  # repos à l'ouverture
    assert data[5:10] == bytes([0, 0, 4, 4, 0])  # alternat
    assert data[-5:] == bytes([0, 0, 4, 0, 0])  # relâché à la fermeture
