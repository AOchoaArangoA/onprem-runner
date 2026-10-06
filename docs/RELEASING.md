# Publicar una versión

Configuración única (Trusted Publishing, sin tokens):

1. En PyPI: *Your projects → Publishing → Add a pending publisher* con
   proyecto `onprem-runner`, owner/repo de GitHub, workflow `publish.yml`,
   environment `pypi`.
2. En GitHub: *Settings → Environments → New environment* llamado `pypi`
   (opcionalmente con aprobación manual).

Cada versión:

1. Subir `__version__` en `src/onprem_runner/__init__.py` y mergear a `main`.
2. Crear un Release en GitHub con el tag `vX.Y.Z` (debe coincidir con `__version__`;
   el workflow falla si no).
3. `publish.yml` construye y publica en PyPI automáticamente.
