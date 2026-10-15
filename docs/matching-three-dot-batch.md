# Evaluación acotada DIN: tres puntos

El ejecutor opt-in `scripts/run_dinaut_three_dot_matches.py` compara las oportunidades
que el frontend clasifica con tres puntos. Usa el motor canónico de cada fuente:
afinidad por texto de PROD2 y `profile_contract_fit` de contratos menores. No usa
el panel retirado PROD4 ni inicia ingestas. Excluye lecturas inexistentes, fallidas,
vacías, obsoletas o sin requisitos sustantivos obligatorios.

La selección privada se congela mediante una consulta de solo lectura. Perfil,
líneas de negocio, fuente, extracción, requisitos y afinidad se comparan otra vez
antes de enviar y publicar. Una lectura con revisión pendiente puede evaluarse
provisionalmente; su flag permanece. Tres reemplazos históricos concretos tienen
allowlist y respaldo completo con CAS. Los demás matches no se modifican.

## Modelos y puntaje

- JEV `jev-1.13.0`: decisiones atómicas y adecuación ordinal por requisito.
- Reglas locales: pesos rubro 20, experiencia 30, experiencia económica 30 y
  profesionales 20; criterios no aplicables se reponderan. Un criterio económico
  sin datos computables sigue desconocido y el puntaje parcial se identifica.
- La comparación económica usa Decimal, alcance, moneda, período, participación,
  conformidad y deduplicación; no presume que un monto global sea experiencia
  compatible. Cercanía al mínimo se expresa como brecha numérica, sin dispensarlo.
- Gemini `gemini-3.1-flash-lite`, elegido expresamente por el usuario tras el
  rechazo de cuota de Gemma 4, mediante la credencial existente de Google: únicamente
  explica evidencia, brecha y acción; no puede devolver otro puntaje o estado.
  Se valida cobertura de requisitos y referencias existentes. Sin fallback.

JEV recibe los resúmenes existentes de todos los documentos, datos esenciales y
advertencias. El explicador comparte los 43 resúmenes completos una vez por lote
de hasta cinco oportunidades, con matriz de campos, defaults explícitos, montos,
fechas y flags de revisión. Omite prosa repetida de advertencias; mantiene sus
contadores y la incertidumbre. No usa este contexto reducido para recalcular JEV. Los hechos y citas completos quedan disponibles localmente para
aritmética y trazabilidad. `shared_defaults` factoriza repeticiones con expansión
exacta comprobada; no corta fuentes ni requisitos. No se releen PDF ni se crean
resúmenes con otra inferencia. RUC/RNP/CCI/formularios quedan en un checklist sin
impactar el puntaje. La cobertura no acredita equivalencia semántica ni aptitud
legal. Los resultados nuevos se publican como provisionales, nunca aprobados.

## Operación y recibos

Sin `--execute`, el script solo prepara desde archivos locales:

```sh
.venv/bin/python scripts/run_dinaut_three_dot_matches.py --folder tmp/dinaut-three-dot-20261015
```

`--execute` habilita la operación productiva expresamente solicitada. `--limit 1`
permite comprobar primero una oportunidad del lote real; después la misma carpeta
reutiliza ese resultado sin repetir proveedores. No copiar la carpeta para reintentar.

Hay un coordinador por perfil, reservas PROD2 antes del envío y un ledger PG por
etapa para ambas fuentes. Cada petición/respuesta se conserva en archivos privados
0600 antes de continuar. Un envío sin respuesta o una respuesta inválida/rechazada
detiene nuevas llamadas; no se reintenta ni se sustituye el modelo automáticamente.
El ledger PG bloquea repagos desde otra carpeta sin sus recibos. Una respuesta
guardada puede conciliarse localmente sin volver a inferir.

PROD2 conserva el nombre legacy `gemini_match` de su reserva para compatibilidad
del lector web, con proveedores/modelos reales explícitos en metadata. Publica
match, provenance y extracción exacta. Menores usa `matches` y `match_events`.
No modifica notas/estado del usuario ni envía notificaciones. No hay migraciones,
reinicios de workers, cambios del modelo predeterminado o despliegue web.

