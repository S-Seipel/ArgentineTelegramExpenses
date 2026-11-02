# Roadmap

Estado de las fases financieras y de las herramientas implementadas.

Revisión: 2026-11-02. Las propuestas anteriores se contrastaron con el código
y se sustituyeron por el alcance efectivamente entregado.

## Financial phases

| Phase | Scope | State |
|---|---|---|
| F0 | Expense identity, exact money, fixed-payment mirrors, reminders, dates and migrations | Closed |
| F1 R1 | Payday configuration and cycle bootstrap | Closed |
| F1 R2 | Read-only MoneyEngine, cycle closing, rollover and snapshots | Closed |
| F1 R3 | Income, extras and savings mutations | Closed |
| F1 R4 | Repair, Telegram/API/dashboard integration, immutable history and concurrency checks | Running locally |
| F1.1 | Archive previous monthly income in `monthly_budget_legacy`, preserving records and rejecting legacy writes | Running locally; introduced in migration 0016 |
| F2 | Cycle commands and dashboard are the primary financial interface; `/liberado` aliases `/disponible` | Running locally |
| F3 | Independent payday settings, cycles, rollover and money isolation per currency | Running locally on migration 0017 |
| F1.5 | Opt-in recurring commitments with stable due dates across reminder snoozes | Running locally; introduced in migration 0014 |
| Payday calendar | Fixed monthly day or the first selected weekday; first Friday is supported explicitly | Implemented and validated on migration 0015; rollout recorded separately |
| Local backups | Manual copies and daily Buenos Aires schedule, checksums, isolated restore and seven-copy automatic retention | Installed locally; the daily job and its first verified copy passed |

The existing local installation was updated on 2026-11-02. Telegram polling,
the dashboard, and the configured text provider were checked after startup.
Whisper's cached model runs as the non-root app user; silent audio is rejected
without inventing a transcription. The existing environment configuration
was preserved. Original database records and currency totals were unchanged.

Release validation includes the normal suite on the Python 3.12 runtime
image, PostgreSQL migration and concurrency tests, runtime dependency
auditing, browser checks and a rehearsal on an isolated restored backup.
Exact results are recorded per release rather than freezing a test count
in this roadmap.

F1.5 forecasts the cycle and retains one overdue occurrence per recurring
template; it does not accumulate forgotten monthly installments as debt.
Closed snapshots remain immutable. Ordinary reminders reserve no money.

F1.1/F2/F3 extend F1; they are not additional R blocks. Migration 0016
renames the old monthly table without copying its amounts into cycle
income. Its compatibility API remains read-only. Migration 0017 introduces
currency calendars and independent cycle identities. Each currency has its
own current cycle, closing snapshot, successor and pending obligations.
Existing closed mixed-currency snapshots remain unchanged and readable.
There is no currency conversion or combined ARS/USD total.

The first-Friday calendar in ARS remains configured. An explicit calendar
change affects only that currency's successors, preserving open and closed
cycle dates. Saving a calendar does not create a cycle or enter income.

---

## Herramientas del roadmap implementadas

Las propuestas antiguas se conciliaron con el código: esta tabla describe
las capacidades entregadas, sin repetir funciones ya disponibles como trabajo pendiente.

| Capacidad | Implementación |
|---|---|
| Presupuestos con alertas | Límites por categoría y moneda; avisos de consumo |
| Tickets por foto | OCR local JPEG/PNG, borrador editable y confirmación explícita; reintentos sin duplicados |
| Dashboard | Vistas general/mensual, ciclos, historial y herramientas en móvil |
| Buscar y comparar | Búsqueda por nombre, comparación entre períodos y categorías |
| Rangos de fechas | `/rango` y consultas con fechas explícitas, inclusive por voz |
| Posponer avisos | Mañana, en tres días o el próximo viernes; la deuda conserva su vencimiento |
| Deshacer | Últimas diez ediciones/borrados/ubicaciones de gastos normales; historial durable y guardas de revisión |
| Varios usuarios | Propietario y allowlist explícita; gastos, ciclos, preferencias y avisos propios |
| Respuestas por voz | eSpeak NG y FFmpeg locales, opt-in, español/inglés/portugués |
| Ubicación | Asociación explícita, eliminación reversible y mapa de coordenadas local |
| Clasificador aprendido | Tres ejemplos propios unánimes por nombre; casos desconocidos siguen usando IA |
| Proyección de gasto | Previsión por moneda en el dashboard, con comparación de períodos |
| Comandos de grupo | Sufijo `@botname` reconocido |
| Backups | Copias verificadas, retención y automatización diaria ya instalada |
| Métricas | `/metrics` para Prometheus: operaciones, latencias, errores y rutas sin datos personales |
| Idiomas | Ayuda y respuestas estructuradas del bot en ES/EN/PT; zona y voz por usuario |

Migraciones nuevas: `0018_expense_history_location` y `0019_user_preferences`.
Guardar preferencias no crea ciclos ni movimientos financieros. La primera
inicialización crea únicamente el perfil del propietario configurado; nadie
más recibe acceso automáticamente.

## Alcance operativo

- Los tickets guardan un gasto por total confirmado. Un OCR ambiguo requiere
  corrección manual; no se inventan importes sumando ítems.
- Undo opera sobre gastos normales, incluidos tickets; los pagos fijos y
  recurrentes se revierten desde sus propios flujos.
- Cada usuario utiliza su chat de Telegram. El dashboard sigue siendo local
  y privado del propietario, sin publicación en Internet.
- Los idiomas se aplican al bot y sus mensajes estructurados. El dashboard
  permanece en español; nombres, categorías e informes libres de IA conservan
  su contenido original.
- El proveedor de texto configurado se conserva. Fotos, transcripción y
  síntesis de voz se procesan localmente. No hay fallback automático a un
  proveedor externo nuevo.
- Las integraciones de pagos e inversiones permanecen fuera del alcance.

Ver `REPORTE_ROADMAP_TOOLS_RELEASE.md` para validación y despliegue.
