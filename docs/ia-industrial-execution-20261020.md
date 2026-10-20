# Ejecución DIN IA/industrial — 20/10/2026

El lote autorizado está **en ejecución**, con dos lectores/modelslots y un único coordinador PostgreSQL. Corte fresco **16:18:39UTC**: nueve resultados de157IDs,146queued/2pending. Hay trescerrados, doslecturas históricas preservadas, dosrevisiones documentales tipadas, unrechazo JEV<60 y unaapertura no confirmada. **No hay lecturas LLM1 nuevas ni matching nuevo terminado en este corte.** Los contextos de1047330/1048448 necesitan ampliación o revisión; sus entradas y recibos se conservan, sin reenvío pagado automático.

Costo local conocido parcial: **US$0,002236876**, siete intentos conocidos (cuatro JEV originales y tres embeddings nuevos,97.085tokens), cero pagos inciertos nuevos. No se repitieron los cuatro pagos originales. Los seis inciertos históricos de lotes anteriores siguen aparte y desconocidos. Fuente completa, piloto iniciado o recibos conocidos no equivalen al cierre de las lecturas.

## Autoridad de ejecución

Runtime **7dccbc6ec92543455cf856054d8b00a2f7ce327b**, commit/push en `codex/prod2-parallel-discovery-prep`. CLI enMac conPG/Redis productivos por túnel; sin imagen nueva ni despliegue web. Supervisor85202/lector88477 al corte; se valida identidad actual, no permanencia de PIDs al terminar.

- Plan actual: `tmp/ia-industrial-execution-20261020/chain-plan.runtime-recovery-002.private.json`, SHA`25922f5da9f518064543ca875db33b541e6fe778606a0606175a53793a51c061`.
- Supervisor actual: `tmp/ia-industrial-execution-20261020/chain-runtime-recovery-002/`.
- Misma carpeta del lector: `tmp/ia-industrial-execution-20261020/runs/prod2-din-ia-industrial-20261020/`.
- Matching previsto: `tmp/dinaut-ia-industrial-20261020/batch-01/`.

Son157candidatos del mapeo cerrado (154industriales y tres deIA/automatización;128IDs nuevos y29conocidos sin baseline comparable). Se conservan48límites de rubro por título fuera del runtime y1.531exclusiones de título, sin llamarlas rechazosJEV ni censo semántico. No se repite el mapeo. PROD2 Mac pasó acceso nativo; una comprobación dePROD6 a14:41UTC devolvió403. PROD6 continúa pendiente de acceso verificable, sin nuevas probes, bypass ni activación.

## Flujo y límites

Se preservan históricos antes de fuente/JEV y se confirma apertura antes de documentos/modelos. Solo los abiertos conJEV>=60 avanzan a lecturaV5/OCRlocalparcial y clasificación comercial en la misma lectura. Un contexto insuficiente queda en revisión tipada sin fallbackPDFcompleto/Luna. PG conserva UUID, hashes, resultados, fichas, cronogramas y referencias oficiales. Los documentos son temporales de análisis; no se crean originales nuevos enR2.

Dos consumidores, dos slots de modelos y pacing global de fuente2s. Piloto de10IDs/10unidades documentales, ampliación acumulativa tras readbackPG/Redis fresco y hasta24etapas finitas. Contenedor y miembros de archivo cuentan como unidades distintas. Los topes de llamadas son finitos, sin `--all-results`; la guía deUS$1 para piloto no es un tope monetario implementado. No se compran créditos ni se cambian límites.

La reservaLLM1 comercial conjunta heredada bajoVEYON mantiene su identidad; no prueba capacidadDIN. El matching es soloDIN: JEV/reglas para comparar rubro, experiencia, monto y profesionales, y Gemini3.1Flash-Lite para recomendaciones. Requiere lectura lista, apertura confirmada<=6h y fechaPG futura idéntica; new-only, sin restricción fit2/fit3, sin sobrescribir evaluaciones históricas ni notificaciones. RUC/RNP y trámites no bajan puntaje. `ingestion_enabled=false` se conserva porque este es un lote manual.

## Recuperaciones verificadas

El primer arranque se detuvo antes de manifest/fuente/modelos por0042ausente. Solo se aplicó su CHECK7z/registro, conservando4.891filas de descargas;0043embedding se verificó con conexiónIDLE.

A15:28 se detuvo el lector por adaptador de embeddings sin tres propiedades requeridas por el store. Fix y recuperación001 conservaron la identidad pagada y reencolaron solo1048448. El nuevo intento se detuvo a15:56: la metadata congelada contenía el timestamp de verificación, que cambiaba en cada reentrada aunque el documento fuese igual. Ninguno de esos fallos generó llamadas nuevas a embeddings/LLM1.

El código actual separa metadata documental inmutable de observaciones lifecycle append-only; conserva sus bytes antiguos y sigue rechazando cambios fuente/documento/schedule/detailhash. Ante error local inesperado detiene nuevos envíos con recibo sanitizado, conserva payload sin ACK/outcome y respeta respuestas conocidas en vuelo. Reviews documentales tipadas y límites esperados siguen porcaso.

Recuperación002 aplicada a16:10 y relanzada16:11: CAS exacto de1048448/1047330/1049583, cola148→151, PG0antes,291inputs/146.154.045bytes y12referencias de historia001 verificados. Treserrores archivados conSHA; seisotros resultados, cuatroJEV y207checkpointsOCR preservados. Planes/manifiestos/reportes/estados anteriores permanecen intactos. Autorización002 SHA`ffc37a8bc3e7a5c0dad23ea43ebde3fb0a1fec2b20245aa610927ba2c4463c74`;204bindings runtime nuevos se registran separados de153bindings originales usados únicamente como identidad de inputs pagados. No es una política genérica de reintentos.

## Verificación pendiente y seguimiento

Principal: **4.327 pruebas offline aprobadas/67omitidas**, revisión independiente y replay de tresfichas reales con copiasJSON. El progreso real ya confirma metadata fresca, tresenvíos embedding conocidos y192vectoresdone enPG. No acredita QAsemántica ni cierre del lote.

El supervisor continúa hasta la partición exacta de157IDs, colas/reservas propias drenadas y matching de lecturas listas. El cierre verificará nuevos/preservados/rechazados/reviews, fichas/cronogramas/referencias, costos conocidos/inciertos y limpieza segura, manteniendo pendientes reales. Colas antiguas no se adoptan ni limpian.

Seguimiento `completar-lote-din-ia-e-industrial` activo cada15min, apuntando alplan002: silencio en avance sano, aviso solo por cambio material/fallo/intervención/cierre. El seguimiento antiguo del mapeo permanece pausado. Helpers privados aceptan `--plan` y `--chain-folder`; auditor finalPGREADONLY se ejecutará solo conchain.complete y no aprueba semántica.

Limpieza propia verificada: **318.473.818bytes lógicos**, principalmente basetemps sintéticos cerrados. Logs y recibos compactos permanecen. PDFs/OCR/entradas y respuestas de proveedores activas o pendientes se conservan para evitar repetir trabajo/pagos. Datos, hashes y cortes: `docs/ia-industrial-execution-20261020.json`; evidencia privada: `tmp/ia-industrial-execution-20261020/activation/`.
