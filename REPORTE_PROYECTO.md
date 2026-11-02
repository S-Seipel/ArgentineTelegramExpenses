# Reporte integral del proyecto `telegram-expenses`

Documento de transferencia técnica para que otra IA pueda entender, mantener y
extender el proyecto sin tener que reconstruir el contexto desde cero.

## Índice

1. [Resumen ejecutivo](#1-resumen-ejecutivo)
2. [Arquitectura general](#2-arquitectura-general)
3. [Capa web](#3-capa-web)
4. [Capa Telegram](#4-capa-telegram)
5. [Capa de IA](#5-capa-de-ia)
6. [Persistencia y dominio](#6-persistencia-y-dominio)
7. [Scheduler y ciclo de vida](#7-scheduler-y-ciclo-de-vida)
8. [Configuración](#8-configuración)
9. [Deploy y operación](#9-deploy-y-operación)
10. [Tests](#10-tests)
11. [Mapa de archivos](#11-mapa-de-archivos)
12. [Comandos de Telegram](#12-comandos-de-telegram)
13. [Reglas para otra IA](#13-reglas-para-otra-ia)

---

## 1. Resumen ejecutivo

`telegram-expenses` es un gestor personal de gastos operado principalmente
desde Telegram. Permite registrar gastos escribiendo lenguaje natural o
enviando audios, consultar totales, administrar presupuestos, gastos
recurrentes y gastos fijos mensuales, y visualizar toda la información en un
dashboard web.

Características principales:

- Bot personal de Telegram, limitado a un único `TELEGRAM_ALLOWED_USER_ID`.
- Registro de uno o varios gastos en un mismo mensaje.
- Interpretación de lenguaje natural mediante Ollama local.
- Transcripción de voice notes mediante `faster-whisper`.
- Consultas por día, semana, mes, categoría y últimos gastos.
- Gastos recurrentes mensuales y anuales con recordatorios automáticos.
- Presupuestos mensuales por categoría con alertas al 80% y 100%.
- Gastos fijos mensuales con estado pendiente, pagado, salteado y diferencias.
- Dashboard FastAPI con gráficos, KPIs, tablas, comparativas y proyecciones.
- Persistencia en PostgreSQL mediante SQLAlchemy 2.x.
- Migraciones administradas por Alembic.

### Stack

- Python 3.12.
- FastAPI `0.141.1`.
- Uvicorn `0.32.1`.
- `python-telegram-bot` `21.10`.
- SQLAlchemy `2.0.52`.
- PostgreSQL 15 con `psycopg` 3.
- Alembic `1.14.0`.
- Pydantic 2 + `pydantic-settings`.
- Ollama para interpretación local.
- `httpx` para llamar a Ollama.
- `faster-whisper` para transcripción.
- Chart.js 4.4.4 cargado desde CDN para el dashboard.
- Docker Compose para PostgreSQL y la aplicación.

### Modelo operativo

- La aplicación web y el bot corren en el mismo proceso FastAPI.
- Ambos usan la misma base de datos y las mismas tablas.
- No existe una API intermedia entre el bot y el dashboard.
- El dashboard lee directamente los datos que el bot escribe.
- El servicio está pensado para uso local: Docker publica los puertos en
  `127.0.0.1`.
- El dashboard no tiene login, cookies, sesiones ni tokens HTTP.

---

## 2. Arquitectura general

```text
Telegram
   |
   v
python-telegram-bot -> handlers -> services -> repositories -> PostgreSQL
                              |
                              +-> Ollama local
                              +-> faster-whisper local

Browser -> FastAPI -> web routes -> web queries -> PostgreSQL

FastAPI lifespan
   +-> inicia el bot de Telegram
   +-> hidrata las categorías
   +-> inicia el scheduler de recordatorios
```

### Entrada principal: `app/main.py`

La aplicación FastAPI se define en `app/main.py`.

El `lifespan` hace lo siguiente:

1. Lee la configuración mediante `get_settings()`.
2. Configura el logging global.
3. Crea `BotRuntime` y `BotDependencies`.
4. Siembra/hidrata las categorías desde PostgreSQL.
5. Construye la aplicación de Telegram.
6. Inicializa `python-telegram-bot`.
7. Inicia el polling de Telegram.
8. Inicia el bot.
9. Crea el task asíncrono de recordatorios recurrentes.
10. Guarda referencias en `app.state`.
11. Al apagar, cancela el scheduler, detiene el bot y cierra el cliente de IA.

La aplicación continúa sirviendo la API aunque el bot no pueda arrancar por un
token inválido o un problema de red. Ese error queda registrado en logs.

### Rutas globales

- `GET /` redirige al dashboard mediante el router web.
- `GET /health` devuelve `{"status": "ok"}`.
- `GET /dashboard` sirve el dashboard HTML.
- Las rutas JSON del dashboard usan el prefijo `/api/`.

### Flujo de una operación típica

Ejemplo: el usuario escribe `gasté 10k en un café`.

1. Telegram recibe el mensaje.
2. `handle_text()` valida el usuario autorizado.
3. `_process_message_async()` valida longitud y texto.
4. `OllamaAIService.interpret()` envía el mensaje a Ollama.
5. Ollama devuelve JSON estructurado.
6. Pydantic valida el JSON.
7. El servicio normaliza monto, moneda, fecha y categoría.
8. `ExpenseService` valida el draft.
9. `ExpenseRepository` guarda el gasto y su categoría.
10. Se evalúan alertas de presupuesto.
11. El bot devuelve una confirmación.
12. El dashboard lo verá en el siguiente refresh.

---

## 3. Capa web

### 3.1 Ubicación y características

La capa web está concentrada principalmente en:

- `app/web/routes.py`: HTML, CSS, JavaScript y endpoints FastAPI.
- `app/web/queries.py`: consultas y cálculos de datos.

No hay:

- Templates Jinja2.
- Carpeta `static`.
- Archivos CSS separados.
- Archivos JavaScript separados.
- React, Vue o cualquier framework frontend.
- Bundler.
- Nginx.

Todo el dashboard se construye como un string HTML grande llamado
`DASHBOARD_HTML` dentro de `routes.py`. Chart.js se carga desde jsDelivr.

### 3.2 Endpoints HTML y JSON

| Método | Ruta | Propósito |
|---|---|---|
| `GET` | `/` | Redirige a `/dashboard`. |
| `GET` | `/dashboard` | Devuelve el dashboard completo como HTML. |
| `GET` | `/api/summary` | Totales del período por moneda y categoría. |
| `GET` | `/api/trend` | Serie diaria para el gráfico de tendencia. |
| `GET` | `/api/recent` | Últimos gastos, con límite configurable. |
| `GET` | `/api/expenses` | Gastos del período para la tabla completa. |
| `GET` | `/api/budgets` | Presupuestos con gasto, porcentaje y estado. |
| `GET` | `/api/comparison` | Período actual contra período anterior. |
| `GET` | `/api/projection` | Proyección lineal hacia el final del período. |
| `GET` | `/api/recurring/upcoming` | Recurrentes próximos en los siguientes 7 días. |
| `GET` | `/api/fixed-expenses` | Gastos fijos del mes actual con estado. |
| `POST` | `/api/fixed-expenses` | Crea un gasto fijo desde el dashboard. |
| `POST` | `/api/fixed-expenses/{id}/pay` | Marca un gasto fijo como pagado. |
| `POST` | `/api/fixed-expenses/{id}/unpay` | Deshace el pago del mes. |
| `POST` | `/api/fixed-expenses/{id}/skip` | Saltea el gasto fijo del mes. |
| `GET` | `/api/month-summary` | Resumen de ingreso, extra, pagos y liberado. |

Parámetros habituales:

- `period`: `today`, `week`, `month`, `last_month` o `year`.
- `custom_start` y `custom_end`: soportados internamente para períodos
  personalizados.
- `limit`: límite de resultados en endpoints de listas.

Desde la web solamente existe edición operativa de gastos fijos. Presupuestos,
recurrentes y gastos comunes se administran desde Telegram.

### 3.3 Layout del dashboard

El dashboard aparece en este orden:

1. Header con título y período actual.
2. Toolbar con botones `Mes`, `Semana`, `Hoy`, `Mes pasado` y `Año`.
3. Grid de KPIs, generado dinámicamente por moneda.
4. Fila de gráficos:
   - Tendencia diaria.
   - Distribución por categoría.
5. Fila de información:
   - Presupuestos.
   - Próximos recurrentes.
6. Panel de gastos fijos:
   - Ingreso.
   - Extra.
   - Pagado.
   - Liberado.
   - Lista de gastos fijos.
   - Alta, pagar, saltear y deshacer pago.
7. Fila de análisis:
   - Comparativa contra período anterior.
   - Proyección del período.
8. Panel `Todos los gastos`.
9. Panel `Últimos 10 gastos`.

El contenedor `.row` tiene dos columnas y se transforma en una columna debajo de
800px. Los paneles usan glassmorphism, bordes redondeados, sombras y
backdrop-filter.

### 3.4 IDs principales del DOM

#### Período y KPIs

- `period-bar`: toolbar de períodos.
- `subtitle`: texto del período actual.
- `kpis`: contenedor de tarjetas KPI.
- `trend-title`, `trend-chart`: gráfico de tendencia.
- `category-title`, `category-chart`: gráfico de categorías.

#### Presupuestos y recurrentes

- `budgets-title`, `budgets-list`.
- `recurring-list`.

#### Gastos fijos

- `fixed-panel`.
- `fixed-title`, `fixed-subtitle`.
- `fixed-income`, `fixed-extra`, `fixed-paid`, `fixed-liberado`.
- `fixed-list`.
- `fixed-add-row`.
- `fx-name`, `fx-amount`, `fx-day`, `fx-method`.
- `fx-save`, `fx-cancel`, `fx-error`, `fx-add-btn`.
- `fixed-actions`.

#### Comparativa y proyección

- `comparison-title`, `comparison-list`.
- `projection-panel`, `projection-title`, `projection-card`.

#### Tablas

- `all-title`, `all-subtitle`, `all-total`.
- `all-sort`, `all-tbody`, `all-footer`.
- `recent-title`, `recent-tbody`.

### 3.5 JavaScript del dashboard

El JavaScript está embebido dentro de `DASHBOARD_HTML`.

Funciones principales:

- `getJSON(url)`: wrapper de `fetch`, valida `response.ok` y parsea JSON.
- `refresh()`: función principal de actualización.
- `animateNumber()`: anima valores numéricos con ease-out.
- `renderFixedExpenses()`: renderiza gastos fijos y KPIs.
- `renderAllExpenses()`: renderiza la tabla completa.
- `allSort()`: cambia orden por fecha o monto.
- `fxCreate()`: crea gastos fijos mediante POST.
- `fxAction()`: helper POST común.
- `fxPay()`: marca pago.
- `fxSkip()`: marca salteado.
- `fxUnpay()`: deshace pago.
- `fxToggleAdd()`: muestra u oculta el formulario.
- `palette()`: paleta de colores para gráficos.
- `chartOpts()`: opciones compartidas de Chart.js.
- `escapeHTML()`: escapa strings antes de usar `innerHTML`.

`refresh()` realiza en paralelo las consultas de:

- Summary.
- Trend.
- Recent.
- Budgets.
- Upcoming recurring.
- Comparison.
- Projection.
- Fixed expenses.
- Month summary.
- All expenses.

El dashboard se actualiza:

- Al cargar la página.
- Cada 60 segundos.
- Al cambiar el período.
- Después de crear, pagar, saltear o deshacer un gasto fijo.

Al cambiar de período se aplica una animación de fade y luego se vuelven a
consultar todos los datos.

### 3.6 Períodos

La toolbar expone:

| Valor | Uso |
|---|---|
| `month` | Mes en curso. |
| `week` | Semana actual. |
| `today` | Sólo hoy. |
| `last_month` | Mes anterior. |
| `year` | Año actual. |

El backend también entiende períodos internos como `yesterday`, `last_week`,
`last_year` y `custom`.

### 3.7 Queries web

`app/web/queries.py` separa la lógica SQL del router HTTP.

#### `PeriodWindow`

Dataclass con:

- `label`.
- `start`.
- `end`.
- `days_elapsed`.
- `days_in_period`.
- `is_full_period`.

#### Funciones principales

- `resolve_period()`.
- `summary_for_period()`.
- `daily_trend()`.
- `recent_expenses()`.
- `budget_status()`.
- `comparison()`.
- `projection()`.
- `fixed_expenses_current_month()`.
- `month_summary_dashboard()`.
- `recurring_upcoming()`.

Reglas importantes:

- `daily_trend()` rellena con cero los días sin gastos.
- `budget_status()` prorratea límites fuera de períodos mensuales.
- Estado de presupuesto:
  - `ok`: menos del 80%.
  - `warning`: desde 80%.
  - `exceeded`: desde 100%.
- `comparison()` compara con el período equivalente anterior.
- `projection()` calcula `spent * days_in_period / days_elapsed`.
- Si el período actual es `today`, la proyección no aplica.

### 3.8 Seguridad web

El dashboard está diseñado como una interfaz local single-user:

- No hay autenticación HTTP.
- No hay login.
- No hay sesiones.
- No hay cookies de autenticación.
- No hay CORS configurado.
- No hay `ALLOWED_HOSTS`.
- No hay rate limiting.
- No hay CSRF explícito.
- El usuario de las queries siempre es `settings.telegram_allowed_user_id`.
- Docker publica el puerto solamente en `127.0.0.1`.
- `escapeHTML()` protege datos de usuario antes de insertarlos en HTML.

Si se expone el dashboard fuera de localhost, primero habría que agregar
autenticación, protección CSRF, CORS controlado, TrustedHost y un reverse proxy
con TLS.

---

## 4. Capa Telegram

### 4.1 Construcción de la aplicación

Archivo: `app/bot/app.py`.

`build_application()`:

1. Construye `ApplicationBuilder` con `TELEGRAM_BOT_TOKEN`.
2. Guarda dependencias en `application.bot_data`.
3. Registra handlers para comandos.
4. Registra handlers de voz/audio.
5. Registra handler de texto que no sea comando.

`BotDependencies` contiene el `BotRuntime`, la configuración y el servicio de
IA. `_SilentStop` corta silenciosamente el procesamiento de usuarios no
autorizados.

### 4.2 Enrutamiento de comandos

La entrada principal es `handle_command()` en `app/bot/handlers.py`.

Flujo:

1. Validar autorización.
2. Leer `message.text`.
3. Separar por espacios.
4. Convertir el primer token a lowercase.
5. Resolver comandos rápidos mediante `command_to_spec()`.
6. Enviar cada grupo al service correspondiente.
7. Abrir una sesión de base de datos por operación.
8. Responder el resultado en Telegram.

Grupos de handlers:

- Consultas rápidas.
- Borrado.
- Edición.
- Exportación CSV.
- Recurrentes.
- Presupuestos.
- Búsqueda.
- Gastos fijos.
- Ayuda.

### 4.3 Comandos rápidos

`app/bot/specs.py` mapea estos comandos a `QuerySpec`:

| Comando | Resultado |
|---|---|
| `/hoy` | Total de hoy. |
| `/semana` | Total de la semana. |
| `/mes` | Total del mes. |
| `/gastos` | Últimos 10 gastos. |
| `/desglose` | Categorías del mes. |
| `/desglose_hoy` | Categorías de hoy. |
| `/desglose_semana` | Categorías de la semana. |
| `/desglose_mes` | Categorías del mes. |

### 4.4 Procesamiento de texto libre

Los mensajes que no son comandos pasan a `handle_text()` y luego a
`_process_message_async()`.

Tipos de intención soportados:

- `register_expense`.
- `register_recurring`.
- `query`.
- `greeting`.
- `help`.
- `cancel`.
- `confirm`.
- `unknown`.

Ejemplos de mensajes válidos:

```text
gasté 10k en un café
hoy gasté 5k en café y 12k en Uber
ayer gasté 20 dólares en Steam
gasto 30k en Netflix cada mes el día 15
cuánto gasté hoy
cuánto llevo en comida
mostrame mis últimos 10 gastos
```

El flujo de registro es:

1. Ollama interpreta el mensaje.
2. Pydantic valida el JSON.
3. Se normalizan montos y monedas.
4. Se normaliza la categoría.
5. Se normaliza la fecha.
6. Se crean uno o varios `ExpenseDraft`.
7. Se validan los drafts.
8. Se resuelven las categorías en DB.
9. Se insertan los gastos en batch.
10. Se calculan alertas de presupuesto.
11. Se devuelve una confirmación formateada.

### 4.5 Voice notes

`handle_voice()` acepta mensajes `voice` y `audio`.

Flujo:

1. Obtiene el archivo desde Telegram.
2. Lo descarga en memoria.
3. Lo envía a `OllamaAIService.transcribe()`.
4. Whisper genera un texto.
5. Ese texto se procesa igual que un mensaje normal.
6. La respuesta incluye el texto transcripto.

La transcripción usa `faster-whisper` en `asyncio.to_thread()` porque el modelo
es bloqueante. Requiere `ffmpeg` para decodificar audio.

Formatos prácticos:

- Voice notes `.ogg`/Opus.
- Audio `.mp3`.
- Otros formatos que `ffmpeg` pueda decodificar.

### 4.6 Seguridad Telegram

`_ensure_authorized()` se ejecuta antes de procesar comandos, texto o audio.

La validación es:

```text
update.effective_user.id == settings.telegram_allowed_user_id
```

Si no coincide:

- Se registra un warning.
- Se responde acceso denegado.
- Se detiene el handler.

No hay multiusuario ni roles.

---

## 5. Capa de IA

### 5.1 Ollama

Archivo: `app/ai/service.py`.

`OllamaAIService` usa un `httpx.AsyncClient` y llama:

```text
POST {OLLAMA_BASE_URL}/api/chat
```

Payload relevante:

- Modelo configurable.
- Mensaje system.
- Mensaje user.
- `stream: false`.
- `format: json`.
- `think: false`.
- `keep_alive: 30m`.
- `temperature: 0.1`.
- `top_p: 0.9`.
- `repeat_penalty: 1.1`.
- `num_ctx: 2048`.
- `num_predict: 512`.

Si el modelo devuelve JSON inválido o que no valida con Pydantic, se reintenta
con un mensaje indicando el error. `OLLAMA_MAX_RETRIES` es `1` por defecto.

Los errores de red y timeout se convierten en `AIUnavailable` y se comunican al
usuario con instrucciones para verificar `ollama serve` y `ollama list`.

### 5.2 Schemas Pydantic

`app/ai/schemas.py` define:

- `Intent`.
- `ExpenseExtraction`.
- `ExtractedExpense`.
- `RecurringExtraction`.
- `QueryIntent`.
- `ParsedMessage`.
- `IntentType`.
- `QueryPeriod`.
- `SortOrder`.

Los validadores normalizan:

- Montos a `Decimal`.
- Monedas como `$` o `AR$` a `ARS`.
- Monedas como `U$S` o `U$D` a `USD`.
- Fechas en varios formatos a ISO.
- Campos faltantes de aclaración.

Si Ollama dice que quiere registrar un gasto pero no devuelve gastos, el schema
crea un placeholder que pide aclaración al usuario.

### 5.3 Prompts

`app/ai/prompts.py` contiene un prompt de sistema y uno de usuario.

El modelo recibe:

- Fecha actual.
- Timezone.
- Categorías disponibles.
- Tipos de intención permitidos.
- Estructura JSON esperada.
- Reglas para pesos, dólares, slang argentino y fechas.

La IA no debe generar SQL ni modificar la DB. Sólo devuelve JSON estructurado.

### 5.4 Validación antes de guardar

`ExpenseService.register_many()` valida:

- Monto mayor a cero.
- Monto máximo de 100.000.000.
- Moneda permitida.
- Nombre no vacío.
- Nombre de máximo 200 caracteres.
- Preguntas de aclaración.
- Categoría válida o fallback a `Otros`.

Si falta información, no persiste parcialmente: devuelve las preguntas de
aclaración.

### 5.5 Servicio de prueba

`StubAIService` permite tests sin Ollama real. Recibe respuestas pregrabadas y
registra llamadas y transcripciones.

---

## 6. Persistencia y dominio

### 6.1 Base de datos

`app/database/database.py` contiene:

- `Base` de SQLAlchemy.
- Engine singleton.
- Session factory.
- `session_scope()` con commit, rollback y close.
- `init_db()` para creación directa, aunque el flujo real usa Alembic.

Las migraciones reales se ejecutan con:

```bash
docker compose exec app alembic upgrade head
```

### 6.2 Migraciones

Hay 7 migraciones:

| Migración | Contenido |
|---|---|
| `0001_create_expenses` | Tabla inicial de gastos. |
| `0002_bigint_telegram_user_id` | Cambia user IDs a BIGINT. |
| `0003_categories_and_fk` | Categorías, árbol y FK de gastos. |
| `0004_recurring` | Gastos recurrentes. |
| `0005_budgets` | Presupuestos e índice único parcial. |
| `0006_fixed_expenses` | Gastos fijos, pagos y presupuesto mensual. |
| `0007_fixed_payment_expense_link` | Link entre pago fijo y gasto real. |

### 6.3 Tabla `expenses`

Modelo principal del sistema.

Campos importantes:

- `id`.
- `telegram_user_id`.
- `name`.
- `amount` como `Numeric(18,2)`.
- `currency`.
- `category_id`.
- `expense_date`.
- `original_message`.
- `ai_confidence`.
- Timestamps.

El dashboard calcula sus totales a partir de esta tabla.

### 6.4 Categorías

El árbol inicial incluye:

- Comida.
- Transporte.
- Entretenimiento.
- Ropa.
- Tecnología.
- Salud.
- Educación.
- Hogar.
- Viajes.
- Regalos.
- Servicios.
- Otros.

Hay subcategorías como Supermercado, Restaurante, Café, Delivery, Uber, Taxi,
Combustible, Juegos, Cine, Suscripciones y Salidas.

`app/categories/registry.py` mantiene un cache thread-safe en memoria. Se
hidrata desde la DB al iniciar el proceso.

La normalización intenta:

1. Match exacto sin distinguir mayúsculas.
2. Match parcial.
3. Fallback a `Otros`.

### 6.5 Recurrentes

Tabla: `recurring_expenses`.

Soporta:

- Frecuencia mensual.
- Frecuencia anual.
- Día del mes.
- Mes del año para anuales.
- Próxima fecha de vencimiento.
- Fecha de última notificación.
- Estado activo/inactivo.

Cuando se confirma un recordatorio, se crea un gasto real y se avanza la fecha
del recurrente.

### 6.6 Presupuestos

Tabla: `budgets`.

Un presupuesto se identifica por:

- Usuario.
- Categoría.
- Moneda.
- Estado activo.

Umbrales:

- Warning: 80%.
- Exceeded: 100%.

No se repite una alerta dentro del mismo mes calendario.

### 6.7 Gastos fijos

Tablas:

- `fixed_expenses`: definición mensual.
- `fixed_expense_payments`: estado por mes.
- `monthly_budget`: ingreso y extra por mes.

Estados de un gasto fijo:

- Pendiente.
- Pagado exacto.
- Pagado por encima.
- Pagado por debajo.
- Salteado.

Cuando se marca como pagado, el service crea o actualiza un `Expense` espejo y
guarda el vínculo en `fixed_expense_payments.expense_id`. Esto permite que el
dashboard incluya los pagos en los totales generales.

Cuando se saltea, se elimina el expense espejo asociado.

`liberado` se calcula como:

```text
income + extra - total_fixed_paid
```

### 6.8 Repositorios

- `ExpenseRepository`: CRUD, búsquedas, sumas, filtros, últimos gastos.
- `RecurringRepository`: templates, vencimientos y estado activo.
- `BudgetRepository`: upsert, listados, activación y alertas.
- `FixedExpenseRepository`: templates, pagos y presupuesto mensual.
- `CategoryRepository`: árbol, seed y búsquedas por nombre.

### 6.9 Servicios

- `ExpenseService`: validación y persistencia batch.
- `RecurringService`: creación, ocurrencias, skip y postpone.
- `BudgetService`: límites y alertas.
- `FixedExpenseService`: pagos, skips, resumen mensual y vinculación con
  `expenses`.
- `QueryService`: consultas rápidas y lenguaje natural.

---

## 7. Scheduler y ciclo de vida

### 7.1 Recordatorios recurrentes

Archivo: `app/recurring/scheduler.py`.

Constantes:

- Tick cada 300 segundos.
- Hora de notificación: 09:00.
- Ventana válida: 09:00 a 09:30.
- Timezone configurable.

Durante la ventana, busca recurrentes vencidos y envía un mensaje con opciones:

- `sí`: registra el gasto.
- `no`: saltea el mes.
- `posponer`: mueve el vencimiento un día.
- `eliminar`: desactiva el recurrente.

El estado de recordatorios pendientes vive en memoria dentro de
`BotRuntime.pending_reminders`. Si el proceso se reinicia, ese estado temporal
se pierde.

### 7.2 Hidratación de categorías

En el startup:

1. `ensure_seed()` crea categorías si la tabla está vacía.
2. `refresh_from_db()` carga el registry en memoria.

### 7.3 Cierre

Al detener la app:

- Se cancela el task de recordatorios.
- Se detiene Telegram polling.
- Se cierra la aplicación PTB.
- Se cierra el cliente HTTP de Ollama.

---

## 8. Configuración

Las variables se cargan desde `.env` mediante Pydantic Settings.

| Variable | Default | Descripción |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | requerido | Token del bot. |
| `TELEGRAM_ALLOWED_USER_ID` | requerido | Usuario único autorizado. |
| `DATABASE_URL` | PostgreSQL local del compose | Conexión DB. |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Endpoint de Ollama. |
| `OLLAMA_MODEL` | `qwen3:4b` | Modelo de interpretación. |
| `OLLAMA_TIMEOUT` | `90.0` | Timeout total. |
| `OLLAMA_MAX_RETRIES` | `1` | Reintentos de JSON inválido. |
| `WHISPER_MODEL_SIZE` | `base` | Tamaño del modelo Whisper. |
| `WHISPER_DEVICE` | `cpu` | `cpu` o `cuda`. |
| `WHISPER_COMPUTE_TYPE` | `int8` | Tipo numérico Whisper. |
| `TIMEZONE` | `America/Argentina/Buenos_Aires` | Zona horaria. |
| `MAX_MESSAGE_LENGTH` | `2000` | Máximo de texto NLP. |
| `HOST` | `0.0.0.0` | Host de Uvicorn. |
| `PORT` | `8000` | Puerto de Uvicorn. |
| `APP_ENV` | `development` | Ambiente. |
| `LOG_LEVEL` | `INFO` | Nivel de logs. |

### Monedas permitidas

`ARS`, `USD`, `EUR`, `BRL`, `CLP`, `MXN`, `UYU`, `PYG`, `GBP`, `JPY`, `CNY`.

### Métodos de pago permitidos

`TRANSFERENCIA`, `EFECTIVO`, `DEBITO`, `TARJETA`, `CREDITO`, `MERCADO PAGO`,
`APP`, `OTRO`.

### Fechas

Se aceptan:

- `YYYY-MM-DD`.
- `DD/MM/YYYY`.
- `DD-MM/YYYY`.
- `hoy`.
- `ayer`.
- `anteayer`.
- Días de la semana en español.

---

## 9. Deploy y operación

### 9.1 Docker Compose

`docker-compose.yml` define dos servicios.

#### `postgres`

- Imagen `postgres:15-alpine`.
- Usuario, password y DB: `expenses`.
- Puerto host `127.0.0.1:5432`.
- Healthcheck con `pg_isready`.
- Volumen persistente `postgres_data`.

#### `app`

- Build desde `Dockerfile`.
- Puerto host `127.0.0.1:8000`.
- Depende de PostgreSQL saludable.
- Monta `./app:/app/app` para hot reload.
- Monta `huggingface_cache` para Whisper.
- Mapea `host.docker.internal` para acceder a Ollama del host.
- Ejecuta Uvicorn con `--reload` en Compose.

Arranque típico:

```bash
docker compose up -d --build
docker compose exec app alembic upgrade head
```

### 9.2 Dockerfile

- Base `python:3.12-slim`.
- Instala `curl` y `ffmpeg`.
- Instala `requirements.txt`.
- Copia `app`, `alembic` y `alembic.ini`.
- Crea y usa usuario no-root `app`.
- Expone puerto 8000.
- Incluye healthcheck a `/health`.

### 9.3 Logging

Formato:

```text
%(asctime)s [%(levelname)s] %(name)s: %(message)s
```

Mensajes relevantes:

- Bot iniciado.
- Bot no pudo iniciar.
- Ollama listo.
- Whisper cargado.
- Categorías hidratadas.
- Usuario no autorizado rechazado.
- Error del scheduler.
- Error enviando recordatorio.
- Error evaluando presupuesto.

### 9.4 Healthcheck

```bash
curl http://localhost:8000/health
```

Respuesta esperada:

```json
{"status":"ok"}
```

---

## 10. Tests

El proyecto incluye tests con pytest y pytest-asyncio.

Áreas cubiertas:

- Servicio de IA.
- Montos y formatos argentinos.
- Postprocesamiento de montos del bot.
- Comandos del bot.
- Servicio principal del bot.
- Presupuestos.
- Categorías y foreign keys.
- Comparativas.
- CRUD de gastos.
- Servicio de gastos.
- Gastos fijos.
- Vinculación de pagos fijos con gastos.
- Recurrentes.
- Búsqueda.
- Formateo.
- Query service.
- Telegram user IDs grandes.
- Voice handler.
- Transcripción.
- Rutas web.

Archivos principales en `tests/`:

```text
conftest.py
```

Para ejecutar la suite:

```bash
pytest -q
```

---

## 11. Mapa de archivos

### Aplicación principal

| Archivo | Responsabilidad |
|---|---|
| `app/main.py` | FastAPI, lifespan, bot y scheduler. |
| `app/config/settings.py` | Configuración desde `.env`. |
| `app/database/database.py` | Engine, sesiones y `Base`. |

### Telegram

| Archivo | Responsabilidad |
|---|---|
| `app/bot/app.py` | Construcción de PTB y registro de handlers. |
| `app/bot/handlers.py` | Dispatch de comandos, texto, voz y recordatorios. |
| `app/bot/service.py` | NLP, persistencia de mensajes y formato de respuestas. |
| `app/bot/specs.py` | Comandos rápidos a `QuerySpec`. |

### IA

| Archivo | Responsabilidad |
|---|---|
| `app/ai/service.py` | Ollama, Whisper, retry y cliente HTTP. |
| `app/ai/schemas.py` | Modelos Pydantic de entrada de IA. |
| `app/ai/prompts.py` | Prompts para Ollama. |

### Dominio

| Directorio | Responsabilidad |
|---|---|
| `app/expenses/` | Gastos normales. |
| `app/categories/` | Árbol, seed y normalización de categorías. |
| `app/budgets/` | Presupuestos y alertas. |
| `app/recurring/` | Gastos recurrentes y scheduler. |
| `app/fixed_expenses/` | Gastos fijos, pagos y presupuesto mensual. |
| `app/queries/` | Query specs y consultas compartidas. |
| `app/utils/` | Montos, fechas y formato. |

### Web

| Archivo | Responsabilidad |
|---|---|
| `app/web/routes.py` | Dashboard completo y endpoints HTTP. |
| `app/web/queries.py` | Queries y cálculos del dashboard. |

### Infraestructura

| Archivo | Responsabilidad |
|---|---|
| `Dockerfile` | Imagen de aplicación. |
| `docker-compose.yml` | PostgreSQL + app local. |
| `requirements.txt` | Dependencias Python. |
| `alembic.ini` | Configuración de migraciones. |
| `alembic/env.py` | Contexto Alembic. |
| `alembic/versions/` | Siete migraciones. |

---

## 12. Comandos de Telegram

### Ayuda y consultas

| Comando | Uso |
|---|---|
| `/start`, `/help`, `/ayuda` | Ayuda general. |
| `/hoy` | Total de hoy. |
| `/semana` | Total semanal. |
| `/mes` | Total mensual. |
| `/gastos` | Últimos 10 gastos con IDs. |
| `/desglose` | Desglose mensual por categoría. |
| `/desglose_hoy` | Desglose de hoy. |
| `/desglose_semana` | Desglose semanal. |
| `/desglose_mes` | Desglose mensual. |
| `/buscar <texto>` | Busca gastos por nombre. |

### Edición y exportación

| Comando | Uso |
|---|---|
| `/borrar_ultimo` | Borra el último gasto. |
| `/borrar <id>` | Borra un gasto por ID. |
| `/editar_ultimo <monto>` | Corrige monto del último gasto. |
| `/editar_ultimo nombre: <texto>` | Corrige nombre del último gasto. |
| `/editar <id> <monto>` | Corrige monto por ID. |
| `/editar <id> nombre: <texto>` | Corrige nombre por ID. |
| `/exportar` | CSV del mes. |
| `/exportar hoy` | CSV de hoy. |
| `/exportar semana` | CSV de la semana. |
| `/exportar mes` | CSV del mes. |
| `/exportar todo` | CSV histórico. |
| `/csv` | Alias de `/exportar`. |

### Recurrentes

```text
/recurrente_add Netflix 30000 15
/recurrente_add_anual Dominio 15000 3 15
/recurrentes
/recurrente_off 3
/recurrente_on 3
/recurrente_del 3
```

El recurrente anual recibe mes y día. Los IDs se obtienen de `/recurrentes`.

### Presupuestos

```text
/presupuesto comida 50000
/presupuesto salidas 50 USD
/presupuestos
/presupuesto_off 3
/presupuesto_on 3
/presupuesto_del 3
```

### Gastos fijos

```text
/gastofijo_add CASA 90000 10 transferencia
/gastosfijos
/pague CASA
/pague CASA 92000
/salte NETFLIX
/gastofijo_off 3
/gastofijo_on 3
/gastofijo_del 3
/ingreso 1100000
/extra 75000
/liberado
```

`/gastofijo_add` acepta nombre, monto, día opcional y método opcional. El día
por defecto es 1.

### Lenguaje natural

```text
gasté 10k en un café
ayer gasté 20 dólares en Steam
hoy gasté 5k en café y 12k en Uber
gasto 30k en Netflix cada mes el día 15
cuánto gasté hoy
cuánto llevo en comida
mostrame mis últimos 10 gastos
```

Montos aceptados:

- `15000`.
- `15k`.
- `15 lucas`.
- `1.500,50`.
- `20 dólares`.
- `USD 20`.

---

## 13. Reglas para otra IA

Al modificar este proyecto, una IA debería respetar estas reglas:

1. Mantener la arquitectura single-user salvo que se pida explícitamente
   multiusuario.
2. Filtrar siempre por `telegram_allowed_user_id` en operaciones web.
3. No guardar datos provenientes de IA sin validación Pydantic y validación de
   dominio.
4. No permitir que Ollama genere SQL.
5. Reutilizar los repositories y services existentes.
6. Usar `session_scope()` para las operaciones de DB.
7. Mantener la vinculación entre `fixed_expense_payments` y `expenses`.
8. Si se modifica el schema, crear una migración Alembic.
9. Si se cambia un comando, actualizar handlers, ayuda, tests y documentación.
10. Si se cambia el dashboard, recordar que HTML, CSS y JS están inline en
    `app/web/routes.py`.
11. Escapar cualquier string de usuario antes de usar `innerHTML`.
12. Mantener montos como `Decimal` en backend y sólo convertir a float en JSON
    cuando sea necesario.
13. Respetar el timezone configurado para fechas, períodos y recordatorios.
14. No asumir que Ollama está disponible: manejar `AIUnavailable` y `AIError`.
15. No cargar Whisper al startup: debe permanecer lazy para no penalizar el
    arranque.
16. Agregar o actualizar tests para cualquier cambio de comportamiento.
17. Verificar primero con `pytest -q`, `git diff --check` y una validación del
    dashboard si el cambio es visual.

### Riesgos y límites actuales

- El dashboard no tiene autenticación HTTP.
- El dashboard depende de Chart.js desde CDN.
- El estado de recordatorios pendientes vive sólo en memoria.
- El scheduler corre dentro del mismo proceso que FastAPI.
- El proyecto usa un solo proceso Uvicorn en el flujo normal.
- No hay rate limiting.
- Los presupuestos, recurrentes y gastos comunes no se administran desde la
  web.
- La interfaz frontend está concentrada en un archivo Python grande.

Este archivo es documentación de arquitectura y operación. La fuente de verdad
del comportamiento siempre es el código y los tests del repositorio.
