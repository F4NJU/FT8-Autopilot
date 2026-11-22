# Checklist de préparation de release

Cette procédure prépare les fichiers d'une release sans la publier. Aucune
release GitHub n'est créée tant que l'opérateur n'a pas donné une validation
explicite.

## 1. État du dépôt

- [ ] La branche `main` est à jour et la CI est verte.
- [ ] Aucun changement fonctionnel non validé n'est en attente.
- [ ] La version lisible dans `src/wsjtx_autopilot/__init__.py` est la bonne.
      C'est la seule source de vérité : ne pas modifier `pyproject.toml` ni le
      script Inno Setup pour changer le numéro.
- [ ] Si la version doit changer, le faire dans `__init__.py`, puis vérifier
      avec `py -3.14 tools\release.py check`.

## 2. Contrôles locaux

Depuis la racine du dépôt :

```powershell
py -3.14 -m ruff check .
py -3.14 -m compileall -q src tests
py -3.14 -m pytest -q
py -3.14 tools\release.py check
py -3.14 tools\release.py version
```

La suite doit rester verte ; l'unique avertissement `pytest_asyncio` connu est
toléré.

## 3. Construction des fichiers

Fournir le runtime AP1 intégré et l'URL de sa source, puis lancer :

```powershell
.\packaging\release.ps1 -Ap1RuntimePath <chemin> -Ap1SourceUrl <url>
```

Le script effectue, dans cet ordre :

1. lecture et contrôle de la version source ;
2. construction du bundle PyInstaller intégré ;
3. génération de l'installateur avec Inno Setup ;
4. génération puis vérification de `SHA256SUMS.txt` avec le parseur utilisé par
   le client de mise à jour ;
5. validation finale avec `tools\release.py check-artifact`.

Les deux fichiers à joindre à une release sont :

- `dist\WSJTX-AutoPilot-Setup.exe`
- `dist\SHA256SUMS.txt`

Ils doivent être publiés ensemble. `SHA256SUMS.txt` doit contenir exactement
une ligne, au format `<sha256> *WSJTX-AutoPilot-Setup.exe`.

## 4. Points d'attention

- L'extension ATU-100 (`SetTune` / `TuneState`) reste expérimentale et
  désactivée par défaut. Le runtime AP1 embarqué dans la v1.0.4 ne contient pas
  ces messages : ne pas annoncer cette fonctionnalité dans les notes de release.
- Le patch AP1 local reste en attente de reconstruction et de validation ; ne
  pas le présenter comme livré.
- La CI ne fait que valider le « plumbing » de release : artifact PyInstaller,
  version source unique, somme de contrôle et détection d'une somme corrompue.
  Elle ne publie aucune release.

## 5. Publication

La publication GitHub demandée séparément doit joindre uniquement les deux
fichiers ci-dessus et des notes reprenant les changements réellement validés.
