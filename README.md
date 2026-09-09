# Telegram Expenses

[![CI](https://github.com/S-Seipel/ArgentineTelegramExpenses/actions/workflows/ci.yml/badge.svg)](https://github.com/S-Seipel/ArgentineTelegramExpenses/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Bot personal de Telegram para registrar y consultar gastos en lenguaje natural, usando un LLM local (Ollama) y PostgreSQL.

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
- Usa IA **local** vía [Ollama](https://ollama.com), no envía tus datos a la nube.
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
│   ├── service.py       # OllamaAIService + StubAIService + retry de JSON
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

OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_MODEL=qwen3:4b

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

### Gastos recurrentes

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

Los tests **no necesitan Ollama corriendo** ni PostgreSQL: usan SQLite en memoria y un `StubAIService` que devuelve respuestas guionadas.

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

- Toda la IA corre local con Ollama, nada sale a internet.
- Postgres **no** se expone al host (`127.0.0.1:5432`).
- Ollama **no** se expone al host.
- El bot sólo responde a tu `TELEGRAM_ALLOWED_USER_ID`.
- No se loggean mensajes ni montos en producción.

---

## 14. Licencia

MIT.
