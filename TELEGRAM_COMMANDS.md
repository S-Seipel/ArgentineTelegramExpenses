# Comandos de Telegram

El bot acepta mensajes en lenguaje natural para registrar gastos. También podés
usar estos comandos cuando necesitás una acción específica.

## Ayuda y consultas

| Comando | Uso |
|---|---|
| `/start`, `/help`, `/ayuda` | Muestra la ayuda del bot. |
| `/hoy` | Total gastado hoy. |
| `/semana` | Total gastado esta semana. |
| `/mes` | Total gastado este mes. |
| `/gastos` | Lista los últimos 10 gastos y sus IDs. |
| `/desglose` | Desglose por categoría del mes. |
| `/desglose_hoy` | Desglose por categoría de hoy. |
| `/desglose_semana` | Desglose por categoría de esta semana. |
| `/desglose_mes` | Desglose por categoría del mes. |
| `/analisis` | Opinión de la IA sobre tus gastos y posibles ajustes. |
| `/buscar <texto>` | Busca gastos por nombre. |

Ejemplo:

```text
/buscar supermercado
```

También podés preguntar en lenguaje natural, por ejemplo: `cuánto llevo en
comida`, `total del mes` o `cuál fue mi gasto más grande este mes`.

## Registrar y editar gastos

Para registrar gastos, escribí normalmente:

```text
gasté 10k en un café
hoy gasté 5k en café y 12k en Uber
ayer gasté 20 dólares en Steam
```

| Comando | Uso |
|---|---|
| `/borrar_ultimo` | Borra el último gasto registrado. |
| `/borrar <id>` | Borra el gasto indicado. |
| `/editar_ultimo <monto>` | Corrige el monto del último gasto. |
| `/editar_ultimo nombre: <texto>` | Corrige el nombre del último gasto. |
| `/editar <id> <monto>` | Corrige el monto de un gasto. |
| `/editar <id> nombre: <texto>` | Corrige el nombre de un gasto. |
| `/exportar` | Envía un CSV del mes en curso. |
| `/csv` | Alias de `/exportar`. |

Los montos aceptan formatos como `15000`, `15k`, `15 lucas` y `1.500,50`.
Para exportar otro período:

```text
/exportar hoy
/exportar semana
/exportar mes
/exportar todo
```

## Gastos recurrentes

```text
/recurrente_add Netflix 30000 15
/recurrente_add_anual Dominio 15000 3 15
```

`/recurrente_add` usa nombre, monto y día del mes. El comando anual agrega el
mes, expresado como número del 1 al 12.

| Comando | Uso |
|---|---|
| `/recurrentes` | Lista los recurrentes y sus IDs. |
| `/recurrente_del <id>` | Elimina un recurrente. |
| `/recurrente_off <id>` | Pausa un recurrente. |
| `/recurrente_on <id>` | Reactiva un recurrente pausado. |

## Presupuestos

```text
/presupuesto comida 50000
/presupuesto salidas 50 USD
```

El presupuesto es mensual por categoría. La moneda es opcional y, si no la
indicás, se usa la moneda predeterminada.

| Comando | Uso |
|---|---|
| `/presupuestos` | Lista los presupuestos y sus IDs. |
| `/presupuesto_del <id>` | Elimina un presupuesto. |
| `/presupuesto_off <id>` | Pausa un presupuesto. |
| `/presupuesto_on <id>` | Reactiva un presupuesto pausado. |

## Gastos fijos mensuales

Los gastos fijos se cargan una vez y después se marcan como pagados o salteados
cada mes.

```text
/gastofijo_add CASA 90000 10 transferencia
/gastofijo_add GYM 60000 15 efectivo
/gastofijo_add NETFLIX 5000 5 tarjeta
```

La sintaxis es `/gastofijo_add <nombre> <monto> [día] [método]`. El día es
opcional y por defecto es `1`. Métodos válidos: `transferencia`, `efectivo`,
`debito`, `tarjeta`, `credito`, `mercado pago`, `app` u `otro`.

