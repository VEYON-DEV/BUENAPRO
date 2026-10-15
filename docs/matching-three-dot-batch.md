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
- Gemma `gemma-4-31b-it` mediante la credencial existente de Google: únicamente
  explica evidencia, brecha y acción; no puede devolver otro puntaje o estado.
  Se valida cobertura de requisitos y referencias existentes. Sin fallback.

Se envían los resúmenes existentes de todos los documentos, datos esenciales y
advertencias. Los hechos y citas completos quedan disponibles localmente para
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

El costo se calcula desde usage real de JEV. Gemma por Google tiene tarifa pública
gratuita, sujeta al acceso y límites de esa cuenta; eso no prueba saldo ni acceso
efectivo hasta recibir la respuesta. Se guarda el uso sin afirmar factura verificada.
