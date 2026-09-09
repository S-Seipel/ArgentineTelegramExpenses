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
| `/ingreso <monto>` | Define el ingreso del mes actual. |
| `/extra <monto>` | Define el ingreso extra del mes actual. |
| `/liberado` | Muestra ingreso, extra, gastos fijos y monto liberado. |

Ejemplo:

```text
/gastosfijos
/pague CASA
/pague GYM 65000
/salte NETFLIX
/ingreso 1100000
/extra 75000
/liberado
```

Los nombres y IDs se obtienen de `/gastosfijos`. Para modificar o eliminar
recurrentes, presupuestos o gastos fijos, primero listalos para consultar el
ID correcto.

## Dashboard

Con la aplicación levantada, el dashboard está disponible en:

```text
http://localhost:8000/dashboard
```
