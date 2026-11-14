import json
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from crossgate import audio
from crossgate.auth import hash_password, make_token, read_token, verify_password
from crossgate.config import Config
from crossgate.server import create_app

PWD = hash_password("motdepasse1")


def make_config(tmp_path, radios=None):
    return Config.model_validate(
        {
            "server": {"secret": "secret-de-test-assez-long", "data_dir": str(tmp_path)},
            "audio": {"hang_ms": 300, "tot_s": 30, "record": True},
            "channels": [{"id": "hf", "name": "HF Barrett"}, {"id": "vhf", "name": "VHF DMR"}, {"id": "lte", "name": "Équipes 4G"}],
            "users": [
                {"username": "pc", "display_name": "Poste de commandement", "password": PWD, "role": "admin", "priority": 10},
                {"username": "alpha", "display_name": "Alpha 1", "password": PWD, "default_channel": "lte"},
                {"username": "bravo", "display_name": "Bravo 2", "password": PWD, "channels": ["lte", "vhf"], "default_channel": "lte"},
            ],
            "radios": radios or [],
        }
    )


def token(client, user):
    r = client.post("/api/login", json={"username": user, "password": "motdepasse1"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def recv_until(ws, predicate, limit=50):
    for _ in range(limit):
        m = ws.receive()
        if m.get("bytes") is not None:
            if predicate(m["bytes"]):
                return m["bytes"]
            continue
        data = json.loads(m["text"])
        if predicate(data):
            return data
    raise AssertionError("message attendu non reçu")


def test_password_and_tokens():
    h = hash_password("abc12345")
    assert verify_password("abc12345", h) and not verify_password("faux", h)
    t = make_token("cle-secrete-de-test", "alpha", 1)
    assert read_token("cle-secrete-de-test", t) == "alpha"
    assert read_token("autre-cle-secrete!!", t) is None
    assert read_token("cle-secrete-de-test", t[:-2] + "xx") is None
    assert read_token("cle-secrete-de-test", make_token("cle-secrete-de-test", "a", -1)) is None


def test_config_rejects_bad_references(tmp_path):
    with pytest.raises(ValueError):
        make_config(tmp_path, radios=[{"type": "simulateur", "id": "s", "name": "S", "channel": "inconnu"}])


def test_login_and_permissions(tmp_path):
    with TestClient(create_app(make_config(tmp_path))) as c:
        assert c.post("/api/login", json={"username": "alpha", "password": "faux"}).status_code == 401
        t_alpha, t_pc = token(c, "alpha"), token(c, "pc")
        me = c.get("/api/me", headers={"Authorization": f"Bearer {t_alpha}"}).json()
        assert me["user"]["display_name"] == "Alpha 1" and len(me["channels"]) == 3
        assert c.get("/api/state", headers={"Authorization": f"Bearer {t_alpha}"}).status_code == 403
        assert c.get("/api/state", headers={"Authorization": f"Bearer {t_pc}"}).status_code == 200
        assert c.get("/api/me").status_code == 401
        assert c.get("/").status_code == 200 and "CrossGate" in c.get("/").text
        assert c.get("/console").status_code == 200
        assert c.get("/audio-worklet.js").status_code == 200


def test_login_rate_limit(tmp_path):
    with TestClient(create_app(make_config(tmp_path))) as c:
        for _ in range(5):
            c.post("/api/login", json={"username": "alpha", "password": "faux"})
        r = c.post("/api/login", json={"username": "alpha", "password": "motdepasse1"})
        assert r.status_code == 429


def test_ptt_between_two_phones_with_recording(tmp_path):
    with TestClient(create_app(make_config(tmp_path))) as c:
        ta, tb = token(c, "alpha"), token(c, "bravo")
        with c.websocket_connect(f"/ws?token={ta}") as a, c.websocket_connect(f"/ws?token={tb}") as b:
            assert recv_until(a, lambda m: isinstance(m, dict) and m["t"] == "welcome")["channel"] == "lte"
            recv_until(b, lambda m: isinstance(m, dict) and m["t"] == "welcome")

            # Audio sans alternat : ignoré
            a.send_bytes(audio.to_bytes(audio.tone(1000, 1)[0]))

            a.send_text(json.dumps({"t": "ptt", "on": True}))
            assert recv_until(a, lambda m: isinstance(m, dict) and m["t"] == "floor")["state"] == "granted"
            rx = recv_until(b, lambda m: isinstance(m, dict) and m["t"] == "rx")
            assert rx["talker"] == "Alpha 1"

            # Bravo tente de parler pendant qu'Alpha émet : refusé
            b.send_text(json.dumps({"t": "ptt", "on": True}))
            denied = recv_until(b, lambda m: isinstance(m, dict) and m["t"] == "floor")
            assert denied == {"t": "floor", "state": "denied", "holder": "Alpha 1"}

            frames = audio.tone(800, 10)
            for f in frames:
                a.send_bytes(audio.to_bytes(f))
            got = [recv_until(b, lambda m: isinstance(m, bytes)) for _ in frames]
            assert np.array_equal(audio.from_bytes(got[0]), frames[0])

            a.send_text(json.dumps({"t": "ptt", "on": False}))
            recv_until(b, lambda m: isinstance(m, dict) and m["t"] == "rx_end")

        # Les deux sont partis : l'enregistrement de 0,2 s existe
        tp = token(c, "pc")
        recs = c.get("/api/recordings", headers={"Authorization": f"Bearer {tp}"}).json()
        assert len(recs) == 1 and abs(recs[0]["seconds"] - 0.2) < 0.03
        wav = c.get(f"/api/recordings/file?path={recs[0]['path']}&token={tp}")
        assert wav.status_code == 200 and wav.content[:4] == b"RIFF"
        assert c.get(f"/api/recordings/file?path={recs[0]['path']}&token={ta}").status_code == 403
        assert c.get(f"/api/recordings/file?path=../../x.wav&token={tp}").status_code == 404


def test_channel_permissions_and_join(tmp_path):
    with TestClient(create_app(make_config(tmp_path))) as c:
        tb = token(c, "bravo")
        with c.websocket_connect(f"/ws?token={tb}") as b:
            w = recv_until(b, lambda m: isinstance(m, dict) and m["t"] == "welcome")
            assert [x["id"] for x in w["channels"]] == ["lte", "vhf"]
            b.send_text(json.dumps({"t": "join", "channel": "hf"}))  # non autorisé : ignoré
            b.send_text(json.dumps({"t": "join", "channel": "vhf"}))
            assert recv_until(b, lambda m: isinstance(m, dict) and m["t"] == "moved")["channel"] == "vhf"


def test_console_patch_bridges_hf_and_phones(tmp_path):
    radios = [{"type": "simulateur", "id": "sim-hf", "name": "Barrett simulée", "channel": "hf", "mode": "echo"}]
    with TestClient(create_app(make_config(tmp_path, radios))) as c:
        tp, ta = token(c, "pc"), token(c, "alpha")
        h = {"Authorization": f"Bearer {tp}"}
        with c.websocket_connect(f"/ws?token={tp}&mode=console") as con, c.websocket_connect(f"/ws?token={ta}") as a:
            snap = recv_until(con, lambda m: isinstance(m, dict) and m["t"] == "systeme")
            assert any(e["name"] == "Barrett simulée" for e in snap["endpoints"])

            r = c.post("/api/patches", json={"channels": ["hf", "lte"]}, headers=h)
            assert r.status_code == 200
            assert c.post("/api/patches", json={"channels": ["hf"]}, headers=h).status_code == 400

            # Alpha (4G) parle : la radio HF simulée l'entend puis répète (écho) vers Alpha
            recv_until(a, lambda m: isinstance(m, dict) and m["t"] == "welcome")
            a.send_text(json.dumps({"t": "ptt", "on": True}))
            recv_until(a, lambda m: isinstance(m, dict) and m["t"] == "floor" and m["state"] == "granted")
            for f in audio.tone(600, 5):
                a.send_bytes(audio.to_bytes(f))
            a.send_text(json.dumps({"t": "ptt", "on": False}))
            rx = recv_until(a, lambda m: isinstance(m, dict) and m["t"] == "rx", limit=200)
            assert rx["talker"] == "Barrett simulée"
            echoed = recv_until(a, lambda m: isinstance(m, bytes), limit=200)
            assert len(echoed) == audio.FRAME_BYTES

            # Le poste de commandement voit l'événement et la position GPS
            a.send_text(json.dumps({"t": "gps", "lat": 5.35, "lon": -4.01, "acc": 12}))
            state = recv_until(con, lambda m: isinstance(m, dict) and m["t"] == "systeme" and m["positions"], limit=400)
            assert state["positions"][0]["name"] == "Alpha 1"

            pid = r.json()["id"]
            assert c.delete(f"/api/patches/{pid}", headers=h).status_code == 200
            assert c.get("/api/state", headers=h).json()["patches"] == []


def test_priority_preemption_over_websocket(tmp_path):
    with TestClient(create_app(make_config(tmp_path))) as c:
        ta, tp = token(c, "alpha"), token(c, "pc")
        with c.websocket_connect(f"/ws?token={ta}") as a, c.websocket_connect(f"/ws?token={tp}") as pc:
            recv_until(a, lambda m: isinstance(m, dict) and m["t"] == "welcome")
            w = recv_until(pc, lambda m: isinstance(m, dict) and m["t"] == "welcome")
            pc.send_text(json.dumps({"t": "join", "channel": "lte"}))
            recv_until(pc, lambda m: isinstance(m, dict) and m["t"] == "moved")
            a.send_text(json.dumps({"t": "ptt", "on": True}))
            recv_until(a, lambda m: isinstance(m, dict) and m["t"] == "floor" and m["state"] == "granted")
            pc.send_text(json.dumps({"t": "ptt", "on": True}))
            assert recv_until(pc, lambda m: isinstance(m, dict) and m["t"] == "floor")["state"] == "granted"
            pre = recv_until(a, lambda m: isinstance(m, dict) and m["t"] == "floor" and m["state"] != "granted")
            assert pre["state"] == "preempted" and pre["by"] == "Poste de commandement"


def test_bad_token_rejected(tmp_path):
    from starlette.websockets import WebSocketDisconnect

    with TestClient(create_app(make_config(tmp_path))) as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws?token=faux") as ws:
                ws.receive_text()
