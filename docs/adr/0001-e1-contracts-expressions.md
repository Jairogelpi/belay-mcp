# ADR 0001: E1 — contracts and the expression language

Fecha: 2026-07-22
Estado: aceptado

## Contexto

E1 implementa `docs/spec.md` §4 (Reversibility contracts) y su Apéndice A
(JSON Schema normativo), según `docs/plan.md` sección "E1 — Contratos y
lenguaje de expresiones".

## Decisiones

- **Parser de expresiones: recursivo-descendente escrito a mano, no `ast`.**
  `docs/plan.md` §1 permite ambas rutas ("`ast` de gramática cerrada" o "parser
  recursivo manual" como alternativa aceptada). Se eligió el parser manual
  porque la gramática de §4.3 es pequeña y cerrada (cuatro raíces `$args/
  $result/$context/$state`, un puñado de operadores, un único builtin
  `coalesce`), y un tokenizer + parser propio hace el límite de seguridad
  *auditable a simple vista*: no hay `ast.parse` cuyo árbol haya que podar
  después, ni nodos de Python (llamadas, comprensiones, lambdas) que deban
  excluirse caso por caso. El árbol resultante (`Literal | PathRef | BinOp |
  UnaryNot | Coalesce`) solo puede representar lo que la gramática permite;
  no existe una rama que ejecute código. `eval`/`exec` quedan prohibidos por
  `docs/plan.md` §1 y el AGENTS.md del repo; el parser manual además prohíbe
  cualquier `ast.parse`/`compile` transitivamente.
- **Rechazo de dunders explícito, no incidental.** Aunque la gramática no
  soporta llamadas de función (salvo `coalesce`) y por tanto `__import__(...)`
  ya no parsea, se añadió un rechazo *explícito* de cualquier segmento de ruta
  que empiece o termine en `__` (p. ej. `$args.__class__`), documentado como
  requisito de seguridad en spec §4.3 y probado con un test dedicado y una
  propiedad Hypothesis, en vez de depender solo de que la gramática no tenga
  soporte de llamadas.
- **Validación de Appendix A vía Pydantic v2, no una librería de JSON Schema.**
  Las tres restricciones `allOf` de Appendix A (reversible↔undo,
  irreversible↔¬undo, conditional↔undo∧conditions) se codifican como un
  `model_validator` sobre el modelo `Contract`, reutilizando Pydantic (ya
  dependencia del proyecto) en vez de añadir `jsonschema` como dependencia
  nueva. Los tests verifican los tres casos uno a uno.
- **Campos desconocidos rechazados vía `extra="forbid"`** en todos los
  modelos (Contract, Undo, Capture, Effect, Provenance, ContractSet), spec
  §14: "la autoridad es estricta" para contratos.
- **`set_hash` = `sha256:` + SHA-256 de la forma canónica JSON** (ordenada por
  `tool`, claves ordenadas, sin espacios) usando `belay/canonical.py`
  (implementado ahora, antes stub de E0). Estable frente a reordenar claves,
  YAML vs JSON, y orden de ficheros de entrada; cambia ante cualquier byte
  distinto.
- **Un fichero de contratos puede tener varios documentos** (YAML
  multi-documento `---` o lista JSON/YAML) porque `docs/plan.md` §2 describe
  un fichero por servidor (`fs.yaml`, `crm.yaml`, `email.yaml`) que agrupa
  contratos de varias tools.

## Referencias

- `docs/spec.md` §4 (contratos), §4.3 (expresiones), Apéndice A (JSON Schema).
- `docs/plan.md` sección "E1 — Contratos y lenguaje de expresiones (spec §4)".
- Código: `belay/contracts/{model,loader,expressions}.py`.
- Tests: `tests/contracts/test_{model,loader,expressions}.py`.
