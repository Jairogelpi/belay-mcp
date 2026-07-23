# ADR 0013: E13 — Cryptographically signed, offline-verifiable evidence

Fecha: 2026-07-23
Estado: aceptado

## Contexto

E13 implementa `docs/plan-v2.md` sección "E13 — Cryptographically signed,
offline-verifiable evidence". El hash chain del ledger (E2, spec §9.2)
prueba consistencia interna -- que ningún evento fue alterado o reordenado
-- pero solo para quien confía en quien presenta la cadena y tiene acceso
para recomputarla. E13 añade una firma Ed25519 sobre esa misma cadena para
que un tercero, sin acceso a la base de Belay y sin relación de confianza
con el operador, pueda verificar de forma independiente que una secuencia
exacta de eventos ocurrió y no fue alterada desde su exportación.

## Decisiones

- **Ed25519 vía `cryptography`, no sigstore/X.509, para v1.** plan.md §11 ya
  apuntaba a "firma de contract sets vía sigstore" como dirección v0.2+;
  E13 generaliza la idea a todo el ledger de sesión pero elige Ed25519 plano
  porque el requisito explícito es "fully offline-verifiable": sigstore
  depende de un Fulcio/Rekor (CA transparente + log de transparencia) que
  requiere red para emitir *y* para verificar contra el log; X.509
  tradicional requiere una CA (propia u operada por terceros) y gestión de
  revocación (CRL/OCSP, de nuevo red). Ed25519 con una clave que el operador
  genera y controla (`belay keygen`) no depende de nada externo: la
  verificación solo necesita el fichero exportado y la clave pública, cero
  red, cero base de datos, cero llamada a Belay -- literalmente lo que pide
  el criterio de salida. **Camino de mejora explícito, no descartado:**
  cuando el operador sí quiera identidad verificable de terceros (no solo
  "esta clave firmó esto") sigstore/X.509 encajan *encima* de este mismo
  formato de evidencia sin romperlo -- añadiendo un certificado/cadena de
  confianza junto a `public_key` en el bundle, no reemplazando el mecanismo
  de firma.
- **Qué prueba la firma y qué NO prueba (honestidad, no sobre-reclamar).**
  Una verificación exitosa prueba: (1) esta secuencia exacta de eventos
  encadenados existió cuando se exportó (cadena de hashes de E2, intacta),
  y (2) la clave privada correspondiente a `public_key` firmó exactamente
  ese resumen (`session_id`, `set_hash`, `chain_head_hash`, `event_count`,
  `signed_at`). **No prueba** identidad del humano que aprobó cada paso --
  eso ya vive, sin cambios, en los campos `approved_by` que el ledger de E4
  registra dentro de `payload`; la firma de E13 certifica la integridad del
  *contenedor* de esa evidencia, no añade una segunda capa de verificación
  de identidad humana por encima de lo que E4 ya capturaba. Tampoco prueba
  que la clave privada del operador nunca fue robada o usada por otra
  persona -- eso es exactamente lo que "no repudio criptográfico" nunca
  puede probar por sí solo, en ningún esquema de firma; solo dice "alguien
  con esta clave privada firmó esto".
