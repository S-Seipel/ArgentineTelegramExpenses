# Herramientas del roadmap — implementación y verificación

Fecha: 2026-11-02.

Se entregaron OCR local de tickets con confirmación e idempotencia,
historial durable para deshacer, ubicación opcional reversible, acceso
multiusuario explícito, preferencias de idioma/voz/zona, síntesis local de
audio, métricas sin datos personales, rangos de fechas, posposición de avisos
y un clasificador conservador aprendido del historial propio.

El dashboard incluye los flujos de ticket, undo, preferencias, usuarios y
ubicaciones. Las búsquedas, presupuestos, comparaciones, proyecciones y
backups ya existentes se conservaron y quedaron identificados como
implementados en el roadmap conciliado.

## Datos y contratos

- `0018_expense_history_location` agrega coordenadas y un historial de
  cambios sin modificar las columnas anteriores. Snapshots con importes
  exactos y revisión protegen la reversión; hasta diez acciones quedan
  disponibles por usuario. Los espejos de pagos tienen su propio dueño.
- `0019_user_preferences` agrega perfiles con un propietario único,
  allowlist explícita, idioma ES/EN/PT, voz apagada inicialmente y zona IANA.
  La migración no enrola personas; startup inicializa sólo el propietario.
- Ambos downgrades rechazan datos que se perderían. Las restauraciones se
  ensayan sobre copias desechables y nunca reemplazan automáticamente la base real.
- Fotos y voz utilizan Tesseract, eSpeak NG y FFmpeg locales, con límites,
  timeouts, limpieza y concurrencia acotada. No se agregaron paquetes Python.
- Un ticket se convierte en gasto sólo tras confirmar. El source key es
  estable por mensaje Telegram o archivo web y usuario. Los reintentos no
  duplican ni restauran silenciosamente gastos borrados.
- Idiomas traducen estructuras conocidas del bot, preservando nombres,
  importes y prosa libre. El dashboard se mantiene en español.
- Fechas y avisos usan la zona de cada perfil; el scheduler verifica de nuevo
  acceso, horario e idioma antes de enviar y permite reintentar fallos.
- Los centavos ahora se muestran: `1234.56` se presenta como `1.234,56`;
  los enteros conservan su formato anterior. También se corrigieron una
  importación local que rompía totales no vacíos y el acceso al contexto de
  exportación CSV desde un Update real.

## Validación

- **1.069 pruebas normales PASS** con Python 3.12 en la imagen final.
- **66 pruebas PostgreSQL PASS**, más los chequeos financieros R1/R2/R3.
- Los **107 archivos** empaquetados coinciden con la fuente revisada y los
  **60 paquetes Python** conservan las versiones de la base anterior.
- La apertura concurrente de ciclos se verificó en diez rondas de doce
  solicitudes. Se corrigió la carrera entre las dos lecturas y la reutilización
  de un estado OPEN cacheado; cuatro regresiones ARS/USD conservan también
  la prohibición de reabrir un período cerrado manualmente.
- La copia aislada de la instalación pasó `0017 → 0019 → 0017 → 0019`, con
  las columnas y registros anteriores idénticos. El bootstrap del propietario,
  el calendario del primer viernes y las consultas de API/UI/métricas se
  verifican sin crear movimientos financieros.

La versión se verifica con la suite normal en Python 3.12, PostgreSQL 15
aislado, regresiones de migración/concurrencia, OCR y audio nativos sin red,
flujos completos de Telegram/API y navegador con datos sintéticos, y ensayo
sobre una copia verificada de la instalación antes de aplicar el cambio.

El OCR nativo leyó un ticket sintético de 3.750,00 ARS y produjo borrador sin
escrituras. La síntesis generó audio Opus mono de 48 kHz en los tres idiomas.
El navegador verificó corrección de total/fecha, reintento sin duplicados,
deshacer, guardar/quitar/restaurar ubicación, preferencias y acceso de un
usuario sintético. Viewport de 390 px sin desbordes y consola sin errores.

Las pruebas guardan datos y evidencias sintéticas fuera del repositorio.
Los backups y credenciales reales permanecen fuera de Git.
