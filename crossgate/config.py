"""Configuration CrossGate (fichier YAML)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Union

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8443
    ssl_cert: str | None = None
    ssl_key: str | None = None
    data_dir: str = "./donnees"
    secret: str = Field(..., min_length=16, description="Clé de signature des sessions (au moins 16 caractères)")
    session_hours: int = 12


class AudioConfig(BaseModel):
    hang_ms: int = Field(800, ge=100, le=5000)
    tot_s: int = Field(60, ge=5, le=600)
    record: bool = True


class ChannelConfig(BaseModel):
    id: str = Field(..., pattern=r"^[a-z0-9_-]+$")
    name: str
    description: str = ""


class UserConfig(BaseModel):
    username: str = Field(..., pattern=r"^[a-zA-Z0-9_.-]+$")
    display_name: str
    password: str = Field(..., description="Empreinte pbkdf2 (python -m crossgate mot-de-passe)")
    role: Literal["utilisateur", "operateur", "admin"] = "utilisateur"
    priority: int = 0
    channels: list[str] | Literal["*"] = "*"
    default_channel: str | None = None


# ---------------------------------------------------------------- radios


class PttConfig(BaseModel):
    type: Literal["serie", "cm108", "aucun"] = "serie"
    port: str | None = None  # /dev/ttyUSB0, COM3 ; pour cm108 : /dev/hidraw0
    line: Literal["rts", "dtr"] = "rts"
    invert: bool = False
    gpio: int = 3  # CM108 : broche GPIO de l'alternat (3 sur la plupart des interfaces)
    pre_ms: int = Field(150, ge=0, le=2000, description="Délai entre l'alternat et l'audio")
    tail_ms: int = Field(250, ge=0, le=3000, description="Maintien de l'alternat après l'audio")


class CosConfig(BaseModel):
    type: Literal["vox", "serie_cts", "serie_dsr", "serie_cd"] = "vox"
    port: str | None = None
    invert: bool = False
    threshold_dbfs: float = Field(-42.0, ge=-80, le=0)


# Réglages par famille de radio donneuse : délai d'alternat, maintien, seuil VOX.
# Une valeur écrite explicitement dans la configuration reste prioritaire.
PROFILES: dict[str, dict] = {
    "dmr": {"pre_ms": 150, "tail_ms": 250, "threshold_dbfs": -42},            # DM4600, DM4000, postes MOTOTRBO
    "analogique": {"pre_ms": 100, "tail_ms": 200, "threshold_dbfs": -40},    # VHF/UHF analogique
    "hf": {"pre_ms": 300, "tail_ms": 400, "threshold_dbfs": -38},            # Barrett 4050/4090, HF ALE
    "huawei-elte-portatif": {"pre_ms": 450, "tail_ms": 300, "threshold_dbfs": -40},  # EP650, EP680/681, EP720, EP820/821
    "huawei-elte-vehicule": {"pre_ms": 400, "tail_ms": 300, "threshold_dbfs": -40},  # EV750, EV751
}


class SoundcardRadio(BaseModel):
    type: Literal["audio"]
    id: str
    name: str
    channel: str
    profil: Literal["dmr", "analogique", "hf", "huawei-elte-portatif", "huawei-elte-vehicule"] | None = None
    priority: int = 0
    input_device: str | int | None = None
    output_device: str | int | None = None
    sample_rate: int = 48000
    rx_gain_db: float = 0.0
    tx_gain_db: float = 0.0
    ptt: PttConfig = PttConfig()
    cos: CosConfig = CosConfig()

    @model_validator(mode="before")
    @classmethod
    def _apply_profile(cls, data):
        if isinstance(data, dict) and data.get("profil") in PROFILES:
            prof = PROFILES[data["profil"]]
            ptt = dict(data.get("ptt") or {})
            cos = dict(data.get("cos") or {})
            ptt.setdefault("pre_ms", prof["pre_ms"])
            ptt.setdefault("tail_ms", prof["tail_ms"])
            cos.setdefault("threshold_dbfs", prof["threshold_dbfs"])
            data = {**data, "ptt": ptt, "cos": cos}
        return data


class VocoderConfig(BaseModel):
    type: Literal["thumbdv", "aucun"] = "thumbdv"
    port: str = "/dev/ttyUSB0"
    baudrate: int = 460800


class IpscRadio(BaseModel):
    type: Literal["ipsc"]
    id: str
    name: str
    channel: str
    priority: int = 0
    master_ip: str
    master_port: int = 50000
    local_port: int = 50001
    radio_id: int = Field(..., ge=1, le=0xFFFFFFFF, description="Identifiant IPSC de la passerelle (unique sur le système)")
    auth_key: str | None = Field(None, description="Clé d'authentification IPSC en hexadécimal (jusqu'à 40 caractères)")
    timeslot: Literal[1, 2] = 1
    talkgroup: int = Field(1, ge=1, le=0xFFFFFF)
    keepalive_s: float = 5.0
    tx_enabled: bool = Field(False, description="Émission vers le relais (expérimental : voir README)")
    tx_template: str | None = None
    vocoder: VocoderConfig = VocoderConfig()

    @field_validator("auth_key")
    @classmethod
    def _hex(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        v = v.strip()
        if len(v) > 40:
            raise ValueError("40 caractères hexadécimaux au maximum")
        bytes.fromhex(v.rjust(40, "0"))
        return v


class SimulatorRadio(BaseModel):
    type: Literal["simulateur"]
    id: str
    name: str
    channel: str
    priority: int = 0
    mode: Literal["bip", "echo"] = "echo"
    interval_s: float = 30.0


RadioConfig = Annotated[Union[SoundcardRadio, IpscRadio, SimulatorRadio], Field(discriminator="type")]


class Config(BaseModel):
    server: ServerConfig
    audio: AudioConfig = AudioConfig()
    channels: list[ChannelConfig]
    patches: list[list[str]] = []
    users: list[UserConfig]
    radios: list[RadioConfig] = []
    map: dict = Field(
        default_factory=lambda: {
            "center": [-5.55, 7.54],
            "zoom": 6,
            "tiles": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
            "attribution": "© OpenStreetMap",
        }
    )

    @model_validator(mode="after")
    def _check_refs(self) -> "Config":
        ids = {c.id for c in self.channels}
        if len(ids) != len(self.channels):
            raise ValueError("Identifiants de canaux en double")
        for r in self.radios:
            if r.channel not in ids:
                raise ValueError(f"Radio {r.id} : canal inconnu « {r.channel} »")
        radio_ids = [r.id for r in self.radios]
        if len(set(radio_ids)) != len(radio_ids):
            raise ValueError("Identifiants de radios en double")
        for p in self.patches:
            if set(p) - ids:
                raise ValueError(f"Interconnexion {p} : canal inconnu")
        for u in self.users:
            if u.channels != "*" and set(u.channels) - ids:
                raise ValueError(f"Utilisateur {u.username} : canal inconnu")
            if u.default_channel and u.default_channel not in ids:
                raise ValueError(f"Utilisateur {u.username} : canal par défaut inconnu")
        if len({u.username for u in self.users}) != len(self.users):
            raise ValueError("Noms d'utilisateur en double")
        return self

    @property
    def data_path(self) -> Path:
        return Path(self.server.data_dir)


def load(path: str | Path) -> Config:
    with open(path, encoding="utf-8") as f:
        return Config.model_validate(yaml.safe_load(f))
