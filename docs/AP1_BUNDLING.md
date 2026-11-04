# Moteur AP1 intégré

La version distribuable d'AutoPilot contient l'exécution complète de WSJT-X
Improved / AP1 sous `wsjtx-ap1`. AutoPilot démarre ce moteur réduit et ne ferme
que l'instance qu'il a lui-même créée.

Au premier lancement du moteur intégré, `WSJT-X.ini` est copié de
`%LOCALAPPDATA%\WSJT-X` vers `%LOCALAPPDATA%\WSJT-X - AP1\WSJT-X - AP1.ini`
seulement si ce dernier fichier n'existe pas. La configuration d'origine n'est
jamais modifiée ; un profil AP1 déjà présent n'est jamais remplacé.

## Construire une distribution intégrée

Le runtime AP1 ne doit pas être commité dans ce dépôt. Construire avec le
répertoire complet du runtime et l'URL exacte de ses sources GPL :

```powershell
.\packaging\build-integrated.ps1 `
  -Ap1RuntimePath 'C:\chemin\vers\WSJTX-AP1-install-stage' `
  -Ap1SourceUrl 'https://sourceforge.net/projects/wsjt-x-improved/files/WS_v3.2.0/Source%20code/'
```

La construction échoue volontairement s'il manque le runtime complet ou l'URL
des sources. Le dossier produit est le seul dossier distribuable :
`dist\WSJTX-AutoPilot`. PyInstaller place les données internes sous
`_internal`, qui contient donc aussi `wsjtx-ap1\AP1-SOURCE.txt`, la licence
AutoPilot et les notices AP1 livrées par le runtime.

Pour produire l'installateur Windows unique, compiler ensuite
`packaging\WSJTX-AutoPilot.iss` avec Inno Setup 6 en définissant
`AppSourceDir` sur ce dossier `dist`. L'installateur est produit sous
`dist\WSJTX-AutoPilot-Setup.exe` et ne supprime jamais les profils WSJT-X ou
AP1 stockés dans AppData.

