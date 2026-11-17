# Smart Docan BMS

Add-on **Home Assistant OS / Supervised** pour les batteries **Docan Power NOON équipées d'un BMS Daren** diffusant un nom Bluetooth commençant par \`DWF\`.

> **Statut : 0.1.0-dev — première version à tester sur matériel réel.**
> La connexion à quatre batteries n'a pas encore été validée sur l'installation cible.
> Ce projet n'effectue aucune commande de configuration des BMS : uniquement des requêtes de télémétrie. Il ne remplace **jamais** les protections du BMS.

## Fonctionnalités de cette première version

- Découverte Bluetooth automatique des BMS \`DWF*\` sur le mini-PC Home Assistant.
- Plusieurs BMS, lecture séquentielle pour limiter les conflits sur l'adaptateur.
- Mesures retournées par [aiobmsble](https://github.com/patman15/aiobmsble), bibliothèque Apache-2.0 (version 0.33.0), dont les **16 tensions de cellules** quand elles sont transmises.
- Tension, courant, puissance, SOC, SOH, capacité, cycles, écarts, numéros des cellules min/max, températures individuelles, équilibrage, MOSFET, alarmes et informations disponibles selon le firmware.
- MQTT Discovery : entités créées automatiquement **uniquement lorsqu'une valeur est réellement reçue**.
- Tableau de bord en français dans Home Assistant (**Ingress**, port interne 1895).
- Synthèse multi-batteries en **parallèle** : SOC pondéré par capacité lorsque connue, somme courants et puissances, tension moyenne, suspendue si une batterie connue est hors ligne.
- Échecs Bluetooth : 3 erreurs consécutives avant passage hors ligne; reconnexion automatique au cycle suivant.

## Installation

1. **Pré-requis** : Home Assistant OS/Supervised avec adaptateur Bluetooth fonctionnel, intégration Bluetooth activée et broker Mosquitto configuré avec l'intégration MQTT.
2. Dans **Paramètres → Modules complémentaires / Applications → Boutique → ⋮ → Dépôts**, ajouter \`https://github.com/tapion69/smart-docan-bms\`.
3. Installer **Smart Docan BMS**, ouvrir la configuration et laisser \`auto_discover: true\` pour retrouver les BMS \`DWF\`.
4. Démarrer l'add-on, consulter l'onglet **Journal** puis **Ouvrir l'interface web**.
5. Les appareils/capteurs apparaîtront via **MQTT** après une première lecture réussie.

Le Supervisor installe uniquement depuis la branche principale de ce dépôt. Les branches de développement nécessitent une fusion dans \`main\` avant de pouvoir être installées par URL.

## Options

| Option | Exemple | Rôle |
| --- | --- | --- |
| \`auto_discover\` | \`true\` | Trouve automatiquement les appareils \`DWF*\` |
| \`addresses\` | \`""\` | Liste optionnelle d'adresses Bluetooth séparées par virgules ou points-virgules ; si remplie, seuls ces appareils sont interrogés |
| \`names\` | \`""\` | Alias optionnels : \`AA:BB:CC:DD:EE:FF=Batterie 1;11:22:33:44:55:66=Batterie 2\` |
| \`scan_seconds\` | \`8\` | Durée d'une recherche BLE |
| \`rescan_seconds\` | \`180\` | Intervalle entre deux recherches BLE |
| \`poll_seconds\` | \`30\` | Pause entre cycles complets de lecture |
| \`log_level\` | \`info\` | \`info\`, \`debug\` ou \`warning\` |

Les adresses personnelles de l'installation ne sont pas présentes dans le code.

## Coexistence avec BLE Battery Management (HACS)

**Important :** cette application établit ses propres connexions BLE. Pour éviter les conflits d'accès au même BMS, désactivez temporairement l'entrée des BMS \`DWF\` dans l'intégration HACS *BLE Battery Management System* pendant les tests de notre add-on. Ne supprimez pas HACS : une simple désactivation suffit et reste réversible.

## Limites et sécurité

- Les 16 valeurs de cellules sont créées **si** le BMS les fournit. Aucun capteur inventé ni tension simulée.
- La synthèse courant/puissance suppose un **câblage parallèle**. Elle n'est pas adaptée à une architecture série ou mixte.
- Les données sont indicatives ; ne les utilisez pas pour les protections critiques contre la surcharge, la sous-tension ou l'emballement thermique.
- Cette version n'a pas encore été testée sur le Bluetooth du mini-PC de l'utilisateur. Les essais unitaires n'émulent pas une connexion Daren réelle.
- Si la carte réseau Bluetooth n'est pas accessible depuis le conteneur, contrôler l'option \`host_dbus\` et les journaux. Ne pas désactiver AppArmor ou le mode protégé sans diagnostic.
- Les mesures de capacité restante ou d'énergie stockée peuvent être calculées et sont alors des **estimations**, pas des compteurs d'énergie.

## Structure

\`\`\`text
repository.yaml
smart_docan_bms/
  config.yaml        # manifest HA Supervisor
  Dockerfile
  requirements.txt
  app/
    run.py           # boucle Bluetooth et MQTT
    logic.py         # traitement des données
    dashboard.py     # interface HTML Ingress
tests/
  test_logic.py
\`\`\`

## Dépendances et attributions

Le décodage Daren est fourni **sans copie du code de ce projet** par [patman15/aiobmsble](https://github.com/patman15/aiobmsble), **Apache License 2.0**, maintenu notamment par Patrick Loschmidt et les contributeurs. Les autres dépendances sont décrites dans \`requirements.txt\`.
