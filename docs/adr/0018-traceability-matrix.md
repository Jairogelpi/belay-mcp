# ADR 0018: Matriz de trazabilidad spec MUST -> test (docs/plan.md §8)

Fecha: 2026-07-23
Estado: aceptado

## Contexto

`docs/plan.md` §8 exigía desde E0 mantener `docs/traceability.md`: una tabla
sección-de-spec -> tests que la cubren, generada por un script que lee
marcadores `@spec("X.Y")` en los tests, con una lista de MUSTs extraída a
mano. Nunca se construyó — `CHANGELOG.md` (bajo `[0.1.0]` -> "Known gaps") y
el ADR 0009 lo documentaron honestamente como ausente. Esta entrega lo
construye.

## Metodología de extracción de MUSTs

Se leyó `docs/spec.md` completo y se localizó cada ocurrencia de "MUST"
(incluyendo "MUST NOT") por `grep`. No todas las ocurrencias son requisitos
distintos: varias frases dentro de una misma sección son la misma obligación
repetida en distinta redacción, o dos cláusulas paralelas dentro de una
frase (p. ej. §4.2 describe tres reglas de reversibilidad en una sola
sección — `reversible`, `irreversible`, `conditional` — que son tres
requisitos distintos, no uno). Regla aplicada: **un MUST curado = una
afirmación verificable de forma independiente por un test**, sin importar
cuántas veces la spec la repita con otras palabras. El resultado son 31
requisitos (`MUSTS` en `scripts/traceability.py`), identificados como
`<sección>.<índice>` (p. ej. `4.2.1`, `4.2.2`, `4.2.3` para las tres reglas
de §4.2; `10.3.1`/`10.3.2` para las dos obligaciones de honestidad de §10.3).
Actualizar esta lista es, por diseño, parte de cambiar la spec (tal como
pide el plan).

## Convención de marcador elegida

Ya existía una convención parcial: `@spec("9.x")` en el *docstring del
módulo* de `belay/ledger/model.py`, `store.py`, `verify.py`, `redact.py`,
`replay.py`. El plan, sin embargo, describe marcadores **sobre tests**
específicos, no sobre módulos de producción. Se extendió la misma sintaxis
(`@spec("X.Y")` dentro de una cadena, entre comillas dobles) al *docstring
de la función de test* — no se inventó una segunda convención (p. ej.
decoradores, comentarios `# spec:`), se generalizó la ya existente al nuevo
lugar que pedía el plan. El script la localiza parseando el AST de cada
`tests/**/test_*.py` y `conformance/tests/**/test_*.py`, extrayendo el
docstring de cada función `test_*` (o método de clase) y aplicando una
regexp sobre marcadores `@spec("...")`.

## Qué se encontró sin cubrir

De los 31 MUSTs curados, **30 ya tenían un test real que los ejercitaba**
sin marcador — resultado esperado de la disciplina TDD de E0-E9. Se
identificaron los tests correspondientes leyendo cada suite (`tests/contracts/`,
`tests/executor/`, `tests/proxy/`, `tests/planner/`, `tests/policy/`,
`tests/approvals/`, `tests/ledger/`, `tests/rewind/`) y se les añadió el
docstring con el marcador.

Un MUST genuinamente **no tenía test**: spec §14 exige "Unknown fields MUST
be... rejected (contracts, policies)". `tests/contracts/test_model.py`
prueba el rechazo de campos desconocidos en `Contract`
(`test_unknown_top_level_field_is_rejected`), pero no existía el equivalente
para `PolicyDoc` — aunque `belay/policy/model.py` ya usa
`ConfigDict(extra="forbid")`, nadie lo había probado explícitamente. Se
escribió `tests/policy/test_engine.py::test_unknown_top_level_field_in_policy_doc_is_rejected`,
que falla si `extra="forbid"` se relaja alguna vez en `PolicyDoc`. No hubo
que tocar `belay/`: el comportamiento ya era correcto, solo no estaba
verificado por un test con nombre.

## El script

`scripts/traceability.py`:

- `MUSTS`: la lista curada (`Must(id, section, text)`).
- `scan_markers()`: recorre `tests/` y `conformance/tests/`, parsea AST,
  devuelve `{must_id: ["archivo.py::test_nombre", ...]}`.
- `build_report(musts, coverage)`: cruza ambas listas, produce la tabla
  Markdown y la lista de MUSTs sin cobertura.
- `main()`: falla (`exit 1`) nombrando cada MUST sin test si hay alguno;
  si no, escribe `docs/traceability.md` y sale con 0.

Wired en CI (`.github/workflows/ci.yaml`) como paso `Spec MUST
traceability`, después de la suite completa — si alguien borra o rompe el
marcador de un test que cubre un MUST único, el build falla nombrando
exactamente cuál.

## Tests del propio generador

`tests/tools/test_traceability.py` prueba, sin fixtures ni mocks pesados:
que el estado real del repo no tiene MUSTs sin cubrir; que inyectar un MUST
falso sin marcador es detectado; que una lista sintética totalmente cubierta
pasa; y que la tabla generada tiene una fila por cada MUST curado con el
texto exacto.

## Consecuencia

`docs/plan.md` §8 queda cerrado: la trazabilidad ya no es "se verificó
leyendo la suite" (como decía el gap documentado), es un artefacto generado
y verificado en CI. `CHANGELOG.md` mantiene la nota histórica del gap y
añade la fecha de resolución, para no reescribir la historia.
