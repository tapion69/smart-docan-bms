# Smart Docan BMS

Add-on **Home Assistant OS / Supervised** pour les batteries **Docan Power NOON équipées d'un BMS Daren** diffusant un nom Bluetooth commençant par `DWF`.

> **Statut : 0.1.4-dev — développement, validé sur une première installation à 4 batteries.**
> La lecture des 64 cellules a été confirmée sur cette installation. La connexion MQTT reste à valider sur le matériel cible après configuration manuelle.
> Ce projet n'effectue aucune commande de configuration des BMS : uniquement des requêtes de télémétrie. Il ne remplace **jamais** les protections du BMS.

## Fonctionnalités de cette version

- Découverte Bluetooth automatique des BMS `DWF*` sur le mini-PC Home Assistant.
- Plusieurs BMS, lecture séquentielle pour limiter les conflits sur l'adaptateur.
- Mesures retournées par [aiobmsble](https://github.com/patman15/aiobmsble), bibliothèque Apache-2.0 (version 0.33.0), dont les **16 tensions de cellules** quand elles sont transmises.
- Tension, courant, puissance, SOC, SOH, capacité, cycles, écarts, numéros des cellules min/max, températures individuelles, équilibrage, MOSFET, alarmes et informations disponibles selon le firmware.
- MQTT Discovery : entités créées automatiquement **uniquement lorsqu'une valeur est réellement reçue**.
- Tableau de bord en français dans Home Assistant (**Ingress**, port interne 1895).
- Synthèse multi-batteries en **parallèle** : SOC pondéré par capacité lorsque connue, somme courants et puissances, tension moyenne, suspendue si une batterie connue est hors ligne.
- Échecs Bluetooth : 3 erreurs consécutives avant passage hors ligne; reconnexion automatique au cycle suivant.
- MQTT automatique (identifiants du Supervisor) ou manuel (hôte, port, identifiant et mot de passe masqué).
- Période cible de collecte configurable : mesurée du **début du cycle BLE au début du cycle BLE suivant**, et non comme une pause systématique à la fin. Publication MQTT lors des lectures réussies, indépendante du rafraîchissement de la page.

## Installation

1. **Pré-requis** : Home Assistant OS/Supervised avec adaptateur Bluetooth fonctionnel, intégration Bluetooth activée et broker Mosquitto configuré avec l'intégration MQTT.
2. Dans **Paramètres → Modules complémentaires / Applications → Boutique → ⋮ → Dépôts**, ajouter `https://github.com/tapion69/smart-docan-bms`.
3. Installer **Smart Docan BMS**, ouvrir la configuration et laisser `auto_discover: true` pour retrouver les BMS `DWF`.
4. Démarrer l'add-on, consulter l'onglet **Journal** puis **Ouvrir l'interface web**.
5. Les appareils/capteurs apparaîtront via **MQTT** après une première lecture réussie.

Le Supervisor installe uniquement depuis la branche principale de ce dépôt. Les branches de développement nécessitent une fusion dans `main` avant de pouvoir être installées par URL.

## Identifiants fixes (depuis 0.1.3-dev)

Les **`unique_id`** MQTT ne sont plus dérivés de l'adresse MAC Bluetooth. Ils sont désormais liés à l'emplacement logique de la batterie (slot) et identiques d'une installation à l'autre.

| Exemple de mesure | Unique ID et `default_entity_id` conseillé |
| --- | --- |
| Batterie 1, SOC | `smart_docan_bms_battery_1_soc` / `sensor.smart_docan_bms_battery_1_soc` |
| Batterie 1, tension | `smart_docan_bms_battery_1_voltage` / `sensor.smart_docan_bms_battery_1_voltage` |
| Batterie 1, cellule 01 | `smart_docan_bms_battery_1_cell_01` / `sensor.smart_docan_bms_battery_1_cell_01` |
| Batterie 4, cellule 16 | `smart_docan_bms_battery_4_cell_16` / `sensor.smart_docan_bms_battery_4_cell_16` |
| Global, SOC | `smart_docan_bms_global_soc` / `sensor.smart_docan_bms_global_soc` |

Le `default_entity_id` est **une suggestion à la première création** : Home Assistant peut ajouter un suffixe si un identifiant est déjà utilisé ou si l'utilisateur l'a renommé. Le `unique_id`, lui, reste fixe par slot et par mesure.

**Association des batteries :** au premier démarrage, l'add-on attribue les BMS `DWF` découverts aux emplacements libres 1–4 puis sauvegarde les correspondances Bluetooth dans `/data/slots.json`. Le fichier survit aux mises à jour et redémarrages, même si une batterie disparaît temporairement du scan. Un nouvel appareil **ne remplace jamais automatiquement** une batterie déjà affectée.

Si l'ordre physique importe, renseigner **`slot_1_address` à `slot_4_address`** dans la configuration (l'adresse MAC n'est jamais écrite dans GitHub). Une adresse configurée manuellement prend priorité sur une attribution automatique; on peut également changer les noms des quatre emplacements.

`strict_slots: true` interdit l'attribution de tout BMS non déjà associé. Avec `false`, la découverte des nouveaux BMS `DWF` reste automatique dans les slots disponibles.

### Migration depuis la version basée sur les MAC

Par défaut, `migrate_legacy_entities: true` publie les nouveaux capteurs MQTT fixes et **supprime ensuite du broker les anciennes annonces MQTT retenues** liées à l'adresse MAC et les anciennes entités globales. Les anciennes entités peuvent donc disparaître de Home Assistant, et les anciennes références de dashboard / automatisations doivent être adaptées. Les historiques ne sont pas transférés automatiquement d'un `unique_id` à l'autre.

Pour conserver temporairement les anciennes entités (par exemple, pendant une migration de dashboard), placer `migrate_legacy_entities: false` **avant** le premier lancement de 0.1.3-dev. Cela conserve les anciennes annonces retenues du broker mais peut afficher des entités obsolètes ou indisponibles; il faudra les nettoyer ensuite.

**Sauvegarde recommandée :** effectuer une sauvegarde Home Assistant avant le changement de version. Ne pas supprimer `/data/slots.json` pendant les mises à jour.

## Logo

Le logo choisi pour Smart Docan BMS (batterie à quatre cellules vertes, connexion Bluetooth bleue et courbe de télémétrie) est intégré sous `smart_docan_bms/icon.png` et `smart_docan_bms/logo.png`. Il apparaît dans la boutique et la page de l'add-on ainsi que dans le tableau de bord Ingress.

## Options

| Option | Exemple | Rôle |
| --- | --- | --- |
| `auto_discover` | `true` | Trouve automatiquement les appareils `DWF*` |
| `strict_slots` | `false` | Ne pas attribuer automatiquement de nouveaux BMS lorsqu'activé |
| `migrate_legacy_entities` | `true` | Supprime les anciennes découvertes MQTT après publication des nouveaux ID |
| `slot_1_address` à `slot_4_address` | `""` | Adresses Bluetooth optionnelles fixées par emplacement |
| `slot_1_name` à `slot_4_name` | `Batterie 1`… | Noms personnalisés des batteries |
| `addresses` | `""` | Liste optionnelle d'adresses Bluetooth séparées par virgules ou points-virgules ; si remplie, seuls ces appareils sont interrogés |
| `names` | `""` | Ancienne option conservée pour compatibilité (utiliser désormais `slot_1_name` à `slot_4_name`) |
| `scan_seconds` | `8` | Durée d'une recherche BLE |
| `rescan_seconds` | `180` | Intervalle entre deux recherches BLE |
| `poll_seconds` | `30` | Période cible entre débuts des cycles de lecture complets. Inclut le temps BLE (minimum configurable : 5 s) |
| `dashboard_refresh_seconds` | `20` | Actualisation automatique de la page Ingress (5–120 s) |
| `mqtt_mode` | `auto` | `auto` utilise le service MQTT du Supervisor, `manual` prend les valeurs ci-dessous |
| `mqtt_host` | `core-mosquitto` | Serveur MQTT pour le mode manuel |
| `mqtt_port` | `1883` | Port TCP MQTT (par défaut non chiffré) |
| `mqtt_username` | `""` | Identifiant MQTT fourni par le broker |
| `mqtt_password` | `""` | Mot de passe masqué dans le formulaire |
| `mqtt_tls` | `false` | Activer uniquement pour un serveur TLS correctement configuré |
| `log_level` | `info` | `info`, `debug` ou `warning` |

Les adresses personnelles de l'installation ne sont pas présentes dans le code.

## Connexion MQTT manuelle (Mosquitto)

Pour utiliser un utilisateur MQTT explicitement autorisé par Mosquitto, ouvrir l'onglet **Configuration** de Smart Docan BMS (sur Home Assistant, pas sur GitHub) :

1. Mettre `mqtt_mode: manual`.
2. Indiquer `mqtt_host: core-mosquitto`, `mqtt_port: 1883` si le broker Mosquitto officiel tourne sur cette même installation Home Assistant.
3. Renseigner `mqtt_username` et `mqtt_password` correspondant à un compte MQTT dédié (créé/autorisé dans Mosquitto).
4. Laisser `mqtt_tls: false` pour le port 1883 standard.
5. **Enregistrer** puis **redémarrer** l'add-on. Vérifier "MQTT : Connecté" sur le dashboard et la présence des entités dans l'intégration MQTT de Home Assistant.

En mode `auto`, `mqtt_host`, `mqtt_port`, `mqtt_username` et `mqtt_password` sont volontairement ignorés et les données du service Supervisor sont utilisées à la place. La déclaration du service est facultative (`mqtt:want`) pour permettre un broker externe.

**Confidentialité** : le mot de passe est une option `password` masquée par le formulaire Home Assistant. Il est enregistré localement dans les options de l'add-on, jamais inclus dans ce dépôt ni exposé dans la page web ou les journaux. Ne pas partager de capture de l'écran de configuration avec des secrets visibles.

## Régler la fréquence des remontées

- `poll_seconds: 30` = lancement d'un cycle de lecture des quatre batteries au maximum toutes les 30 secondes. Si le cycle BLE prend 9 s, l'add-on attend 21 s. Si le cycle prend 38 s, le cycle suivant démarre sans pause additionnelle.
- Les mesures sont publiées au broker MQTT après chaque interrogation d'une batterie; Home Assistant reçoit donc les nouveaux états à chaque cycle.
- `dashboard_refresh_seconds: 20` = fréquence de rafraîchissement de la page web; ce paramètre **ne pilote pas** le Bluetooth et n'accélère pas les valeurs MQTT.
- La page affiche la **durée du dernier cycle BLE complet** afin de vérifier si la période souhaitée est réalisable avec 4 BMS.

Les options enregistrées prennent effet au redémarrage de l'add-on. La valeur `poll_seconds: 5` est autorisée depuis 0.1.4-dev. Avec quatre batteries lues successivement, si les quatre échanges Bluetooth demandent plus de cinq secondes, la cadence réelle sera plus longue : aucune lecture en parallèle ni accélération forcée. Des délais trop courts peuvent dégrader la stabilité Bluetooth ; commencer à 30 s et diminuer progressivement.

## Diagnostic MQTT et températures

- Si MQTT affiche **Non connecté**, le panneau indique l’hôte et le port du service Supervisor, l’état de connexion et le compteur d’échecs (jamais le mot de passe).
- Vérifier les journaux du module **Smart Docan BMS**, ainsi que ceux de **Mosquitto broker**. Les erreurs TCP/TLS ne sont plus silencieuses.
- Si une batterie affiche **Températures : Non disponible**, la ligne de diagnostic indique le nombre de sondes annoncé dans la trame, le type de données et le nombre de mesures effectivement décodées.
- L’onglet **Autres informations disponibles** présente les capacités, numéros de cellules extrêmes, MOSFET, équilibrage et codes remontés. Aucun paramètre BMS n’est écrit.

## Coexistence avec BLE Battery Management (HACS)

**Important :** cette application établit ses propres connexions BLE. Pour éviter les conflits d'accès au même BMS, désactivez temporairement l'entrée des BMS `DWF` dans l'intégration HACS *BLE Battery Management System* pendant les tests de notre add-on. Ne supprimez pas HACS : une simple désactivation suffit et reste réversible.

## Limites et sécurité

- Les 16 valeurs de cellules sont créées **si** le BMS les fournit. Aucun capteur inventé ni tension simulée.
- La synthèse courant/puissance suppose un **câblage parallèle**. Elle n'est pas adaptée à une architecture série ou mixte.
- Les données sont indicatives ; ne les utilisez pas pour les protections critiques contre la surcharge, la sous-tension ou l'emballement thermique.
- La découverte et la lecture de 4 batteries Daren et de 64 cellules ont été vérifiées sur une installation réelle; MQTT manuel, répétition des lectures et reconnexion après incident restent à confirmer sur place.
- Si la carte réseau Bluetooth n'est pas accessible depuis le conteneur, contrôler l'option `host_dbus` et les journaux. Ne pas désactiver AppArmor ou le mode protégé sans diagnostic.
- Les mesures de capacité restante ou d'énergie stockée peuvent être calculées et sont alors des **estimations**, pas des compteurs d'énergie.

## Structure

```text
repository.yaml
smart_docan_bms/
  config.yaml        # manifest HA Supervisor
  Dockerfile
  requirements.txt
  app/
    run.py           # boucle Bluetooth et MQTT
    logic.py         # traitement des données
    settings.py      # options MQTT et cadence
    slots.py         # associations BMS -> emplacement, stockées dans /data/slots.json
    discovery.py     # découverte MQTT avec IDs fixes et migration MAC
    dashboard.py     # interface HTML Ingress
  icon.png           # icône officielle de l'add-on
  logo.png           # logo accepté
  translations/
    fr.yaml
    en.yaml
tests/
  test_logic.py
```

## Dépendances et attributions

Le décodage Daren est fourni **sans copie du code de ce projet** par [patman15/aiobmsble](https://github.com/patman15/aiobmsble), **Apache License 2.0**, maintenu notamment par Patrick Loschmidt et les contributeurs. Les autres dépendances sont décrites dans `requirements.txt`.
