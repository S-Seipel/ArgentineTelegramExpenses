# Telegram Expenses

[![CI](https://github.com/S-Seipel/ArgentineTelegramExpenses/actions/workflows/ci.yml/badge.svg)](https://github.com/S-Seipel/ArgentineTelegramExpenses/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Bot personal de Telegram para registrar y consultar gastos en lenguaje natural, usando MiniMax u Ollama y PostgreSQL.

```
Usuario: "gasté 10k en un café"
Bot:    "☕ Gasto registrado
         Café
         💰 $10.000 ARS
         📂 Categoría: Café
         📅 Fecha: 19/08/2026"
```

---

## 1. ¿Qué hace este proyecto?

- Registra gastos desde Telegram en lenguaje natural.
- Detecta múltiples gastos en un solo mensaje.
- Responde consultas (total del día, semana, mes, por categoría, gasto más grande, últimos N gastos, etc.).
- Usa IA configurable: MiniMax vía API o [Ollama](https://ollama.com) local.
- Almacena todo en PostgreSQL con SQLAlchemy 2.x.
- Aísla al usuario permitido vía `TELEGRAM_ALLOWED_USER_ID`.

---

## 2. Arquitectura

```
app/
├── main.py              # FastAPI + lifespan del bot
├── config/settings.py   # Configuración tipada (pydantic-settings)
├── bot/
│   ├── app.py           # Build de la PTB Application + router
│   ├── handlers.py      # Handlers de comandos y mensajes
│   ├── service.py       # Orquestación: AI -> validación -> DB -> respuesta
│   └── specs.py         # /hoy /semana /mes /gastos -> QuerySpec
├── ai/
│   ├── service.py       # MiniMax/Ollama + StubAIService + retry de JSON
│   ├── schemas.py       # Pydantic models (ExpenseExtraction, Intent, QueryIntent)
│   └── prompts.py       # Prompts estrictos (la IA NUNCA genera SQL)
├── expenses/
│   ├── models.py        # SQLAlchemy Expense
│   ├── repository.py    # CRUD / sumas / listados
│   ├── service.py       # Validación de drafts (montos, monedas, nombres)
│   └── schemas.py       # DTOs internos (ExpenseCreate, ExpenseSummary)
├── queries/
│   ├── intents.py       # AI Intent -> QuerySpec (SQL parametrizado)
│   └── service.py       # Construcción segura de queries
├── categories/
│   └── categories.py    # Árbol de categorías (mutable en caliente)
├── database/database.py # Engine + sesión
└── utils/
    ├── formatting.py    # $10.000 ARS, iconos por categoría
    └── dates.py         # Parser de "hoy", "ayer", "lunes", etc.

tests/                   # pytest (corre sin Ollama)
alembic/                 # Migraciones
docker-compose.yml       # app + postgres
Dockerfile
```

**Regla clave:** la IA nunca toca la DB ni genera SQL. Sólo devuelve JSON estructurado validado con Pydantic. Si la IA se rompe, la app sigue funcionando con respuestas amigables.

---

## 3. Requisitos

- Docker + Docker Compose
- Ollama instalado y corriendo en tu Mac (o Linux). Descargar de https://ollama.com
- Modelo `qwen3:4b` (o el que prefieras) bajado localmente
- Un bot de Telegram (token) y tu `user_id` de Telegram

---

## 4. Instalación paso a paso

### 4.1 Clonar el proyecto

```bash
git clone <repo-url> telegram-expenses
cd telegram-expenses
cp .env.example .env
```

### 4.2 Crear el bot de Telegram y obtener tu `user_id`

1. Hablar con [@BotFather](https://t.me/BotFather), enviar `/newbot` y seguir los pasos. Te dará un token con formato `123456789:ABCdef...`.
2. Para conocer tu `user_id` escribile a [@userinfobot](https://t.me/userinfobot) o [@RawDataBot](https://t.me/RawDataBot). Te responderá con un número entero.

### 4.3 Instalar Ollama

En macOS:

```bash
brew install ollama
ollama serve        # Lo deja corriendo (o usar la app de Ollama)
```

Verificá:

```bash
curl http://localhost:11434/api/version
```

### 4.4 Bajar el modelo

```bash
ollama pull qwen3:4b
```

Si querés usar otro modelo (`llama3.1:8b`, `mistral`, `gemma2`, etc.) editá `OLLAMA_MODEL` en `.env`.

### 4.5 Configurar `.env`

```ini
TELEGRAM_BOT_TOKEN=123456789:ABCdef...
TELEGRAM_ALLOWED_USER_ID=987654321

DATABASE_URL=postgresql+psycopg://expenses:expenses@postgres:5432/expenses

AI_PROVIDER=minimax
MINIMAX_BASE_URL=https://api.minimax.io/v1
MINIMAX_API_KEY=tu_subscription_key
MINIMAX_MODEL=MiniMax-M3

TIMEZONE=America/Argentina/Buenos_Aires

APP_ENV=development
LOG_LEVEL=INFO
```

> `host.docker.internal` ya está mapeado desde el contenedor a tu Mac.
> Si corrés Ollama en otra máquina/VM, reemplazá por su IP.

### 4.6 Levantar la app + Postgres

```bash
docker compose up -d --build
```

Esto crea la red, levanta Postgres y construye el contenedor de la app.

### 4.7 Ejecutar las migraciones

```bash
docker compose exec app alembic upgrade head
```

La migración `0001_create_expenses` crea la tabla `expenses` con todos los índices.

---

Con `AI_PROVIDER=minimax`, la clave debe ser la **Subscription Key** del Token
Plan de MiniMax. No la subas al repositorio.

## 5. Uso

Escribile a tu bot desde Telegram:

### Registrar gastos

| Mensaje | Resultado |
|---|---|
| `gasté 10k en un café` | ☕ Café — $10.000 ARS — Café |
| `ayer gasté 20 dólares en Steam` | 🎮 Steam — US$ 20 USD |
| `hoy gasté 5k en café y 12k en Uber` | ☕ + 🚗 — Total $17.000 ARS |
| `gasté en McDonald's` | 🤔 ¿Cuánto gastaste en McDonald's? |

Acepta jerga argentina: `10k`, `10 lucas`, `10 mil`, `$10.000`, `10,000`, `20 dólares`, `usd 20`.

### Consultas

| Mensaje | Resultado |
|---|---|
| `cuánto gasté hoy` | Total del día en ARS |
| `total del mes` | Total del mes en curso |
| `cuánto llevo en comida` | Total filtrado por categoría |
| `cuál fue mi gasto más grande este mes` | Top 1 del mes |
| `mostrame mis últimos 10 gastos` | Lista los últimos N |

### Comandos

- `/start` o `/help` — mensaje de ayuda
- `/hoy`, `/semana`, `/mes` — totales rápidos
- `/gastos` — últimos 10 gastos
- `/desglose` — desglose por categoría del mes en curso (también `/desglose_hoy`, `/desglose_semana`, `/desglose_mes`)
- `/borrar_ultimo` — borra el último gasto registrado
- `/borrar <id>` — borra un gasto por ID (los IDs salen de `/gastos`)
- `/editar_ultimo <monto>` — corrige el monto del último gasto (acepta `15k`, `15 lucas`, `1.500,50`)
- `/editar_ultimo nombre: <texto>` — corrige el nombre del último gasto
- `/editar <id> <monto>` — corrige el monto de un gasto por ID
- `/exportar` — envía un CSV con los gastos del mes (acepta `hoy`, `semana`, `todo` como argumento)
- `/recurrente_add <nombre> <monto> <día>` — registra un gasto recurrente mensual
- `/recurrente_add_anual <nombre> <monto> <mes> <día>` — recurrente anual
- `/recurrentes` — lista los recurrentes activos
- `/recurrente_off <id>` / `/recurrente_on <id>` — pausa / reanuda
- `/recurrente_del <id>` — elimina un recurrente
- `/buscar <texto>` — busca gastos por nombre (ej: `/buscar starbucks`)
- `/presupuesto <categoría> <monto> [moneda]` — límite mensual por categoría (avisa al 80% y al 100%)
- `/presupuestos` — lista presupuestos activos
- `/presupuesto_del <id>` / `/presupuesto_off <id>` / `/presupuesto_on <id>` — gestión

### Gastos fijos mensuales

Tu Excel de gastos fijos vive ahora en el bot. Cargas tus gastos mensuales
y los vas tildando a medida que los pagás.

```
/gastofijo_add CASA 90000 10 transferencia
/gastofijo_add GYM 60000 15 efectivo
/gastofijo_add NETFLIX 5000 5 tarjeta
```

**Ver el estado del mes:**
```
/gastosfijos
→ 💸 Gastos fijos — 2026-09
  📊 Progreso: 5/12 pagados

  ✅ `1` CASA — $ 90.000 ARS (TRANSFERENCIA, día 10)
  💰 `2` GYM — $ 60.000 ARS → $ 65.000 (+5.000) (EFECTIVO, día 15)
  ⏳ `3` NETFLIX — $ 5.000 ARS (TARJETA, día 5)
```

**Marcar como pagado:**
```
/pague CASA              # al monto esperado
/pague GYM 65000         # si pagaste distinto
/salte NETFLIX           # este mes no lo pagás
```

**Ingresos y disponible del ciclo:**
```
/ingreso 1100000
/extra 75000
/disponible
```

`/liberado` es un alias de `/disponible`: ambos muestran el ciclo financiero.
`/ingreso` y `/extra` alimentan ese ciclo. Los importes del presupuesto
anterior se conservan en `monthly_budget_legacy`, sin nuevas escrituras ni
copias automáticas a ingresos del ciclo. El histórico se consulta con
`GET /api/month-summary?month_year=2026-09` (ARS, `read_only: true`).

El resumen visual mensual está en `/dashboard/mensual`. Abre el último mes
cerrado y permite recorrer los meses con los botones anterior/siguiente.

### Tickets, deshacer y preferencias

El panel **Herramientas** del dashboard permite leer tickets JPEG/PNG,
revisar nombre, total, moneda y fecha, y confirmar antes de guardar.
La lectura es local: la foto no se almacena en la base ni se manda a otro
proveedor. Si el OCR no distingue un total, tenés que indicarlo. Se registra
un gasto por ticket, sin sumar ítems por suposición. Reintentar una
confirmación o subir el mismo archivo no duplica el gasto de ese usuario.
Los borradores duran 15 minutos y se pierden al reiniciar; los gastos
confirmados permanecen en PostgreSQL.

También podés enviar la foto por Telegram y usar los comandos que devuelve:
`/ticket_confirmar <borrador> [total corregido] [moneda]` o
`/ticket_cancelar <borrador>`. Mandar la foto crea sólo un borrador.

- `/undo` o `/deshacer`: revierte la última edición, eliminación o ubicación
  de un gasto normal. Conserva hasta diez acciones pendientes por usuario y
  sobrevive reinicios. Si el gasto cambió por fuera de esa secuencia, rechaza
  la reversión. Los pagos fijos/recurrentes usan sus propios flujos.
- `/ubicacion <ID>`: selecciona un gasto para asociarle la ubicación que
  compartas después en ese mismo chat. La selección vence a los 15 minutos.
  `/ubicacion <ID> quitar` la elimina; esa acción también se puede deshacer.
  El dashboard muestra las coordenadas y un mapa local sin enviar datos a mapas externos.
- `/idioma es|en|pt`: cambia ayuda y respuestas estructuradas del bot.
  Los nombres e importes no se traducen. La prosa libre de IA y el dashboard
  conservan su idioma original.
- `/voz on|off`: activa audio además del texto, generado localmente con
  eSpeak NG y FFmpeg. La voz empieza apagada; si falla, el texto permanece.
- `/zona America/Argentina/Buenos_Aires`: cambia la zona horaria del usuario.
  Los avisos salen entre las 09:00 y 09:30 de su zona, verificando acceso y
  preferencias antes de enviar. Se pueden posponer con **mañana**,
  **en 3 días** o **el viernes que viene**, sin cambiar la identidad de la deuda.
- `/rango 2026-03-05 2026-03-15 [moneda]`: consulta ambas fechas inclusive.
  También entiende consultas como *cuánto gasté del 5 al 15 de marzo* por
  texto o audio. Sin año explícito usa el año actual.
- `/clasificador`: informa cuántos ejemplos propios conoce. Un nombre con
  al menos tres ejemplos unánimes permite registrar un gasto simple sin
  esperar al modelo; los casos nuevos o ambiguos siguen el flujo habitual.

El propietario conserva el ID configurado en `TELEGRAM_ALLOWED_USER_ID`.
`/usuario_add <ID>` autoriza una persona, `/usuario_off <ID>` pausa su acceso
sin borrar sus registros y `/usuarios` lista los activos. Cada persona
maneja sus datos en su chat de Telegram; sólo el propietario administra la
lista. El dashboard sigue siendo privado y local del propietario. No se
autoriza a nadie más durante una actualización.

La API local agrega `/api/preferences`, `/api/users`, `/api/undo`,
`/api/locations`, `PATCH /api/expenses/{id}/location`,
`POST /api/receipts/preview` (imagen binaria JPEG/PNG) y
`POST /api/receipts/confirm`. `/metrics` expone contadores y latencias en el
[formato de Prometheus](https://prometheus.io/docs/instrumenting/exposition_formats/),
sin IDs, textos, importes ni coordenadas como etiquetas. Los contadores se
reinician con el proceso.

Al actualizar ejecutá `alembic upgrade head` y reconstruí la imagen para
instalar los motores locales de OCR/voz. Las migraciones nuevas son
`0018_expense_history_location` y `0019_user_preferences`; conservan los
registros anteriores y bloquean un downgrade que descartaría historial o
preferencias. Hacé un backup verificado antes de actualizar.

### Planificación por ciclo financiero (F1–F3)

Configurá un día fijo con `/config_payday <día>` o el primer viernes de
cada mes con `/config_payday primer_viernes`. El ciclo va de un
cobro al siguiente; `/cycle` muestra sus fechas y montos. `/ingreso` fija el
ingreso base, `/extra` registra ingresos adicionales, `/target` define la meta
de ahorro, `/reservar` aparta ahorro y `/ahorro_realizado` registra lo ahorrado.
`/disponible` y `/gastar_diario` leen el Money Engine para el ciclo activo.
Cambiar `/config_payday` conserva el ciclo actual y el historial; se aplica
al siguiente ciclo mediante un período de transición continuo, sin huecos
ni solapamientos.
La regla del primer viernes usa el calendario real: por ejemplo,
6 de noviembre → 3 de diciembre de 2026. El primer viernes siguiente es
el 4 de diciembre; el historial conserva la regla con la que abrió cada ciclo.
El panel **Ciclo activo → Configurar calendario de cobro** permite guardar
la misma configuración sin usar Telegram. Guardar el calendario no abre
un ciclo ni registra ingresos; los montos se cargan por separado.
La API `GET/PATCH /api/settings/payday` consulta y guarda `pay_rule`:
`fixed_day` con `payday_day` de 1 a 31, o `first_weekday` con
`payday_weekday` de 0 (lunes) a 6 (domingo). El primer viernes es
`{"pay_rule":"first_weekday","payday_weekday":4}`; su ordinal siempre es 1.
Las instalaciones existentes deben ejecutar `alembic upgrade head` para
aplicar todas las migraciones hasta `0019_user_preferences` antes de iniciar
esta versión.
El disponible descuenta ahorro comprometido, gastos realizados y obligaciones
fijas pendientes; puede ser negativo. ARS y USD se muestran por separado,
sin conversión ni suma entre monedas.

`/repair_fixed_payments` revisa y repara los espejos
de pagos fijos inconsistentes; informa si no había nada que reparar.

El dashboard local y privado incluye **Ciclo activo** con los valores del
Money Engine por moneda. `GET /api/cycle-history` devuelve únicamente ciclos
cerrados desde sus snapshots inmutables; el gasto diario histórico es `null`.
Si no hay ciclo para hoy, **Abrir o actualizar ciclo** lo crea o actualiza
mediante una acción explícita. Un disponible de cero se muestra como cero;
un gasto diario no disponible se identifica por separado. Las inversiones se
gestionan fuera de la aplicación.

Cada moneda puede tener un calendario distinto y un ciclo abierto propio:

```text
/config_payday primer_viernes ARS
/config_payday 15 USD
/ingreso 1100000 ARS
/ingreso 200 USD
/disponible ARS
/disponible USD
/gastar_diario USD
```

Sin moneda se usa ARS. El selector del panel **Ciclo activo** permite cambiar
entre ARS y USD para consultar el ciclo y guardar su calendario. Los ingresos,
ahorros, gastos y obligaciones se calculan únicamente en la moneda de ese
ciclo; cerrar o actualizar ARS no cierra USD. Los calendarios nuevos heredan
la configuración inicial en ARS hasta configurar su propia regla o abrir
su primer ciclo; desde ese momento conservan una configuración independiente.
La API acepta `?currency=USD` en las consultas de ciclos, su historial y las
acciones de apertura/actualización. `PATCH /api/settings/payday` y las
mutaciones de dinero aceptan `currency` en el cuerpo. Enviar un `cycle_id`
con una moneda diferente devuelve `409 cycle_currency_mismatch`.

La migración conserva los cierres antiguos con varias monedas y los marca
como históricos compartidos. Si encuentra un ciclo abierto con varias
monedas, se detiene antes de modificar el esquema: ese ciclo debe cerrarse
con la versión anterior para conservar su cálculo y snapshot completos.
No redistribuye importes ni cambia snapshots automáticamente. Hacé una copia
verificada antes de actualizar; el procedimiento de cierre se describe en
el informe de esta versión.

### Compromisos recurrentes (F1.5)

Cada recurrente nuevo o existente continúa como **solo recordatorio** hasta
que lo incluyas expresamente en la planificación:

```text
/recurrentes
/recurrente_obligacion 3 on
/disponible
/recurrente_obligacion 3 off
```

Con `on`, su vencimiento pendiente dentro del ciclo descuenta del disponible.
Una ocurrencia atrasada se conserva una sola vez, sin acumular automáticamente
cuotas de los meses omitidos. Confirmar el pago cambia lo pendiente por un
gasto real, sin contarlo dos veces. Responder **no** salta la ocurrencia y
libera su reserva. Resolver una deuda atrasada puede dejar visible
el siguiente vencimiento del ciclo. **Posponer** mueve el aviso, conserva la identidad del vencimiento y
no libera dinero, incluso al cruzar el fin del ciclo. Pausar o quitar la
obligación deja de reservar dinero. ARS y USD permanecen separados.

F1.5 ofrece una previsión del ciclo: en el siguiente conserva un único
vencimiento atrasado por recurrente. Las previsiones adicionales del ciclo
anterior no se convierten automáticamente en cuotas adeudadas acumuladas.
El historial cerrado conserva los importes previstos al momento del cierre.

El panel **Recurrentes** permite activar o quitar cada compromiso del
disponible. `GET /api/recurring` lista los recurrentes activos y
`PATCH /api/recurring/{id}/obligation` acepta `{"is_obligation": true|false}`.
Al actualizar una instalación existente ejecutá `alembic upgrade head` antes
de iniciar la app: F1.5 agrega la migración `0014_recurring_obligations`.

### Audios (voice notes)

Mandá un voice note y el bot lo transcribe con `faster-whisper` (Whisper real corriendo local) y lo procesa como texto:

```
🎤 "gasté diez lucas en un café"
→ ☕ Café — $10.000 ARS — Café
```

**Setup:** sin pasos extra. `faster-whisper` se instala con `pip install -r requirements.txt` y el modelo se descarga automáticamente la primera vez que mandes un voice (~150MB para `base`, queda cacheado en `huggingface_cache` volume).

Configurable vía `.env`:

```bash
WHISPER_MODEL_SIZE=base        # tiny|base|small|medium|large-v3
WHISPER_DEVICE=cpu            # cpu|cuda
WHISPER_COMPUTE_TYPE=int8     # int8|float16|float32
```

- `tiny` ≈ 75MB · más rápido, menos preciso
- `base` ≈ 150MB · balanceado (default, **recomendado**)
- `small` ≈ 460MB · más preciso, 2-3x más lento
- `large-v3` ≈ 1.5GB · mejor calidad, requiere GPU

Acepta voice notes de Telegram (`.ogg` Opus) y archivos de audio genéricos adjuntos (`.mp3`, etc).

### Gastos recurrentes

Podés crear un gasto recurrente por comando o por lenguaje natural:

```
/recurrente_add Netflix 30000 15
"gasto 30k en Netflix cada mes el día 15"
```

El bot te avisa a las **09:00 hora local** el día del vencimiento. Respondé:

- **sí** — registra el gasto hoy
- **no** — salta este período
- **posponer** — te avisa mañana
- **eliminar** — borra el recurrente

### Presupuestos mensuales

Definí un límite por categoría y el bot te avisa cuando te acercás o lo superás:

```
/presupuesto comida 50000       # ARS por default
/presupuesto salidas 200 USD    # moneda custom
/presupuestos                   # ver todos
/presupuesto_del 3              # borrar
```

El bot chequea después de cada gasto. Si tu gasto acumulado en esa categoría
supera el **80%** del límite te avisa con `⚠️ Cerca del límite`. Si pasa el
**100%**, te avisa con `🚨 Presupuesto superado`. Una sola notificación por mes
por categoría.

Cualquier otro texto lo interpreta el LLM, así que podés hablarle natural.

---

## 6. Tests

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pytest
```

Los tests normales **no necesitan Ollama corriendo** ni PostgreSQL: usan
SQLite en memoria y un `StubAIService` que devuelve respuestas guionadas.

### Suite de integración contra PostgreSQL real

Hay además `tests_postgres/`, una suite separada y opt-in que necesita
una base PostgreSQL real para verificar lo que SQLite no puede probar:

- la migración Alembic desde cero y su downgrade + re-upgrade (con la
  ausencia de server default sobre `source_type`);
- el índice único parcial sobre `(telegram_user_id, source_key)`;
- la semántica de `SELECT FOR UPDATE` y la concurrencia de `mark_paid`
  entre dos transacciones;
- el comportamiento de las claves foráneas;
- precisión numérica / Decimal;
- la resolución de categorías cuando existen duplicados jerárquicos.

```bash
TGE_PG_INTEGRATION=1 \
DATABASE_URL=postgresql+psycopg://expenses:expenses@localhost:5432/expenses \
.venv/bin/pytest tests_postgres -q
```

La CI tiene un job dedicado (`.github/workflows/ci-postgres.yml`) que
lo corre contra un service container de `postgres:15-alpine`. Incluye
comprobaciones de integridad y carreras entre operaciones simultáneas.

---

## 7. Cambiar el modelo de Ollama

1. Bajar el modelo nuevo:
   ```bash
   ollama pull llama3.1:8b
   ```
2. Editar `.env`:
   ```ini
   OLLAMA_MODEL=llama3.1:8b
   ```
3. Reiniciar:
   ```bash
   docker compose restart app
   ```

Modelos recomendados (probados con `qwen3:4b`): `qwen3:4b`, `qwen3:8b`, `llama3.1:8b`, `mistral:7b`, `gemma2:9b`. Modelos más pequeños (1b–3b) funcionan para gastos simples pero fallan con prompts complejos.

---

## 8. Agregar o modificar categorías

Editar `app/categories/categories.py` en la constante `DEFAULT_CATEGORIES`:

```python
DEFAULT_CATEGORIES: dict[str, list[str]] = {
    "Comida": [
        "Supermercado",
        "Restaurante",
        "Mascotas",     # nueva
    ],
    ...
}
```

Después reiniciar la app:

```bash
docker compose restart app
```

La IA siempre intenta usar una hoja válida de esta lista; si no encaja usa `"Otros"` automáticamente.

> Si querés agregar categorías dinámicamente sin tocar código, expongo `set_categories()` desde `app.categories.categories`. Llamalo desde un comando admin o agregá un endpoint futuro.

---

## 9. Estructura de la tabla `expenses`

| Columna            | Tipo            | Notas |
|--------------------|-----------------|-------|
| `id`               | `Integer` PK    | |
| `telegram_user_id` | `Integer`       | Indexado |
| `name`             | `String(255)`   | |
| `amount`           | `Numeric(18,2)` | `Decimal`, nunca `Float` |
| `currency`         | `String(8)`     | ISO-4217 (`ARS`, `USD`, ...) |
| `category`         | `String(64)`    | Hoja válida |
| `expense_date`     | `Date`          | |
| `original_message` | `String(4000)`  | |
| `ai_confidence`    | `Numeric(4,3)`  | 0–1 |
| `created_at`       | `DateTime(tz)`  | server-side |
| `updated_at`       | `DateTime(tz)`  | server-side |

Índices útiles: `(user_id, date)`, `(user_id, category)`, `(user_id, currency)`, `date`.

---

## 10. Crear una nueva migración

```bash
docker compose exec app alembic revision --autogenerate -m "add_new_field"
```

> Autogenerate funciona mejor con `target_metadata = Base.metadata` ya configurado en `alembic/env.py`.

```bash
docker compose exec app alembic upgrade head
```

---

## Backups locales

Los scripts de `scripts/` usan Python 3.11 o superior y el contenedor
PostgreSQL existente; no modifican los gastos para hacer una copia.
El destino predeterminado es
`~/Library/Application Support/telegram-expenses/backups`.

**Copia manual:**

```bash
python3 scripts/backup_database.py
```

La salida indica el archivo `.dump`, tamaño y SHA-256. Cada copia se crea
como archivo temporal, se valida con `pg_restore --list` y se publica junto
con un JSON de metadatos. Los directorios usan permisos `0700`; las copias,
metadatos y estado usan `0600`. No subas `.env`, dumps ni el directorio de
backups a Git.

**Modo automático e instalación en macOS:**

```bash
# Ejecutar una comprobación automática del corte diario, sin instalar un job.
python3 scripts/backup_database.py --scheduled

# Revisar el plan; no instala ni activa nada.
python3 scripts/install_backups.py

# Generar un plist para revisar antes de instalar.
python3 scripts/install_backups.py --output /tmp/telegram-expenses-backup.plist
plutil -lint /tmp/telegram-expenses-backup.plist

# Copiar el script y el LaunchAgent a ubicaciones persistentes, sin activarlo.
python3 scripts/install_backups.py --install

# Instalar y activar expresamente el LaunchAgent.
python3 scripts/install_backups.py --install --enable
```

El instalador fija el contexto Docker activo para que el trabajo use el
mismo entorno después de reiniciar sesión. El LaunchAgent
`com.telegram-expenses.database-backup` comprueba cada 15 minutos y al
iniciar sesión si corresponde ejecutar `backup_database.py --scheduled`.
El script permite **una copia automática por corte diario de las 03:00 de
Buenos Aires**, independientemente de la zona horaria de la Mac. Si el
equipo estaba dormido o Docker no estaba disponible, recupera el último
corte pendiente al volver a ejecutarse; no genera una copia por cada
comprobación ni acumula ejecuciones de todos los días omitidos.

Se conservan las **últimas siete copias automáticas válidas**; la retención
no elimina copias manuales. Se vuelve a verificar el SHA-256 antes de dar
por cumplido un corte y antes de aplicar retención. El archivo
`backup-status.json`, dentro del destino, registra el último resultado.
Los logs del LaunchAgent quedan en la carpeta hermana `logs`.

**Ensayo de restauración en una base descartable:**

Elegí una copia y compará `shasum -a 256` con el campo `sha256` del JSON
acompañante. Este ejemplo crea otro contenedor, sin publicar puertos ni
usar el volumen de la base real:

```bash
TGE_BACKUP="/ruta/a/la/copia.dump"
shasum -a 256 "$TGE_BACKUP"

docker run --detach --name telegram_expenses_restore_check \
  --env POSTGRES_USER=expenses \
  --env POSTGRES_PASSWORD=restore-check-only \
  --env POSTGRES_DB=expenses_restore \
  postgres:15-alpine

# Esperar a que esta comprobación indique que la base está lista.
docker exec telegram_expenses_restore_check \
  pg_isready --username expenses --dbname expenses_restore

docker exec -i telegram_expenses_restore_check \
  pg_restore --exit-on-error --no-owner --no-privileges \
  --username expenses --dbname expenses_restore < "$TGE_BACKUP"

docker exec telegram_expenses_restore_check \
  psql --username expenses --dbname expenses_restore \
  --command "SELECT version_num FROM alembic_version;"

# Eliminar únicamente el contenedor y volumen descartables de este ensayo.
docker rm --force --volumes telegram_expenses_restore_check
```

La automatización crea y valida copias; **no restaura ni reemplaza la base
real automáticamente**. Una restauración real requiere elegir la copia y
el destino por separado, después de validar el ensayo.

---

## 11. Problemas comunes

### `telegram.error.InvalidToken`

El token es incorrecto. Verificá que empiece con el número correcto y que lo copiaste entero (incluyendo `:` y el sufijo).

### `Ollama is unreachable at http://host.docker.internal:11434`

Ollama no está corriendo o no es accesible. En macOS la app de Ollama abre el listener sólo cuando la abrís; en Linux corré `ollama serve` en otra terminal.

### `Connection refused` al intentar guardar un gasto

El contenedor `app` no llega a `postgres`. Verificá `docker compose ps` y revisá los logs:

```bash
docker compose logs app
docker compose logs postgres
```

### Conectar desde Beekeeper Studio, TablePlus u otro cliente de PostgreSQL

Usá host `127.0.0.1`, puerto `15432`, usuario `expenses` y base `expenses`,
con la contraseña de PostgreSQL configurada en `docker-compose.yml`.
Para esta conexión local, SSL y túnel SSH quedan desactivados.

El puerto `5432` de la Mac puede pertenecer a otra instalación de PostgreSQL.
Si aparece `role "expenses" does not exist`, verificá que el cliente use
`127.0.0.1:15432`. La app sigue conectándose dentro de Docker a
`postgres:5432`; su `DATABASE_URL` no cambia.

### La IA se inventa montos

El modelo devolvió algo sin `needs_clarification`. El sistema valida: si falta el monto **no guarda nada** y pregunta. Si ves guardado algo raro, abrí un issue con el texto original y el JSON devuelto.

### Respuestas lentas

Los modelos locales tardan. El primer mensaje después de un reinicio de Ollama puede tardar **60–90 segundos** porque el modelo se carga en memoria. Subí `OLLAMA_TIMEOUT` (default 180s) si seguís viendo timeouts, o probá un modelo más chico (`qwen3:4b`, `llama3.2:3b`) si te molesta la latencia.

### Resetear la base

```bash
docker compose down -v
docker compose up -d
docker compose exec app alembic upgrade head
```

---

## 12. Variables de entorno

| Variable                    | Requerida | Default | Descripción |
|-----------------------------|-----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN`        | ✅ | — | Token de BotFather |
| `TELEGRAM_ALLOWED_USER_ID`  | ✅ | — | Tu user_id de Telegram |
| `DATABASE_URL`              | ✅ | `postgresql+psycopg://expenses:expenses@postgres:5432/expenses` | |
| `OLLAMA_BASE_URL`           | ✅ | `http://host.docker.internal:11434` | Endpoint HTTP de Ollama |
| `OLLAMA_MODEL`              | ✅ | `qwen3:4b` | Modelo a usar |
| `OLLAMA_TIMEOUT`            | ❌ | `180` | Segundos |
| `OLLAMA_MAX_RETRIES`        | ❌ | `2` | Reintentos si el JSON es inválido |
| `TIMEZONE`                  | ❌ | `America/Argentina/Buenos_Aires` | IANA tz |
| `APP_ENV`                   | ❌ | `development` | |
| `LOG_LEVEL`                 | ❌ | `INFO` | `DEBUG` / `INFO` / `WARNING` |
| `HOST`                      | ❌ | `0.0.0.0` | uvicorn host |
| `PORT`                      | ❌ | `8000` | uvicorn port |
| `MAX_MESSAGE_LENGTH`        | ❌ | `2000` | Caracteres máximos por mensaje |

---

## 13. Privacidad

- Con `AI_PROVIDER=ollama`, la interpretación del texto y la transcripción
  de voz son locales. Con `AI_PROVIDER=minimax`, los mensajes y el contexto
  de análisis se envían a MiniMax; la transcripción de voz sigue siendo local.
- Postgres se publica sólo en el loopback de la Mac (`127.0.0.1:15432`),
  sin acceso directo desde otros equipos.
- Ollama **no** se expone al host.
- El bot sólo responde a tu `TELEGRAM_ALLOWED_USER_ID`.
- No se loggean mensajes ni montos en producción.

---

## 14. Licencia

MIT.
