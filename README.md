# CrossGate

Passerelle radio sur IP de COGITECH : elle interconnecte les réseaux **HF** (Barrett 4090), **VHF/UHF DMR** (relais Motorola SLR5500, postes MOTOTRBO) et les **smartphones** en 4G ou Wi-Fi, avec une console d'opérateur.

- **Alternat sur smartphone** : application web installable (Android, iPhone), bouton « parler », position GPS, reconnexion automatique.
- **Console** : canaux et participants en direct, interconnexion de canaux en un clic (HF ↔ VHF ↔ 4G), déplacement d'un participant, carte des positions, journal horodaté, écoute des enregistrements, alternat au clavier (barre d'espace).
- **Prise de parole** : un seul émetteur par groupe, refus « canal occupé », priorité du poste de commandement (préemption), durée maximale d'émission, libération automatique.
- **Traçabilité** : chaque émission est enregistrée (WAV) et chaque événement est écrit dans un journal mensuel (JSONL).
- **Sécurité** : comptes avec mots de passe hachés (PBKDF2), sessions signées, rôles (utilisateur, opérateur, administrateur), canaux autorisés par utilisateur, limitation des tentatives de connexion, HTTPS.

## Raccorder les radios

### Mode 1 – Radio « donneuse » par interface audio (recommandé)

Une radio du réseau sert de pont : **DM4600 en station de contrôle** sur le relais SLR5500, ou la **Barrett 4090** pour la HF. Elle est reliée au serveur CrossGate par une interface audio USB :

```
Radio (prise accessoires)           Interface USB               Serveur CrossGate
  Sortie audio RX  ───────────────▶ Entrée micro / ligne
  Entrée audio TX  ◀─────────────── Sortie casque
  PTT              ◀─────────────── RTS d'un port série USB  (ou GPIO 3 d'une carte CM108)
  COS / squelch    ───────────────▶ CTS du port série        (facultatif : sinon détection par niveau, VOX)
```

C'est la radio qui code et décode la voix : **aucun vocodeur n'est nécessaire**, et cela fonctionne avec un relais en numérique comme en analogique, avec ses fonctions (slots, groupes, chiffrement de la radio). Interfaces compatibles : toute carte son USB avec un adaptateur série FTDI pour l'alternat, ou les interfaces radio à puce CM108/CM119 (type DMK URI, RA-35).

