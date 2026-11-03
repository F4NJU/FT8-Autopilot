# WSJTX AutoPilot

WSJTX AutoPilot est un assistant Windows pour les QSO FT8/FT4 avec WSJT-X.
Il choisit une station, répond aux appels directs, suit le déroulement du QSO,
recherche des stations rares et peut changer automatiquement de cycle TX,
canal FT8 ou bande lorsque la bande actuelle n'offre plus de contact
intéressant.

L'opérateur reste responsable de la station, des émissions et du respect de
la réglementation.

**Statut : version stable 1.0.0.**

Télécharger l'archive prête à utiliser depuis la
[dernière release](https://github.com/F4NJU/FT8-Autopilot/releases/latest),
l'extraire puis lancer `WSJTX-AutoPilot.exe`.

## Installation

Après extraction de l'archive, l'exécutable est :

```text
dist\WSJTX-AutoPilot\WSJTX-AutoPilot.exe
```

AutoPilot doit être lancé en même temps que la version AP1 de WSJT-X.

L’onglet **Hunt** nécessite également une clé API gratuite **afu.tools**.
La clé s’obtient sur [afu.tools/api](https://afu.tools/api), puis se renseigne
dans **Preferences > Diagnostic > Clé activité DX/FT8 (afu.tools)**.

Pour le filtre **DXCC non validés LoTW uniquement**, renseigner également
l'URL de l'API v2 Wavelog et un jeton `wl2_` ayant l'autorisation
`confirmation:read` dans **Preferences > Diagnostic**. L'AutoPilot ne lit que
les confirmations LoTW ; il ne modifie jamais le carnet Wavelog.

## Configuration obligatoire dans WSJT-X

Dans les paramètres de WSJT-X :

1. Dans **Reporting**, régler le serveur UDP sur `127.0.0.1` et le port sur
   `2237`.
2. Toujours dans **Reporting**, cocher **Accept UDP requests**.
3. Dans **General**, cocher **Double-click on call sets Tx enable**.

Ces deux cases sont indispensables. Si elles ne sont pas cochées, AutoPilot
peut recevoir et afficher les décodages, mais WSJT-X peut refuser la réponse ou
ne pas activer l'émission. C'était notamment la cause du problème où une station
était sélectionnée sans qu'aucune transmission ne démarre.

Le port UDP configuré dans AutoPilot doit être identique à celui de WSJT-X.

## Première utilisation

1. Lancer WSJT-X et vérifier que la réception FT8 fonctionne.
2. Lancer `WSJTX-AutoPilot.exe`.
3. Ouvrir **Preferences** et renseigner l'indicatif, le port UDP, la radio et
   les bandes autorisées. Pour utiliser **Hunt**, renseigner aussi la clé API
   **afu.tools** dans l’onglet **Diagnostic**.
4. Vérifier que **WSJT-X AP1: Available** apparaît.
5. Cliquer sur **ARM** pour autoriser AutoPilot à répondre.

AutoPilot démarre désarmé. Il ne transmet rien tant que **ARM** n'a pas été
activé.

## Services optionnels

| Service | Utilité | Réglage |
| --- | --- | --- |
| Hamlib | fréquence radio, ALC, SWR et sécurité d'antenne | **Preferences > Radio / Hamlib** |
| afu.tools | liste et activité FT8 des indicatifs spéciaux pour Hunt | clé API dans **Diagnostic** |
| PSK Reporter MQTT | suivi des rares et DXCC LoTW recherchés sur les bandes autorisées | **OPTIONS REPLY** |
| Wavelog API v2 | priorités DXCC confirmés/non confirmés par LoTW | URL + jeton `wl2_` dans **Diagnostic** |
| NOAA | données SFI / Kp pour le départage de chemin favorable | automatique quand l'option est activée |
| Discord | présence d'activité FT8 | **Diagnostic** |

Les services Internet restent facultatifs : le fonctionnement Reply classique
repose sur les décodages locaux de WSJT-X.

## Fonctions principales

- interface organisée en trois onglets : **Reply**, **Hunt** et **CQ — À venir** ;
- sélection automatique des CQ et des stations qui appellent directement ;
- priorité aux appels directs sans interrompre un QSO déjà engagé ;
- suivi du QSO jusqu'au message final ;
- filtrage des doublons, blacklist et priorités DXCC/continent ;
- filtre optionnel **DXCC non validés LoTW uniquement**, basé sur les
  confirmations LoTW visibles dans Wavelog ;
- option pour prioriser en mode **Reply** les indicatifs spéciaux FT8 actifs vérifiés dans **Hunt** ;
- option **Priorité chemin favorable (SFI / Kp)** dans **OPTIONS REPLY** :
  départage les CQ ordinaires selon la distance, l'heure, la bande, le SFI et
  le Kp ; elle ne bloque aucun CQ et ne passe jamais devant un appel direct ;
- option **Exclure les stations déjà contactées** dans **OPTIONS REPLY**, sur
  l'historique ADIF/WSJT-X, même un autre jour ou sur une autre bande ;
- changement automatique de cycle TX puis de bande quand les contacts viables
  sont épuisés ;
- changement de bande forcé après une durée maximale réglable, fixée à
  **30 minutes par défaut**, même si les autres critères de rotation ne sont
  pas encore atteints ;
- lecture et commande de la radio par Hamlib ;
- régulation ALC et protection SWR lorsqu'elles sont compatibles avec la radio ;
- affichage Discord, par exemple `FT8 - 40m` et `Working EA7CTA`.

### Priorités Reply

Les appels directs et un QSO déjà en cours restent protégés. Pour les autres
stations, AutoPilot départage selon les préférences DXCC/continent, les
politiques POTA/SOTA/QRP, les doublons, le signal et les options choisies dans
**OPTIONS REPLY** : stations rares, DXCC LoTW non validés, chemin favorable
et exclusion de l'historique.

Le suivi MQTT PSK Reporter sert uniquement à repérer une station rare ou un
DXCC recherché sur une bande autorisée. Il peut faire déplacer le récepteur,
mais ne peut jamais déclencher une émission sans Decode local de WSJT-X.

### Rotation des bandes et canaux

La rotation adaptative peut d'abord essayer l'autre cycle TX, puis changer de
position d'écoute. Pour chaque bande autorisée, elle parcourt le canal FT8
principal puis le canal FT8 secondaire/DX, avant de passer à la bande suivante.
Une rotation temporisée force aussi le passage à une autre bande après la durée
maximale configurée (30 minutes par défaut).

Un changement est effectué seulement en réception, hors QSO, sans appel direct
en attente et jamais vers une bande exclue par la sécurité SWR. Un changement
de bande normal ne lance pas d'accord automatique.

### Onglet Hunt

L’onglet **Hunt** affiche les indicatifs spéciaux annoncés dont une activité
FT8 récente a aussi été observée sur les clusters, le réseau RBN numérique ou
PSK Reporter via afu.tools. Les stations sans preuve d’activité FT8 ne sont pas
proposées. La liste peut être limitée aux
indicatifs français et elle est triée selon la fraîcheur, la bande, les heures
d’activité et une estimation simple de la probabilité de chemin radio.

Le service actuel utilise une clé API gratuite **afu.tools**, à renseigner dans
**Preferences > Diagnostic**. En cas d’indisponibilité du service, le dernier
cache connu est affiché clairement comme tel.

Un double-clic surveille une seule station ; **HUNT TOUS** parcourt la liste.
AutoPilot reste limité aux bandes autorisées et peut passer sur le canal FT8
principal ou secondaire de la bande ciblée. Un spot Internet ne suffit jamais
à lui seul : il attend obligatoirement un vrai Decode local de WSJT-X avant
toute émission. Les appels directs reçus et les QSO déjà engagés restent
prioritaires.

La durée maximale sur une bande se règle dans les préférences. La valeur `0`
désactive uniquement cette limite de temps. Un QSO déjà engagé reste
prioritaire : le changement attend sa fin. Les bandes exclues par la protection
SWR sont ignorées et un changement de bande normal ne lance pas d'accord
automatique.

La protection SWR coupe immédiatement l'émission. Une fois le retour en RX
confirmé, AutoPilot peut tenter un accord par Hamlib et vérifie l'état réel du
tuner ainsi que plusieurs mesures SWR. Si l'accord réussit, le trafic reprend
après un cycle de sécurité. Si le SWR reste trop élevé, la bande est exclue
pour la session et une autre bande autorisée est sélectionnée sans relancer
d'accord. Une commande non confirmée ou des mesures insuffisantes provoquent
un arrêt de sécurité sans exclure la bande.

Le voyant d'antenne résume l'état : vert (normal), jaune (incident corrigé) ou
rouge (arrêt). **RÉINITIALISER SWR** efface explicitement l'état de sécurité
et les exclusions de session, puis réarme sans lancer un nouvel essai d'accord.

## Fonctions retirées ou volontairement non automatisées

- **Smart TX** et la planification automatique du décalage TX ont été retirés ;
- l'apprentissage automatique de drive a été retiré ;
- AutoPilot ne modifie plus l'**USB MOD gain** ;
- un changement de bande normal ne lance jamais d'accord automatique :
  l'accord Hamlib est réservé à la récupération après un incident SWR ;
- un spot Internet (Hunt ou MQTT) ne déclenche jamais seul une émission : un
  Decode local WSJT-X reste obligatoire.

Ces limites sont volontaires : elles réduisent les actions radio non
vérifiables et gardent le comportement prévisible.

## En cas de problème

Si AutoPilot voit les stations mais ne répond pas :

- vérifier **Accept UDP requests** ;
- vérifier **Double-click on call sets Tx enable** ;
- vérifier que WSJT-X et AutoPilot utilisent bien le même port UDP (`2237` par
  défaut) ;
- vérifier que **WSJT-X AP1: Available** est affiché ;
- vérifier qu'AutoPilot est armé ;
- vérifier qu'aucune alerte SWR ou erreur CAT/Hamlib n'est active.

Les journaux sont enregistrés dans :

```text
%LOCALAPPDATA%\FT8-AutoPilot\logs
```

Les réglages sont enregistrés dans :

```text
%APPDATA%\WSJTX-AutoPilot\settings.json
```

## Développement

```powershell
python -m pip install -e ".[gui,test,build]"
python -m pytest -q
pyinstaller -y packaging/wsjtx-autopilot.spec
```

## Licence

GPL-3.0-or-later