- **Trust model del `public_key` embebido: documentado como débil por
  defecto, con opt-in explícito a algo más fuerte.** `SignedEvidence`
  embebe `public_key` por conveniencia (mostrar de quién dice ser, sin
  depender de un fichero externo), pero un atacante que reconstruye todo el
  bundle puede sustituir `public_key` para que combine con una firma
  forjada con su propia clave -- por eso `verify_evidence` acepta
  `trusted_public_key_hex` (y la CLI `--pubkey`) para que el verificador use
  la clave pública que *ya conocía de antemano* (p. ej. publicada por el
  operador por un canal separado), no la que el fichero dice tener. Esto es
  exactamente el escenario de tamper (b) del plan ("re-firmado con clave
  distinta"): sin `--pubkey`, un bundle re-firmado con clave del atacante
  (que también sustituye `public_key`) verificaría "válido" -- el test
  `test_tamper_b_...` lo prueba deliberadamente pasando la clave de
  confianza fuera de banda.
- **Cuatro fallos, cuatro `stage` distintos, en un orden deliberado que
  distingue (b)/(c) de (d).** `verify_evidence` corre, en este orden:
  `chain` (recomputa vía `verify_chain`, sin duplicar su lógica) →
  `coherence` (`verify_coherence`) → `signature` (firma sobre los campos de
  resumen *tal como el bundle los declara ahora mismo*) →
  `summary_mismatch` (los mismos campos, pero recomputados desde los
  eventos embebidos). El orden importa: si se editan campos del resumen sin
  re-firmar (c), el paso de firma falla primero porque los bytes firmados
  ya no coinciden con lo que el bundle declara ahora -- coincide con "fails
  the signature check" tal como pide el plan. Si en cambio se *añaden*
  eventos tras la firma (d), el resumen declarado por el bundle no cambió,
  así que la firma sigue verificando correctamente contra ese resumen
  (intacto) -- lo que falla es que el resumen recomputado desde los
  eventos (ahora más largos) ya no coincide con lo declarado, reportado
  como `summary_mismatch`, no como fallo de firma. Reordenar estos dos
  pasos colapsaría (c) y (d) en el mismo `stage`, perdiendo exactamente la
  precisión que el plan exige.
  Tests: `tests/ledger/test_signing.py::test_tamper_{a,b,c,d}_*`, cada uno
  su propio `stage`.
- **`SigningKey` nunca toca `belay/ledger/store.py` ni la tabla `events`.**
  Persistida solo como fichero PKCS8 PEM sin cifrar, en una ruta que el
  operador controla (`belay keygen <path>`); cifrar ese fichero en reposo,
  o guardarlo en un HSM/keychain del SO, es responsabilidad del operador,
  fuera de alcance de v1 (ver brecha de gestión de claves abajo).
  Test explícito: `test_private_key_never_appears_in_the_exported_evidence_bytes`
  grepea los bytes crudos del bundle exportado en busca del material
  privado (raw bytes, hex, y el literal `"PRIVATE KEY"`) y confirma que solo
  la clave *pública* aparece.
- **Propiedad Hypothesis: cualquier byte flipeado dentro de `events` rompe
  la verificación, nunca un falso negativo.** `sign_session`/`verify_evidence`
  reusan `verify_chain`/`canonical_bytes` (E2) precisamente porque ese es el
  mecanismo ya probado (test de conformidad "corromper el evento *k* → falla
  en *k*", ADR 0002) que hace la propiedad cierta por construcción: cualquier
  byte alterado en un evento cambia su hash canónico, lo que rompe `hash` o
  el `prev_hash` del siguiente evento. `verify_evidence` no repite ese
  trabajo con una segunda implementación -- lo llama.
- **`sign_session` rechaza firmar una cadena ya rota o una lista vacía.**
  Firmar algo que ya no encadena sería firmar una mentira; se falla alto y
  claro (`ValueError`) antes de emitir ninguna firma, en vez de producir un
  bundle "firmado" pero inválido desde el origen.

## Brechas conocidas / seguimiento (no resueltas, documentadas honestamente)

- **Sin rotación ni revocación de claves en v1.** Belay genera y usa claves
  Ed25519, pero no hay ningún mecanismo para que un verificador sepa que
  una clave fue comprometida o retirada -- ni una lista de revocación, ni
  expiración, ni versión de clave en el propio bundle más allá de
  `public_key`. Si la clave privada de un operador se filtra, cualquier
  evidencia firmada con ella (pasada o futura, hasta que el operador deje de
  usarla) sigue verificando como "válida" indefinidamente. Esto es una
  brecha real, no un límite teórico -- queda anotada como trabajo futuro
  (p. ej. un `key_id`/`valid_from`/`valid_until` en el bundle, o adoptar
  sigstore/sus logs de transparencia cuando la dependencia de red deje de
  ser un problema para el caso de uso).
- **Sin cifrado en reposo del fichero de clave privada.** `belay keygen`
  escribe PKCS8 PEM sin cifrar; proteger ese fichero (permisos de sistema
  de archivos, HSM, keychain del SO) es responsabilidad exclusiva del
  operador en v1.
- **El `public_key` embebido no es, por sí solo, prueba de identidad.**
  Sin `--pubkey`/`trusted_public_key_hex` suministrada fuera de banda, un
  verificador solo confirma auto-consistencia interna del bundle (ver
  arriba) -- no que la clave pertenezca realmente al operador que dice
  firmarlo. Documentado en la CLI (`verify-evidence --help`) y en este ADR,
  no oculto en la letra pequeña.

## Referencias

- `docs/spec.md` §9 (ledger), `docs/plan-v2.md` sección "E13".
- `docs/adr/0002-e2-ledger.md` (hash chain reusado, no duplicado).
- Código: `belay/ledger/signing.py`, `belay/cli/main.py` (`keygen`,
  `verify-export`, `verify-evidence`).
- Tests: `tests/ledger/test_signing.py`, `tests/cli/test_verify_evidence.py`.
- Demo: `examples/demo_signed_evidence.py`.
