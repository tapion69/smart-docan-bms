# Smart Docan BMS

Add-on **Home Assistant OS / Supervised** pour les batteries **Docan Power NOON équipées d'un BMS Daren** diffusant un nom Bluetooth commençant par `DWF`.

> **Statut : 0.1.7-dev — développement, validé sur une première installation à 4 batteries.**
> La lecture des 64 cellules a été confirmée sur cette installation. La connexion MQTT reste à valider sur le matériel cible après configuration manuelle.
> Ce projet n'effectue aucune commande de configuration des BMS : uniquement des requêtes de télémétrie. Il ne remplace **jamais** les protections du BMS.

## Fonctionnalités de cette version

- Découverte Bluetooth automatique des BMS `DWF*` sur le mini-PC Home Assistant.
- Plusieurs BMS, lecture séquentielle pour limiter les conflits sur l'adaptateur.
- Mesures retournées par [aiobmsble](https://github.com/patman15/aiobmsble), bibliothèque Apache-2.0 (version 0.33.0), dont les **16 tensions de cellules** quand elles sont transmises.
- Tension, courant, puissance, SOC, SOH, capacité, cycles, écarts, numéros des cellules min/max, températures individuelles, équilibrage, MOSFET, alarmes et informations disponibles selon le firmware.
- MQTT Discovery : entités créées automatiquement **uniquement lorsqu'une valeur est réellement reçue**.
- **Sans serveur web ni Ingress** : l’add-on est un collecteur Bluetooth/MQTT uniquement. Un dashboard HTML Home Assistant séparé sera conçu ultérieurement.
- Synthèse multi-batteries en **parallèle** : SOC pondéré par capacité lorsque connue, somme courants et puissances, tension moyenne, suspendue si une batterie connue est hors ligne.
- Échecs Bluetooth : 3 erreurs consécutives avant passage hors ligne; reconnexion automatique au cycle suivant.
- MQTT automatique (identifiants du Supervisor) ou manuel (hôte, port, identifiant et mot de passe masqué).
- Période cible de collecte configurable : mesurée du **début du cycle BLE au début du cycle BLE suivant**, et non comme une pause systématique à la fin. Publication MQTT lors des lectures réussies, indépendante du rafraîchissement de la page.

## Présentation des mesures (depuis 0.1.7-dev)

Le collecteur applique les règles suivantes avant publication MQTT :

- **Tensions des cellules et mesures de tension associées** (min/max, delta, écarts entre batteries) : 3 chiffres après la virgule, par exemple **3,300 V** et **0,003 V**.
- **Tension individuelle de chaque pack** : 2 chiffres après la virgule, par exemple **53,12 V**.
- **Tension moyenne globale** : 3 chiffres après la virgule.
- **Autonomie estimée** : création d'une entité texte supplémentaire, par exemple **1 j 4 h 15 min** ou **2 h 30 min**. Le capteur numérique `runtime` (secondes) est conservé pour les automatisations et pour ne pas changer les `unique_id` existants.

Les capteurs MQTT de tension utilisent `suggested_display_precision` et des modèles d'état à précision fixe. Ils restent numériques : les décimales ajoutées à l'affichage ne signifient pas que le BMS a gagné en précision de mesure. Les entités ne sont créées que si la donnée correspondante est réellement fournie.

Le dashboard HTML prévu ultérieurement sera développé **séparément** dans Home Assistant et réutilisera les entités fixes MQTT, sans serveur web dans l'add-on.

## Installation

1. **Pré-requis** : Home Assistant OS/Supervised avec adaptateur Bluetooth fonctionnel, intégration Bluetooth activée et broker Mosquitto configuré avec l'intégration MQTT.
2. Dans **Paramètres → Modules complémentaires / Applications → Boutique → ⋮ → Dépôts**, ajouter `https://github.com/tapion69/smart-docan-bms`.
3. Installer **Smart Docan BMS**, ouvrir la configuration et laisser `auto_discover: true` pour retrouver les BMS `DWF`.
4. Démarrer l'add-on et consulter uniquement l'onglet **Journal** (pas de bouton d'interface web).
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

Le logo choisi pour Smart Docan BMS (batterie à quatre cellules vertes, connexion Bluetooth bleue et courbe de télémétrie) est conservé sous `smart_docan_bms/icon.png` et `smart_docan_bms/logo.png` pour la fiche de l’add-on. Aucun serveur HTTP n'est intégré.

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
| `rescan_seconds` | `180` | Intervalle normal entre deux recherches BLE quand tous les BMS affectés sont visibles |
| `missing_rescan_seconds` | `30` | Recherche accélérée lorsque au moins une batterie enregistrée n'est pas vue au scan (15 à 180 s) |
| `recover_stale_connections` | `false` | Autorisation explicite de déconnecter uniquement une batterie Bluetooth durablement absente |
| `stale_connection_after_seconds` | `180` | Seuil d'absence et de non-lecture avant tentative ciblée (60 à 1800 s) |
| `poll_seconds` | `30` | Période cible entre débuts des cycles de lecture complets. Inclut le temps BLE (minimum configurable : 5 s) |
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
5. **Enregistrer** puis **redémarrer** l'add-on. Vérifier « MQTT connecté » dans les journaux et la présence des entités dans l'intégration MQTT de Home Assistant.

En mode `auto`, `mqtt_host`, `mqtt_port`, `mqtt_username` et `mqtt_password` sont volontairement ignorés et les données du service Supervisor sont utilisées à la place. La déclaration du service est facultative (`mqtt:want`) pour permettre un broker externe.

**Confidentialité** : le mot de passe est une option `password` masquée par le formulaire Home Assistant. Il est enregistré localement dans les options de l'add-on, jamais inclus dans ce dépôt ni exposé dans la page web ou les journaux. Ne pas partager de capture de l'écran de configuration avec des secrets visibles.

## Régler la fréquence des remontées

- `poll_seconds: 30` = lancement d'un cycle de lecture des quatre batteries au maximum toutes les 30 secondes. Si le cycle BLE prend 9 s, l'add-on attend 21 s. Si le cycle prend 38 s, le cycle suivant démarre sans pause additionnelle.
- Les mesures sont publiées au broker MQTT après chaque interrogation d'une batterie; Home Assistant reçoit donc les nouveaux états à chaque cycle.
- Il n'y a plus de paramètre `dashboard_refresh_seconds` : les mises à jour MQTT et le futur dashboard HTML seront indépendants. La cadence de lecture BLE est toujours limitée par le temps de communication avec les quatre batteries.

Les options enregistrées prennent effet au redémarrage de l'add-on. La valeur `poll_seconds: 5` est autorisée depuis 0.1.7-dev. Avec quatre batteries lues successivement, si les quatre échanges Bluetooth demandent plus de cinq secondes, la cadence réelle sera plus longue : aucune lecture en parallèle ni accélération forcée. Des délais trop courts peuvent dégrader la stabilité Bluetooth ; commencer à 30 s et diminuer progressivement.

## Diagnostic MQTT et températures

- Si MQTT n'arrive pas à se connecter, les journaux indiquent l'hôte, le port, les codes d'échec et les tentatives, sans enregistrer le mot de passe.
- Vérifier les journaux du module **Smart Docan BMS**, ainsi que ceux de **Mosquitto broker**. Les erreurs TCP/TLS ne sont plus silencieuses.
- Si les températures sont absentes, le journal indique le nombre de sondes annoncé dans la trame et les mesures décodées.
- Les capacités, cellules extrêmes, MOSFET, équilibrage et codes disponibles continuent d'être publiés comme entités MQTT. Aucun paramètre BMS n'est écrit.

## Coexistence avec BLE Battery Management (HACS)

**Important :** cette application établit ses propres connexions BLE. Pour éviter les conflits d'accès au même BMS, désactivez temporairement l'entrée des BMS `DWF` dans l'intégration HACS *BLE Battery Management System* pendant les tests de notre add-on. Ne supprimez pas HACS : une simple désactivation suffit et reste réversible.

## Récupération Bluetooth en cas de batterie absente (0.1.7-dev)

L'association des batteries aux slots 1 à 4 reste sauvegardée dans `/data/slots.json`. Lorsqu'un scan ne voit que trois batteries affectées sur quatre, la recherche suivante est programmée après **`missing_rescan_seconds` (30 s par défaut)** au lieu de `rescan_seconds` (180 s par défaut).

L'add-on consigne le numéro de la batterie non vue. Si une batterie dont la connexion avait été établie échoue trois fois, elle est marquée hors ligne, son ancien objet Bluetooth BlueZ est abandonné et un nouveau scan est avancé. Les reconnexions à ce BMS suivent un délai progressif, sans suspendre intentionnellement les trois autres batteries. Chaque échange BMS est borné à 30 secondes pour éviter un blocage permanent.

La réapparition d'une batterie lors d'un scan et le rétablissement des mesures sont consignés dans le journal. Les `unique_id` MQTT et les affectations par slot ne changent pas.

**Limite matérielle** : si le BMS cesse réellement toute publicité Bluetooth ou s'il est hors de portée, l'add-on ne peut pas le réveiller ni garantir son retour. Vérifier alors sa visibilité dans Home Assistant, un autre scanner et la qualité du signal. Ne pas réinitialiser le contrôleur hci0 (partagé avec Home Assistant), et éviter plusieurs clients connectés simultanément au même BMS. En cas de pertes répétées, on peut augmenter `scan_seconds` de 8 à 12–15 secondes et garder `missing_rescan_seconds` à 30 secondes ; cela augmente le temps consacré à la recherche.

## Connexion Bluetooth bloquée dans BlueZ (0.1.7-dev)

Lorsque Home Assistant affiche encore une batterie dans **Bluetooth → Connexions**, même après l'arrêt de l'add-on, elle peut être occupée par un autre client ou par une connexion BlueZ orpheline. Dans ce cas, un scan ne pourra parfois plus recevoir d'annonces de cette batterie. La récupération standard 0.1.6 (recherche toutes les 30 s) peut ne pas suffire.

La nouvelle option **`recover_stale_connections: false`** est désactivée par défaut. Une fois activée volontairement, l'add-on peut tenter une **déconnexion BlueZ ciblée uniquement sur l'adresse d'une batterie associée à un slot** quand *toutes* ces conditions sont réunies :

- Au moins deux scans consécutifs n'ont pas vu la batterie.
- La batterie n'a été détectée **ni lue avec succès** depuis au moins `stale_connection_after_seconds` (180 s par défaut, plage 60–1800).
- Aucune déconnexion ciblée n'a été tentée pour ce slot dans l'heure précédente.

Cette action utilise `close_stale_connections_by_address` de `bleak_retry_connector` et **ne redémarre pas hci0**, n'efface pas les associations et ne modifie pas les autres appareils. Elle peut néanmoins **couper une connexion légitime d'une autre application** sur la batterie concernée. Désactiver tout autre client BLE BMS avant de l'autoriser. La commande peut être refusée par les permissions D-Bus / BlueZ : ce cas sera signalé dans les journaux, sans désactiver AppArmor ni augmenter les privilèges du conteneur.

Pour un diagnostic de la batterie 1 qui reste seule dans **Bluetooth → Connexions** après arrêt de l'add-on, les réglages recommandés pour le test sont :

```yaml
recover_stale_connections: true
stale_connection_after_seconds: 180
missing_rescan_seconds: 30
```

Surveiller les messages « déconnexion BlueZ ciblée », « recherche du BMS relancée » et les nouvelles lectures. Une demande de déconnexion terminée **ne garantit pas** que le BMS répondra de nouveau ; le succès est confirmé uniquement par le retour des mesures.

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
