# F1.1, F2 y F3 — archivo mensual y ciclos por moneda

Fecha: 2026-11-02.

Esta versión implementa las tres ampliaciones financieras solicitadas:

- **F1.1:** `0016_legacy_monthly_budget` renombra `monthly_budget` a
  `monthly_budget_legacy`. Conserva filas, IDs, importes, notas y fechas.
  Los métodos anteriores de escritura rechazan cambios; el histórico ARS
  permanece consultable por mes. No convierte ingresos viejos en ingresos
  del ciclo ni duplica dinero.
- **F2:** `/liberado [moneda]` es un alias anunciado de `/disponible [moneda]`.
  El dashboard usa el MoneyEngine y retira el cálculo mensual anterior de
  la vista operativa. `/api/month-summary?month_year=YYYY-MM` permanece como
  consulta histórica de solo lectura, identificada expresamente en la respuesta.
- **F3:** `0017_currency_cycles` agrega calendarios por moneda e identifica
  los ciclos por usuario, moneda e índice. Puede haber un ciclo abierto de
  cada moneda. Ingresos, ahorros, gastos, compromisos, arrastres y cierre se
  calculan dentro de esa moneda, sin conversión. Los cambios de calendario
  afectan a sus sucesores y no cambian fechas ya abiertas o cerradas.

ARS conserva `UserSettings` y el primer viernes existente. Las otras monedas
pueden tener una regla propia; mientras no se configure o abra su primer
ciclo, muestran la regla inicial heredada. Abrir ese primer ciclo conserva
una configuración propia en la misma transacción: cambiar ARS después no
desplaza los próximos ciclos de la otra moneda. Guardar un calendario no
crea ciclos ni ingresos.

Las consultas y acciones de ciclo aceptan `?currency=USD`; las escrituras de
dinero y calendario aceptan `currency` en el cuerpo. La moneda por omisión
es ARS. Una escritura que combina el ID de un ciclo ARS y moneda USD se
rechaza con `409 cycle_currency_mismatch`. Los reintentos de un mismo
`source_key` no pueden trasladar un ingreso extra o ahorro realizado a otra
moneda; PostgreSQL serializa esa identidad incluso entre dos ciclos.

## Actualización de instalaciones anteriores

1. Crear y verificar un backup antes de cambiar código o esquema.
2. Revisar si quedan ciclos abiertos con importes, gastos u obligaciones de
   varias monedas. `0017` hace esta comprobación antes del DDL y se detiene
   conservando esquema, snapshots y asociaciones si encuentra uno.
3. Si existe un ciclo compartido abierto, cerrar ese ciclo con la versión
   anterior, preferiblemente al terminar su período. La API disponible en
   esa versión es `POST /api/cycle/{id}/close`; si quedan obligaciones,
   `?force=true` conserva el cierre y su arrastre previsto. Cerrar antes del
   fin no permite reabrir el mismo período; los sucesores siguen sus fechas.
   Consultar el cierre antes de continuar con la actualización.
4. Detener el proceso de la app, ejecutar `alembic upgrade head` y arrancar
   la nueva versión. El proceso de PostgreSQL y su volumen se conservan.
5. Verificar calendario, conteos, importes anteriores, API, Telegram y un
   nuevo backup. Probar restauraciones en una base desechable.

Los snapshots cerrados con varias monedas se conservan byte a byte y se
identifican con `legacy_mixed_currency`. Aparecen en el histórico de sus
monedas; sus arrastres se filtran para cada sucesor. No se reparten ni
reescriben asociaciones antiguas. Un ciclo abierto que contiene únicamente
USD conserva además su regla congelada en la configuración USD.

El downgrade rechaza estados que el modelo anterior no puede representar
sin pérdida: calendarios propios, varios ciclos abiertos o índices repetidos
por usuario. Se comprueba antes de modificar el esquema. Para recuperar una
instalación con actividad posterior a F3, usar el backup y su versión de app
compatibles, en lugar de borrar configuraciones para forzar el downgrade.

## Validación de la versión

- **810 pruebas normales PASS** en la imagen final, con Python 3.12.16.
- **50 pruebas PostgreSQL PASS**, más los scripts R1, R2 y R3. Conserva las
  34 regresiones anteriores y agrega 16 de migración y concurrencia.
- **90 archivos del runtime coinciden** con el código revisado. Los 60
  paquetes instalados conservan las mismas versiones de la base auditada.
- La copia aislada de la instalación pasó `0015 → 0017 → 0015 → 0017`;
  todos los conteos y fingerprints de columnas anteriores quedaron iguales,
  incluida la tabla mensual renombrada. Primer viernes ARS conservado, sin
  crear ciclos, ingresos ni ahorros durante las consultas de comprobación.
- Telegram/API prueban monedas inválidas, cargas inválidas sin efectos,
  rechazo de moneda ajena y alias `/liberado`. Una lectura con una sesión
  cacheada vuelve a consultar el ciclo y respeta el snapshot si otro proceso
  ya lo cerró.
- Dashboard revisado con datos sintéticos: ARS primer viernes y USD día 15,
  cambio de moneda con respuestas HTTP demoradas y viewport móvil de 390 px
  sin desbordes. Guardar otro calendario no crea movimientos financieros.
- El control previo a publicación no encontró credenciales reales ni dumps,
  bases de datos o `.env` entre los archivos de la versión.

## Despliegue local verificado

La instalación se actualizó el 2026-11-02 con el código `42ef891` y la imagen
`a01254922042`, pasando de `0015_payday_calendar_rule` a
`0017_currency_cycles`. PostgreSQL conservó su contenedor y volumen. Se
verificaron backups anteriores y posteriores al cambio, y la configuración
de entorno permaneció idéntica.

Los fingerprints de todas las columnas anteriores quedaron iguales después
de migrar, arrancar y consultar la app: se conservaron los 69 gastos, 7
plantillas fijas, 6 pagos y demás registros existentes. El calendario ARS
sigue en el primer viernes de cada mes. Los ciclos, ingresos y registros de
ahorro permanecen vacíos, según la decisión de cargar los montos después.

La app está saludable; Telegram inició y respondió polling con HTTP 200,
sin conflicto ni fallo de autenticación. Dashboard general y mensual,
calendario, historial, recurrentes y archivo mensual respondieron
correctamente. El dashboard abierto en el navegador integrado mostró ARS,
el estado sin ciclo y la configuración del primer viernes, sin errores de
JavaScript. La automatización diaria de backups existente se conserva.
