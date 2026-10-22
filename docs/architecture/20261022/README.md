# Diagramas de flujo de BuenaPro

Dos láminas PNG para explicar el recorrido de lectura y evaluación de DIN, creadas el 22/10/2026 con la herramienta integrada `image_gen`. La versión final sustituye las escenas ilustradas por cajas, flechas verticales y dos bloques claramente separados.

| Diagrama final | Formato |
|---|---|
| [SEACE · PROD2](seace-prod2-flujo.png) | PNG, 1024 × 1536 |
| [Contratos menores · PROD6](menores-prod6-flujo.png) | PNG, 1024 × 1536 |

## Cómo explicarlos

Leer de arriba hacia abajo. El bloque A descubre y lee documentos; PostgreSQL conserva la lectura estructurada. La ficha puede mostrarse en la web antes de evaluar a una empresa. El bloque B se ejecuta por separado y combina requisitos utilizables con el perfil DIN.

**LLM1 estructura → JEV y reglas puntúan → LLM2 explica.** El resultado es provisional y se guarda con su evidencia. RUC/RNP y formatos rutinarios no reducen ese puntaje de capacidad.

En PROD2 se representa la modalidad seleccionada por IDs: identidad y apertura antes de nuevos modelos, filtro comercial JEV≥60, preparación documental y contexto V5, LLM1 y almacenamiento. El mapeador paralelo de metadatos es otra herramienta; no implica lectura automática. Redis mantiene el trabajo pendiente.

En menores, el descubrimiento usa CUBSO y palabras, sin filtro JEV en esa etapa. Su cola vive en PostgreSQL; la ruta documental representada admite PDF TDR/EETT de categoría1. No representa conversión automática de Word/ZIP/RAR ni lectura de todos los anexos.

Las flechas representan el flujo lógico, no el estado actual de ejecución. No se activaron lectores ni se hicieron consultas SEACE, PostgreSQL, Redis o modelos de negocio para crear estas imágenes. La referencia técnica es la [guía de lectores](../../guia-lectores-seace-menores.md); el contexto de continuidad está en [CONTEXTO.md](../../../CONTEXTO.md).

## Prompts y verificación

Los [prompts finales completos](prompts.json) registran el modo integrado y las instrucciones exactas de generación de cada diagrama. Se revisaron visualmente orden, conectores, separación de procesos, fuentes de entrada y función de cada modelo; dos revisiones independientes contrastaron las láminas con la guía. Son esquemas explicativos, no evidencia de ejecución ni QA semántica de licitaciones.

| Archivo | SHA-256 |
|---|---|
| `seace-prod2-flujo.png` | `56ceef7515be6d03161442df35a9baef47b7d7c9b8103cd33f104d45444caaad` |
| `menores-prod6-flujo.png` | `8fda5f8b71710b6716ce9a0ae65e5c4d4c5283e89f8b829c364e58b6c2ab7876` |

Se conservan los dos finales en el proyecto y las generaciones originales en el directorio administrado por Codex. No se crearon descargas documentales, conversiones ni temporales de ingesta; **0 bytes liberados**. Las entradas pendientes y sus recibos permanecen intactos.
