"""Serveur CrossGate : API REST, WebSocket temps réel (smartphones et console) et fichiers web."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from crossgate import audio
from crossgate.auth import make_token, read_token, verify_password
from crossgate.bus import Bus, Channel, FloorDenied
from crossgate.config import Config, IpscRadio, SimulatorRadio, SoundcardRadio, UserConfig
from crossgate.endpoints.ipsc import IpscEndpoint
from crossgate.endpoints.simulator import SimulatorEndpoint
from crossgate.endpoints.soundcard import SoundcardEndpoint
from crossgate.endpoints.webptt import WebPttEndpoint
from crossgate.events import EventLog
from crossgate.recorder import Recorder
from crossgate.vocoder import NullVocoder, ThumbDV

log = logging.getLogger("crossgate")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
OPERATOR_ROLES = ("operateur", "admin")


class Login(BaseModel):
    username: str
    password: str


class PatchBody(BaseModel):
    channels: list[str]


class MoveBody(BaseModel):
    channel: str


class State:
    """Tout ce qui vit pendant l'exécution du serveur."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        cfg.data_path.mkdir(parents=True, exist_ok=True)
        self.events = EventLog(cfg.data_path)
        self.bus = Bus(self.events, cfg.audio.hang_ms, cfg.audio.tot_s)
        self.recorder = Recorder(cfg.data_path) if cfg.audio.record else None
        self.users = {u.username: u for u in cfg.users}
        self.positions: dict[str, dict[str, Any]] = {}
        self.consoles: set[WebPttEndpoint] = set()
        self._push_pending = False

    def user_channels(self, user: UserConfig) -> list[str]:
        return list(self.bus.channels) if user.channels == "*" else list(user.channels)

    def snapshot(self) -> dict[str, Any]:
        snap = self.bus.snapshot()
        snap["positions"] = list(self.positions.values())
        snap["recording"] = self.recorder is not None
        return snap

    # Diffusion de l'état aux consoles, regroupée (au plus 4 fois par seconde)
    def schedule_push(self) -> None:
        if self._push_pending:
            return
        self._push_pending = True
        try:
            asyncio.get_running_loop().call_later(0.25, self._push)
        except RuntimeError:
            self._push_pending = False

    def _push(self) -> None:
        self._push_pending = False
        msg = {"t": "systeme", **self.snapshot()}
        for c in list(self.consoles):
            c.send_json(msg)

    def push_event(self, event: dict[str, Any]) -> None:
        for c in list(self.consoles):
            c.send_json({"t": "event", "event": event})


def build_radio(radio, data_dir: Path, sd_module=None, vocoder_factory=None):
    if isinstance(radio, SoundcardRadio):
        return SoundcardEndpoint(radio, sd_module=sd_module)
    if isinstance(radio, IpscRadio):
        if vocoder_factory:
            vocoder = vocoder_factory(radio.vocoder)
        elif radio.vocoder.type == "thumbdv":
            vocoder = ThumbDV(radio.vocoder.port, radio.vocoder.baudrate)
        else:
            vocoder = NullVocoder()
        return IpscEndpoint(radio, vocoder, data_dir)
    if isinstance(radio, SimulatorRadio):
        return SimulatorEndpoint(radio.id, radio.name, radio.channel, radio.priority, radio.mode, radio.interval_s)
    raise ValueError(f"Type de radio inconnu : {radio}")


