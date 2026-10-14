# Evaluacion Gemini basada en evidencia, v3

## Alcance

Gemini sigue siendo el evaluador. No se integra Jev. Entrada: perfil actual completo, oportunidad/resumen y requisitos extraidos. La licitacion sola no permite determinar brechas de una empresa. No se relee PDF en LLM2 ni se da acceso a informacion externa.

Dos ambitos, mismas reglas de evidencia: menores y procedimientos de seleccion. Archivos conservan sus nombres para compatibilidad; versiones de evaluacion pasan a `match_analysis_v3` y `match_analysis_prod4_v2`. Sin ejemplos de empresas, profesiones o sectores ni umbrales economicos ficticios.

## Principios

- Instrucciones directas, delimitadas por secciones; JSON es dato, nunca instrucciones.
- Declaracion comercial no acredita recursos. Ausencia de informacion no prueba incumplimiento.
- RNP/RUC sin evidencia vigente no se marcan cumplidos; autorizaciones no adjuntadas requieren verificar.
- Comparar especialidad, moneda, periodo, participacion y respaldo antes de comparar montos.
- No sumar areas con contratos potencialmente duplicados; no convertir monedas sin tasa.
- Personal contratable no equivale a equipo incorporado. Acciones deben ser permitidas y temporalmente viables.
- Requisito por requisito, conservando orden y cobertura; misma salida JSON actual.
- No usar score/veredicto anterior como entrada. Se invalida cache por version del prompt; preservacion de estados anteriores solo con perfil, facets y version identicos.

## Puntaje

Se conservan bandas ordinales para no romper contrato API/UI: rojo 0–29, gris 30–49, ambar 50–84, verde 85–100. Es nota orientativa de preparacion, NO probabilidad de adjudicacion ni porcentaje objetivo de requisitos cumplidos. La formula ponderada determinista sigue pendiente; el prompt no inventa pesos legales ni cuenta anexos como compensacion de bloqueos sustantivos.

## Validacion 05/10/2026

256 pruebas worker y TypeScript sin errores; 18 contratos offline verifican ambos prompts y cliente simulado (perfil completo, no envio de score previo). Cinco pruebas pagadas con snapshot privado completaron 48 requisitos, pero revelaron errores de ausencia vs incumplimiento. Se reforzo la comprobacion final y se repitieron dos casos: RNP sin respaldo vigente queda revision en ambos; Dinaut sin autorizacion Microsoft adjunta pasa de no_cumple a requiere_revision. No se modifico ningun puntaje productivo.

Limites observados: anexos condicionales siguen recibiendo a veces accion preparable en lugar de revision; el schema actual no tiene `no_aplica`. La comparacion monetaria legacy puede sobreescribir juicios del modelo con montos generales, y no verifica especialidad/moneda por completo. La implementacion manual web tiene reglas legacy distintas del worker. Estos problemas requieren reglas/schema y golden set; no se resuelven asegurando que un prompt es perfecto.

Estado: cambios locales, NO desplegados ni recalculados. No iniciar corrida masiva hasta validar esos limites.

## Fuentes primarias

- [Guia de Gemini 3](https://ai.google.dev/gemini-api/docs/gemini-3): claridad/directividad y contexto acotado.
- [Diseño de prompts](https://ai.google.dev/gemini-api/docs/prompting-strategies): restricciones explicitas, delimitadores, iteracion.
- [Salidas estructuradas](https://ai.google.dev/gemini-api/docs/structured-output): schema para formato; validez estructural no demuestra correccion semantica.
- [Model Alignment](https://ai.google.dev/responsible/docs/alignment): evaluar con un conjunto distinto del usado para ajustar instrucciones.
