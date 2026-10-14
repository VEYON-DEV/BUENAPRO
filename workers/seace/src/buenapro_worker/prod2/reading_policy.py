"""Explicit output organization; never remove source pages or schema fields."""

COMPACT_UNIQUE_REQUIREMENTS_POLICY = "compact_unique_requirements_v1"

COMPACT_UNIQUE_REQUIREMENTS_INSTRUCTION = (
    "\nPOLITICA_DE_LECTURA=compact_unique_requirements_v1. Lee el PDF COMPLETO y todos "
    "sus rangos; conserva el mismo schema JSON y todos sus campos. Organiza la salida "
    "por cada requisito unico aplicable, con redaccion concisa y evidencia literal "
    "breve suficiente para verificarlo y pagina GLOBAL. No transcribas parrafos "
    "enteros, indices ni formularios vacios; no repitas el mismo requisito por "
    "aparecer en varias paginas o copias equivalentes. Deduplica solo repeticiones "
    "materialmente identicas de la misma exigencia y alcance. No fusiones requisitos "
    "con distintos items, perfiles, unidades, montos, condiciones o documentos de "
    "acreditacion; conserva sus diferencias y referencias. No hay limite de cantidad "
    "de requisitos: no omitas exigencias para abreviar ni devolver solo las principales. "
    "Conserva TODOS los datos materiales documentados: montos y moneda, cantidades y "
    "unidades, porcentajes, formulas, plazos y fechas, condiciones de aplicabilidad "
    "y acreditacion, umbrales de experiencia, perfiles y titulaciones, certificaciones, "
    "licencias, equipos, seguros, entregables, especificaciones, garantias, pagos y "
    "penalidades con topes, condiciones y excepciones. Manten separados requisitos "
    "del postor, obligaciones de ejecucion y bienes por entregar. Usa el campo "
    "correspondiente del schema; sintetizar nunca autoriza a eliminar un dato material. "
    "Si un formulario contiene una exigencia especifica aplicable, conservala; omite "
    "solo espacios por llenar y texto generico que no establezca una exigencia real. "
    "No resuelvas diferencias entre versiones ni contradicciones mediante deduplicacion: "
    "preserva condiciones materiales y registra la incertidumbre para revision. "
    "Conserva participation.consorcio y participation.subcontratacion con su status, "
    "cita literal aplicable, pagina GLOBAL y TODAS sus condiciones; no clasifiques "
    "permisos o prohibiciones a partir de formularios, encabezados o ausencia de clausula. "
    "Una evidencia breve no debe cortar negaciones, porcentajes, excepciones o condiciones "
    "que cambien el sentido. No inventes datos ni conviertas datos de ofertas o ganador "
    "en requisitos; conserva la necesidad de revision humana.\n"
)


def reading_policy(context):
    """Absence is byte-identical legacy; a present policy must be recognized."""
    if not isinstance(context, dict):
        raise ValueError("Selected reading policy requires a JSON context")
    if "llm1_reading_policy" not in context:
        return None
    policy = context["llm1_reading_policy"]
    if not isinstance(policy, str) or policy != COMPACT_UNIQUE_REQUIREMENTS_POLICY:
        raise ValueError("Unsupported selected LLM1 reading policy")
    return policy


def reading_instruction(context):
    return COMPACT_UNIQUE_REQUIREMENTS_INSTRUCTION if reading_policy(context) else ""