La ejecución seleccionada utiliza:

```sh
.venv/bin/python scripts/run_dinaut_three_dot_gemma_batches.py --execute --recommendation-model gemini-3.1-flash-lite
```

El nombre legacy del archivo/etapa `gemma` no cambia el modelo real registrado.
La transición conserva el rechazo Gemma HTTP429 por cuota 16000 tokens/minuto;
cierra solamente su composición fallida y reutiliza el JEV200 exacto. No infiere
otra vez ni considera la cuota de Gemma como cuota de Flash-Lite. Cuenta tokens
antes de enviar; el máximo local Flash-Lite de 30000 es un límite operativo de
entrada, no una cuota verificada de la cuenta. Sin fallback o reintento automático.

El costo se calcula desde usage real único de JEV (USD0.042/M entrada) y Flash-Lite
(USD0.25/M entrada, USD1.50/M salida, incluido thinking), según tarifa pública
consultada el 15/10/2026. El reparto por caso de un batch es informativo: no se
suma otra vez el usage completo compartido ni el JEV reutilizado de la cadena
anterior. Se guardan uso y estimación sin afirmar factura verificada.

## Diagnóstico de tamaño JEV

El segundo caso recibió HTTP400 `max_tokens_exceeded`; el proxy local había
subestimado la representación del proveedor. El rechazo exacto y su reserva2144
se conservan, sin inventar uso/costo cero. `dinaut_jev_transport.py` envía el mismo
`state` como cadena JSON compacta: expansión idéntica, mismas preguntas/modelo,
sin truncar documentos, cambiar manifiestos congelados o usar contexto extendido.
Los éxitos anteriores se reutilizan. Solo un rechazo exacto de validación permite
esta representación alternativa única; cualquier envío sin respuesta u otro
rechazo bloquea. Cada alternativa tiene manifiesto con SHA semántico y de wire.
La recuperación PG acotada a2144 exige propiedad y CAS completos. Una equivalencia
de serialización no acredita QA semántica ni un conteo del tokenizer del proveedor.
Referencia: https://docs.typesafe.ai/models y https://docs.typesafe.ai/api.

## Cobertura del explicador

Flash-Lite recibe ahora `responseJsonSchema` con casos y requisitos en posiciones
exactas (`prefixItems`, `minItems/maxItems`, enum de IDs), acciones como lista y
aliases documentales permitidos. La validación local sigue siendo obligatoria.
Un HTTP200 previo con cuatro casos omitió34 requisitos; no fue publicado. Su
raw, uso y costo conocido USD0.0092455 se conservan fuera de las publicaciones.
Un diagnóstico local explícito ligado a sus SHA permite sustituir solo esa salida
con el esquema corregido. No habilita reintentos ante envíos inciertos, fallos de
acceso, cuotas o errores sin diagnóstico. Los 33 JEV completos se reutilizan.

La variante `responseJsonSchema` con tuplas recibió HTTP400 INVALID_ARGUMENT sin
uso ni respuesta generada; el proveedor no indicó qué característica rechazó.
Se conserva ese recibo y diagnóstico explícito, sin atribuir costo cero. El formato
activo usa `responseSchema` establecido en la aplicación: objetos requeridos por
case_key y requisitos rN, convertidos sin alterar texto a arrays canónicos tras
recibirlos. Máximo dos casos y16 requisitos combinados por petición (un caso con
más requisitos se conserva completo), sin cambiar modelo ni JEV. La validación
local exige de nuevo todas las identidades y aliases; no publica omisiones.

Las referencias profesionales Pn presentes en el perfil congelado se conservan
como `profile_evidence_refs` con `source_path` y SHA del snapshot. No se convierten
en referencias D ni CV acreditados. El guard rechaza cualquier alias que no exista
en esos dos namespaces; una cita profesional legítima puede conciliarse desde la
respuesta ya guardada, sin pagar otra generación ni modificar el puntaje.
