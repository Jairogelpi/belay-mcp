# ADR 0000: E0 scaffolding decisions

Fecha: 2026-07-22
Estado: aceptado

## Contexto

E0 ("Andamiaje") establece la estructura del repo, la configuración de
herramientas y el CI antes de implementar ninguna lógica de negocio, según
`docs/plan.md` §2 y la sección "E0 — Andamiaje".

## Decisiones

- Estructura de paquetes exactamente como en `docs/plan.md` §2: no renombrar
  sin una nueva ADR.
- `pyproject.toml` con `hatchling` como backend de build (estándar, sin
  dependencias extra de empaquetado).
- Umbral de cobertura (`fail_under`) puesto a 0 en E0 a propósito: no hay
  lógica real que cubrir todavía. Debe subir por entrega hasta el 90%
  objetivo (`docs/plan.md` §0) conforme se implementan E1-E9.
- Alembic inicializado con `env.py`/`script.py.mako` pero sin revisiones:
  las tablas (`sessions`, `events`, `approvals`, `contract_sets`) llegan en E2
  junto con los modelos SQLAlchemy que las respaldan.
- CLI de Typer con app vacía: solo se garantiza `belay --help`; los
  subcomandos (`wrap`, `run`, `plan`, `approvals`, `rewind`, `verify`) se
  añaden entrega a entrega.

## Referencias

- `docs/spec.md` (documento completo, aún no implementado en E0).
- `docs/plan.md` §2, sección "E0 — Andamiaje".
