# Resumen documental de experiencia

El script local `scripts/summarize_profile_evidence.py` lee cada PDF de experiencia una
vez por SHA-256 y versión de extractor. PostgreSQL conserva el documento original;
su `metadata_json.documentary_extraction` guarda la extracción estructurada. Cada
contrato conserva sus campos originales, muestra el resumen en `descripcion` y
guarda trazabilidad en `documentary_summary`, incluida la descripción anterior.

## Ejecución

Requiere `.env.local` con Settings del worker, dependencias Python de `workers/seace`
y `pypdf`, y el túnel PostgreSQL autorizado abierto en localhost:5433.

```sh
.venv/bin/python scripts/summarize_profile_evidence.py --profile-id UUID
.venv/bin/python scripts/summarize_profile_evidence.py --profile-id UUID --generate
.venv/bin/python scripts/summarize_profile_evidence.py --profile-id UUID --apply
.venv/bin/python -m unittest discover -s scripts -p test_summarize_profile_evidence.py
```

Sin flags solo inspecciona y cuenta; `--generate` llama Gemini (modelo configurado,
concurrencia máxima 2) y guarda caché privada bajo `tmp/company-evidence/`, ignorada
por Git. Revisar esa extracción antes de usar `--apply`, que NO llama Gemini. Se
exige separar ambos pasos. Las referencias de archivos ya inexistentes se reportan
y conservan, sin inventar una lectura. Archivos no PDF abortan para revisión.

Antes de escribir, `--apply` guarda un backup privado del perfil y metadatos de
documentos en la misma carpeta (sin duplicar bytes PDF). Una única transacción
verifica fecha/JSON originales del perfil y metadatos/fecha/SHA de cada documento.
Si hubo edición concurrente, aborta todo. El hash del perfil se invalida; no se
encolan ingestas ni rematches/LLM2. No borrar estos backups hasta validar producción.

## Límites importantes

- Resumen y hechos citan páginas originales. Estado: pendiente de confirmación.
- Orden de compra, factura y conformidad no se consideran equivalentes.
- Los importes extraídos conservan moneda y concepto originales. Nunca se
  convierten/suman ni sobrescriben montos declarados del contrato o generales.
- Los rubros propuestos se etiquetan como inferencias, no hechos contractuales.
- La compatibilidad de experiencia con cada TDR y el cálculo ponderado son una
  tarea separada; este script no acredita cumplimiento legal.
- No hay scheduler nuevo: al añadir/cambiar documentos se ejecuta el script.

El nuevo JSON `documentary_summary` viaja con `experience_json` al evaluador; evita
reenviar PDF completo en cada evaluación. No implica que el scoring actual ya
calcule automáticamente experiencia económicamente admisible por especialidad.
