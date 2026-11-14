"""Point d'entrée : python -m crossgate [config.yaml]  |  python -m crossgate mot-de-passe"""

from __future__ import annotations

import getpass
import logging
import sys


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] in ("mot-de-passe", "password"):
        from crossgate.auth import hash_password

        pwd = getpass.getpass("Mot de passe : ")
        if len(pwd) < 8:
            sys.exit("8 caractères au minimum")
        if pwd != getpass.getpass("Confirmation : "):
            sys.exit("Les mots de passe ne correspondent pas")
        print(hash_password(pwd))
        return
    if args and args[0] in ("audio", "cartes-son"):
        import sounddevice

        print(sounddevice.query_devices())
        return

    import uvicorn

    from crossgate.config import load
    from crossgate.server import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load(args[0] if args else "config.yaml")
    ssl = {}
    if cfg.server.ssl_cert and cfg.server.ssl_key:
        ssl = {"ssl_certfile": cfg.server.ssl_cert, "ssl_keyfile": cfg.server.ssl_key}
    else:
        logging.warning("HTTPS désactivé : le micro des smartphones ne fonctionnera qu'en HTTPS (voir README)")
    uvicorn.run(create_app(cfg), host=cfg.server.host, port=cfg.server.port, log_level="info", **ssl)


if __name__ == "__main__":
    main()