| Comando | Uso |
|---|---|
| `/gastosfijos` | Muestra el estado de los gastos fijos del mes. |
| `/gastofijo_del <id>` | Elimina un gasto fijo. |
| `/gastofijo_off <id>` | Pausa un gasto fijo. |
| `/gastofijo_on <id>` | Reactiva un gasto fijo pausado. |
| `/pague <nombre>` | Marca un gasto fijo como pagado por el monto esperado. |
| `/pague <nombre> <monto_real>` | Marca como pagado con el monto real. |
| `/salte <nombre>` | Saltea ese gasto fijo durante el mes actual. |
| `/liberado` | Muestra ingreso, extra, gastos fijos y monto liberado. |
| `/repair_fixed_payments` | Repara espejos inconsistentes de pagos fijos. |

Ejemplo:

```text
/gastosfijos
/pague CASA
/pague GYM 65000
/salte NETFLIX
/liberado
```

`/liberado` conserva el cálculo mensual legacy y sus datos históricos.
`/ingreso` y `/extra` ahora alimentan el ciclo financiero, sin sincronizar
ese presupuesto anterior. Para el disponible del ciclo usá `/disponible`.

## Ciclo financiero F1

`/config_payday <día>` fija el día de cobro; el ciclo termina antes del
siguiente cobro. `/cycle` muestra el ciclo activo. `/disponible` y
`/gastar_diario` consultan el Money Engine, con ARS y USD independientes:
no hay conversión ni total combinado. El disponible puede ser negativo.

| Comando | Uso |
|---|---|
| `/config_payday <día>` | Configura el día de cobro. |
| `/cycle` | Muestra fechas y estado del ciclo. |
| `/ingreso <monto>` | Fija el ingreso base del ciclo. |
| `/extra <monto> [nota]` | Registra ingreso extra. |
| `/target <porcentaje o monto>` | Define la meta de ahorro. |
| `/reservar <monto>` | Fija el ahorro reservado. |
| `/ahorro_realizado <monto> [nota]` | Registra ahorro realizado. |
| `/disponible` | Muestra el disponible del ciclo. |
| `/gastar_diario` | Muestra el gasto diario seguro, si está disponible. |
| `/repair_fixed_payments` | Repara espejos de pagos fijos y muestra el resultado. |

El dashboard local contiene **Ciclo activo**, separado por moneda. La API
`GET /api/cycle-history` expone sólo ciclos cerrados desde sus snapshots,
con gasto diario histórico `null`. Un saldo cero se muestra como cero.
Cuando no hay ciclo para hoy, **Abrir o actualizar ciclo** permite abrirlo o
actualizarlo explícitamente. Las inversiones se gestionan fuera de la app.

## Compromisos recurrentes F1.5

| Comando | Uso |
|---|---|
| `/recurrente_obligacion <id> on` | Descuenta el compromiso pendiente de ese recurrente del disponible. |
| `/recurrente_obligacion <id> off` | Lo deja como recordatorio, sin reserva de dinero. |

Los recurrentes existentes y nuevos empiezan como **solo recordatorio**.
`/recurrentes` muestra cuáles son obligaciones. Un vencimiento atrasado se
conserva una sola vez, sin acumular cuotas de los meses omitidos. Responder
**sí** registra el pago sin duplicar el gasto; **no** salta la ocurrencia y
libera la reserva de esa ocurrencia. Resolver una atrasada puede dejar visible
el siguiente vencimiento del ciclo. **Posponer** conserva el vencimiento y el dinero reservado,
aunque el aviso pase al ciclo siguiente. Pausar también deja de reservar.
Al cambiar de ciclo se conserva un solo vencimiento atrasado por recurrente:
las previsiones del ciclo anterior no se acumulan como cuotas adeudadas.

El dashboard permite activar o quitar compromisos en **Recurrentes**.
La API los lista en `GET /api/recurring` y modifica el indicador mediante
`PATCH /api/recurring/{id}/obligation` con `{"is_obligation": true|false}`.

Los nombres y IDs se obtienen de `/gastosfijos`. Para modificar o eliminar
recurrentes, presupuestos o gastos fijos, primero listalos para consultar el
ID correcto.

## Dashboard

Con la aplicación levantada, el dashboard está disponible en:

```text
http://localhost:8000/dashboard
```