def create_app(cfg: Config, sd_module=None, vocoder_factory=None) -> FastAPI:
    state = State(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bus = state.bus
        for c in cfg.channels:
            bus.add_channel(Channel(c.id, c.name, c.description))
        bus.change_listeners.append(state.schedule_push)
        state.events.listeners.append(state.push_event)
        if state.recorder:
            bus.tx_listeners.append(state.recorder)
        for p in cfg.patches:
            bus.patch(p, by="configuration")
        for r in cfg.radios:
            await bus.add_endpoint(build_radio(r, cfg.data_path, sd_module, vocoder_factory))
        bus.start_clock()
        state.events.emit("demarrage", f"CrossGate démarré : {len(cfg.channels)} canaux, {len(cfg.radios)} radios")
        yield
        await bus.shutdown()
        state.events.emit("arret", "CrossGate arrêté")
        state.events.close()

    app = FastAPI(title="CrossGate", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.cg = state

    # ------------------------------------------------------------------ authentification
    def current_user(authorization: str = Header("")) -> UserConfig:
        token = authorization.removeprefix("Bearer ").strip()
        username = read_token(cfg.server.secret, token) if token else None
        user = state.users.get(username or "")
        if not user:
            raise HTTPException(401, "Session expirée, reconnectez-vous")
        return user

    def operator(user: UserConfig = Depends(current_user)) -> UserConfig:
        if user.role not in OPERATOR_ROLES:
            raise HTTPException(403, "Réservé aux opérateurs")
        return user

    def admin(user: UserConfig = Depends(current_user)) -> UserConfig:
        if user.role != "admin":
            raise HTTPException(403, "Réservé aux administrateurs")
        return user

    def public_user(u: UserConfig) -> dict[str, Any]:
        return {"username": u.username, "display_name": u.display_name, "role": u.role, "priority": u.priority}

    failed: dict[str, list[float]] = {}

    @app.post("/api/login")
    async def login(body: Login):
        now = time.time()
        attempts = [t for t in failed.get(body.username, []) if now - t < 300]
        if len(attempts) >= 5:
            raise HTTPException(429, "Trop de tentatives, réessayez dans 5 minutes")
        user = state.users.get(body.username)
        if not user or not verify_password(body.password, user.password):
            await asyncio.sleep(0.5)
            failed[body.username] = attempts + [now]
            state.events.emit("connexion", f"Échec de connexion pour « {body.username} »", "alerte")
            raise HTTPException(401, "Identifiant ou mot de passe incorrect")
        failed.pop(body.username, None)
        state.events.emit("connexion", f"{user.display_name} connecté", user=user.username)
        return {"token": make_token(cfg.server.secret, user.username, cfg.server.session_hours), "user": public_user(user)}

    @app.get("/api/me")
    async def me(user: UserConfig = Depends(current_user)):
        chans = state.user_channels(user)
        return {
            "user": public_user(user),
            "channels": [{"id": c, "name": state.bus.channels[c].name} for c in chans],
            "map": cfg.map,
        }

    # ------------------------------------------------------------------ console
    @app.get("/api/state")
    async def get_state(user: UserConfig = Depends(operator)):
        return {**state.snapshot(), "events": list(state.events.recent)[-100:]}

    @app.post("/api/patches")
    async def create_patch(body: PatchBody, user: UserConfig = Depends(operator)):
        try:
            patch = state.bus.patch(body.channels, by=user.display_name)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"id": patch.id, "channels": sorted(patch.channels)}

    @app.delete("/api/patches/{patch_id}")
    async def delete_patch(patch_id: int, user: UserConfig = Depends(operator)):
        try:
            state.bus.unpatch(patch_id, by=user.display_name)
        except KeyError:
            raise HTTPException(404, "Interconnexion introuvable")
        return {"ok": True}

    @app.post("/api/endpoints/{ep_id}/move")
    async def move_endpoint(ep_id: str, body: MoveBody, user: UserConfig = Depends(operator)):
        if ep_id not in state.bus.endpoints:
            raise HTTPException(404, "Point inconnu")
        try:
            state.bus.move(ep_id, body.channel)
        except ValueError as e:
            raise HTTPException(400, str(e))
        ep = state.bus.endpoints[ep_id]
        if isinstance(ep, WebPttEndpoint):
            ep.send_json({"t": "moved", "channel": body.channel})
        state.events.emit("deplacement", f"{ep.name} placé sur {state.bus.channels[body.channel].name} par {user.display_name}")
        return {"ok": True}

    @app.post("/api/ipsc/{ep_id}/capture")
    async def ipsc_capture(ep_id: str, user: UserConfig = Depends(admin)):
        ep = state.bus.endpoints.get(ep_id)
        if not isinstance(ep, IpscEndpoint):
            raise HTTPException(404, "Liaison IPSC inconnue")
        ep.start_capture()
        return {"ok": True}

    @app.get("/api/recordings")
    async def recordings(user: UserConfig = Depends(operator)):
        return state.recorder.list() if state.recorder else []

    @app.get("/api/recordings/file")
    async def recording_file(path: str, token: str = Query(...)):
        # Lien direct pour le lecteur audio du navigateur : jeton en paramètre
        user = state.users.get(read_token(cfg.server.secret, token) or "")
        if not user or user.role not in OPERATOR_ROLES or not state.recorder:
            raise HTTPException(403, "Accès refusé")
        f = state.recorder.resolve(path)
        if not f:
            raise HTTPException(404, "Enregistrement introuvable")
        return FileResponse(f, media_type="audio/wav")

    @app.get("/api/health")
    async def health():
        return {"ok": True, "endpoints": len(state.bus.endpoints)}

    # ------------------------------------------------------------------ temps réel
    @app.websocket("/ws")
    async def ws(websocket: WebSocket, token: str = Query(""), mode: str = Query("ptt")):
        user = state.users.get(read_token(cfg.server.secret, token) or "")
        if not user:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        allowed = state.user_channels(user)
        channel = user.default_channel if user.default_channel in allowed else allowed[0]
        ep = WebPttEndpoint(
            f"web-{user.username}-{secrets.token_hex(3)}", user.display_name, channel, user.priority, user.username, user.role
        )
        if mode == "console" and user.role in OPERATOR_ROLES:
            ep.kind = "console"
        await state.bus.add_endpoint(ep)
        if ep.kind == "console":
            state.consoles.add(ep)
        ep.send_json(
            {
                "t": "welcome",
                "user": public_user(user),
                "endpoint": ep.id,
                "channel": channel,
                "channels": [{"id": c, "name": state.bus.channels[c].name} for c in allowed],
            }
        )
        if ep.kind == "console":
            ep.send_json({"t": "systeme", **state.snapshot()})

        async def sender():
            while True:
                item = await ep.outbox.get()
                if isinstance(item, bytes):
                    await websocket.send_bytes(item)
                else:
                    await websocket.send_text(json.dumps(item, ensure_ascii=False))

        send_task = asyncio.create_task(sender())
        try:
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    data = msg["bytes"]
                    if len(data) == audio.FRAME_BYTES:
                        state.bus.push_audio(ep, audio.from_bytes(data))
                    continue
                try:
                    cmd = json.loads(msg.get("text") or "{}")
                except json.JSONDecodeError:
                    continue
                t = cmd.get("t")
                if t == "ptt":
                    if cmd.get("on"):
                        try:
                            state.bus.request_floor(ep)
                        except FloorDenied as e:
                            ep.on_floor("denied", {"holder": e.holder.name})
                    else:
                        state.bus.release_floor(ep)
                elif t == "join":
                    ch = cmd.get("channel")
                    if ch in allowed:
                        state.bus.move(ep.id, ch)
                        ep.send_json({"t": "moved", "channel": ch})
                elif t == "gps":
                    try:
                        lat, lon = float(cmd["lat"]), float(cmd["lon"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if -90 <= lat <= 90 and -180 <= lon <= 180:
                        state.positions[user.username] = {
                            "user": user.username, "name": user.display_name, "lat": lat, "lon": lon,
                            "acc": cmd.get("acc"), "ts": time.time(), "online": True, "channel": ep.channel,
                        }
                        state.schedule_push()
                elif t == "ping":
                    ep.send_json({"t": "pong"})
        except WebSocketDisconnect:
            pass
        finally:
            send_task.cancel()
            state.consoles.discard(ep)
            if user.username in state.positions:
                state.positions[user.username]["online"] = False
            await state.bus.remove_endpoint(ep.id)

    # ------------------------------------------------------------------ fichiers web
    @app.get("/console")
    async def console_page():
        return FileResponse(WEB_DIR / "console.html")

    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")

    @app.exception_handler(HTTPException)
    async def http_error(request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app
