# WSJTX AutoPilot

WSJTX AutoPilot est un assistant Windows pour les QSO FT8/FT4 avec WSJT-X.
Il choisit une station, répond aux appels directs, suit le déroulement du QSO
et peut changer automatiquement de cycle TX ou de bande lorsque la bande
actuelle n'offre plus de contact intéressant.

L'opérateur reste responsable de la station, des émissions et du respect de
la réglementation.

**Statut : `v0.1.0-wip.1` — première préversion officielle, encore en cours
de validation sur le terrain.**

## Installation

La version prête à utiliser se trouve ici :

```text
dist\WSJTX-AutoPilot\WSJTX-AutoPilot.exe
```

AutoPilot doit être lancé en même temps que la version AP1 de WSJT-X.

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
   les bandes autorisées.
4. Vérifier que **WSJT-X AP1: Available** apparaît.
5. Cliquer sur **ARM** pour autoriser AutoPilot à répondre.

AutoPilot démarre désarmé. Il ne transmet rien tant que **ARM** n'a pas été
activé.

## Fonctions principales

- sélection automatique des CQ et des stations qui appellent directement ;
- priorité aux appels directs sans interrompre un QSO déjà engagé ;
- suivi du QSO jusqu'au message final ;
- filtrage des doublons, blacklist et priorités DXCC/continent ;
- changement automatique de cycle TX puis de bande quand les contacts viables
  sont épuisés ;
- lecture et commande de la radio par Hamlib ;
- régulation ALC et protection SWR lorsqu'elles sont compatibles avec la radio ;
- affichage Discord, par exemple `FT8 - 40m` et `Working EA7CTA`.

La protection SWR coupe immédiatement l'émission et désarme AutoPilot. La
tentative d'accord automatique et l'exclusion automatique d'une bande sont
actuellement désactivées.

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
