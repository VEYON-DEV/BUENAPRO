# BuenaPro DB

## Migration tool

Use plain SQL migrations in `db/migrations`.

Rationale:

- PostgreSQL is the source of truth.
- The worker is Python and the web is Next.js, so SQL keeps the schema neutral.
- The first MVP needs stable tables before choosing an ORM.

## Running migrations

```bash
export DATABASE_URL=postgresql://buenapro:buenapro@localhost:5432/buenapro
python3 scripts/migrate.py
```

The runner records applied files in `schema_migrations`.

## Timestamp convention

All database timestamps use `TIMESTAMPTZ` and are stored in UTC.

SEACE dates must be parsed as `America/Lima`, converted to UTC before insert, and rendered back to `America/Lima` in the web UI.

## Canonical source identity (migration 0027)

`opportunities.id` is BuenaPro's UUID for a source record. `record_kind` says
whether it is an opportunity, early notice, market sounding, purchase order,
catalog reference, or another kind of record. The separate `object_type`,
`procurement_method`, `lifecycle_stage`, `participation_access`, and
`actionability` columns keep distinct facts from being collapsed into one tag.
An `open` lifecycle stage alone does not mean the record is actionable.

`opportunity_sources` maps `(source_key, id_kind, external_id)` to that UUID and
keeps source-native fields in `native_values`. For PROD6, the mapping is
`(seace_prod6, id_contrato, seace_contracts.id_contrato::text)`; its original
search/detail JSON remains in `seace_contracts`. Future adapters use their own
`source_key`, so the same external ID can exist in different sources.

Migration 0027 backfills every existing `seace_contracts` row and adds its
`opportunity_id` foreign key. The column stays nullable for the worker rollout:
the writer must create or find the source mapping before assigning it on every
new PROD6 row. Existing `id_contrato` keys and all dependent tables continue to
work during this transition. The backfill is safe to rerun and checks that the
contract and source mapping point to the same UUID.

Migration 0028 extends this identity to `historical_contract_outcomes`. An
outcome with the same PROD6 `id_contrato` reuses the live row's UUID; a
history-only outcome receives a new UUID and the same source-key format.
Adjudicated/deserted records become informational, never open opportunities.
The historical table keeps its original primary key and remains optimized for
award comparables.

## Initial seeds

The initial migration seeds stable SEACE catalogs:

- objects: Bien, Servicio, Obra, Consultoria de Obra
- states: Vigente, En Evaluacion, Culminado

CUBSO segments are synced per year from SEACE. MVP enabled segments for tecnologia, transporte and legal must be selected after validating the 2026 catalog.

## Syncing CUBSO segments

Preview the current SEACE catalog:

```bash
python3 scripts/sync_cubso_segments.py --year 2026 --dry-run
```

Persist it to PostgreSQL:

```bash
export DATABASE_URL=postgresql://buenapro:buenapro@localhost:5432/buenapro
python3 scripts/sync_cubso_segments.py --year 2026
```

The initial MVP segment seed enables:

- tecnologia: `43`, `81`
- transporte: `78`
- legal: `80`