Réglages utiles (dans `config.yaml`, section `radios`) : `pre_ms` (délai entre l'alternat et la voix, plus long en HF), `tail_ms`, gains `rx_gain_db` / `tx_gain_db`, seuil VOX `threshold_dbfs`. Le niveau reçu s'affiche en dBFS dans la console pour régler le seuil.

### Mode 2 – Liaison IP directe au relais (IPSC) + vocodeur ThumbDV

CrossGate s'enregistre auprès du SLR5500 comme un site IP Site Connect (authentification IPSC prise en charge) et reçoit les appels du groupe et du slot configurés. La voix DMR (AMBE+2) est décodée par un boîtier USB à puce DVSI (**ThumbDV**, DVstick 30). Sans boîtier, la liaison reste connectée et la console indique « vocodeur indisponible » : le mode 1 prend le relais.

- **Réception** (relais → smartphones et HF) : opérationnelle.
- **Émission** (smartphones et HF → relais) : **expérimentale**. Elle part d'un appel modèle capturé sur votre relais : dans la console, un administrateur clique sur **Modèle** à côté de la liaison IPSC, puis fait un appel d'au moins une seconde depuis une radio du groupe. Activez ensuite `tx_enabled: true`. À valider sur site avant tout usage opérationnel.
- Côté relais : ajoutez la passerelle comme pair IP Site Connect (MOTOTRBO CPS), avec un identifiant unique (`radio_id`) et la même clé d'authentification.

### Et une radio Wi-Fi ou LTE ?

Les radios MOTOTRBO avec Wi-Fi (R7, Ion) utilisent le Wi-Fi pour la programmation et les mises à jour, pas pour transporter la voix vers un logiciel tiers. Les postes hybrides comme le MOTOTRBO Ion passent par WAVE PTX, le service de Motorola, sur abonnement. Un connecteur WAVE PTX pourrait être ajouté plus tard s'il est souscrit. D'ici là, la radio donneuse câblée (mode 1) reste la solution la plus simple et la plus fiable.

## Installation

### Avec Docker (serveur Linux)

```bash
git clone <dépôt> crossgate && cd crossgate
mkdir -p config donnees
cp config/config.example.yaml config/config.yaml
docker compose build
docker compose run --rm crossgate python -m crossgate mot-de-passe   # empreinte à coller dans config.yaml
docker compose run --rm crossgate python -m crossgate cartes-son     # noms des cartes son
nano config/config.yaml        # canaux, utilisateurs, radios, phrase secrète
docker compose up -d
```

Adaptez `CROSSGATE_IP` et la liste `devices` dans `docker-compose.yml` (ports série, ThumbDV). Un certificat HTTPS auto-signé est créé au premier démarrage dans `config/certificats/`.

### Sans Docker (Windows ou Linux)

```bash
python -m venv .venv && . .venv/bin/activate      # Windows : .venv\Scripts\activate
pip install -r requirements.txt
python -m crossgate config/config.yaml
```

## Utilisation

- **Smartphones** : ouvrez `https://<adresse-du-serveur>:8443`. Acceptez l'avertissement du certificat (ou installez le certificat sur les téléphones), connectez-vous, puis **Ajouter à l'écran d'accueil**. Le micro et le GPS ne fonctionnent qu'en HTTPS.
- **Console** : `https://<adresse-du-serveur>:8443/console`, avec un compte opérateur ou administrateur.
- **Hors ligne** : sans Internet, la carte affiche les positions sans fond de carte. Pour un fond hors ligne, renseignez dans `map.tiles` l'adresse d'un serveur de tuiles local.

## Vérifications effectuées

- 34 tests automatiques (`pip install -r requirements-dev.txt && pytest`) : traitement audio (rééchantillonnage 48/8 kHz, anti-repliement), prise de parole (occupation, priorité, libération, durée maximale), interconnexions, enregistrement, sécurité (mots de passe, jetons, droits, limitation), protocole IPSC (authentification, enregistrement, liste des pairs, extraction AMBE), échange complet avec un faux relais, protocole du vocodeur DVSI avec une puce simulée, interface audio avec carte son et port série simulés.
- Essai dans un vrai navigateur Chrome : deux smartphones et une console, alternat avec micro simulé, réception, refus « canal occupé », interconnexion HF ↔ 4G avec radio simulée, enregistrements et positions GPS.

## À valider sur site avec le matériel

1. Niveaux audio, délais d'alternat et seuil VOX avec la DM4600 et la Barrett 4090.
2. Enregistrement IPSC auprès du SLR5500 (version de firmware, clé d'authentification).
3. Décodage avec le ThumbDV (débit 460 800 ou 921 600 bauds selon le modèle).
4. Émission IPSC expérimentale à partir d'un appel modèle.
5. Comportement des smartphones en 4G (latence, mise en veille de l'écran).

## Structure

```
crossgate/
  audio.py          trames PCM 8 kHz, rééchantillonnage, niveaux
  bus.py            canaux, interconnexions, prise de parole
  endpoints/        raccordements : interface audio, IPSC, smartphone, simulateur
  ipsc/protocol.py  protocole Motorola IP Site Connect
  vocoder.py        puce DVSI AMBE-3000 (ThumbDV)
  recorder.py       enregistrement des émissions
  server.py         API, WebSocket, sécurité
web/                application smartphone, console, moteur audio du navigateur
tests/              tests automatiques
```
