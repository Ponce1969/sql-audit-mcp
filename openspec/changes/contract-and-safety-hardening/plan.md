# Plan Formal de Implementación: Contract, Safety & Defensibility Hardening (`sql-audit-mcp`)

---

## 1. Contexto y Objetivo

### 1.1 Contexto
Durante las auditorías forenses y revisiones críticas del proyecto `sql-audit-mcp`, se identificaron discrepancias entre lo que la documentación (`README.md`, `SKILL.md`) afirma, lo que el código en Python implementa y lo que el motor relacional de PostgreSQL ejecuta físicamente. 

Se detectaron:
* Bugs algorítmicos concretos (riesgo de recursión infinita en el análisis de bloqueos).
* Falsos positivos y negativos en la comparación diferencial de evidencias (`AuditDiff`).
* Divergencias en umbrales por defecto y modelos de salida entre la CLI y el servidor MCP.
* Riesgos operacionales en la simulación activa de consultas (`EXPLAIN ANALYZE`) y en sugerencias textuales que aconsejan operaciones bloqueantes (`VACUUM FULL`).
* Afirmaciones documentales sobrevendidas (complejidad $O(1)$, medición "exacta" de bloat, remediación automática).

### 1.2 Principio Rector
> **Evidence over Assumptions.**
> Ningún cambio funcional ni afirmación técnica se implementará si no está respaldado por evidencia directa en el código fuente, en la especificación formal de PostgreSQL o en pruebas reproducibles.

### 1.3 Objetivo de esta Fase
Establecer un plan de implementación formal, ordenado y autocontenido que separe de forma estricta:
1. Decisiones estratégicas que corresponden exclusivamente al propietario del proyecto.
2. Corrección de bugs y riesgos técnicos confirmados.
3. Cambios condicionados a las decisiones del propietario.
4. Mejoras documentales y de UX.
5. Deuda técnica postergable sin impacto funcional inmediato.

### 1.4 Fuera del Alcance
* No se realizarán refactors estéticos ni campañas masivas de reescritura de tipos sin justificación funcional.
* No se modificará la formulación matemática de las consultas de bloat en SQL (ampliamente probadas en la industria).
* No se inventarán valores de configuración ni umbrales canónicos sin la previa autorización del propietario.

---

## 2. Decisiones de Producto y Contrato (Fase 0)

Las siguientes decisiones son precondición obligatoria antes de iniciar cualquier cambio en el código:

### 2.1 Decision Record — T-01: EXPLAIN ANALYZE

#### 2.1.1 Base de Evidencia Fáctica
Para fundamentar esta decisión bajo el principio *Evidence over Assumptions*, se separan los hechos observados en el repositorio de los factores externos y las inferencias:

* **Comportamiento demostrado por el código:**
  * En [`audit_pg.py:317-336`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L317-L336), `get_query_plan` ejecuta consultas con `EXPLAIN (ANALYZE, COSTS, VERBOSE, BUFFERS, FORMAT JSON) {query};` rodeadas por `BEGIN;` y `ROLLBACK;`.
  * En [`mcp_pg_auditor.py:256-268`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/mcp_pg_auditor.py#L256-L268), `get_query_plan` usa una transacción asíncrona (`conn.transaction()`) con `try ... finally: await tr.rollback()`.
  * Ninguno de los dos módulos establece `statement_timeout` ni `lock_timeout` en la sesión de la base de datos antes de ejecutar la sentencia analizada.
  * Ninguno de los adaptadores valida sintaxis ni verifica la presencia de múltiples sentencias concatenadas en el parámetro `query`.
* **Comportamiento demostrado por tests:**
  * La suite actual solo valida la función de dominio [`analyze_execution_plan`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/plan_service.py#L14) en [`tests/test_plan_engine.py`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/tests/test_plan_engine.py#L33) pasando estructuras de diccionarios en memoria.
  * Ningún test de la suite ejecuta `get_query_plan` contra un motor PostgreSQL ni valida el comportamiento transaccional de `BEGIN/ROLLBACK`, el manejo de excepciones o la inyección de sentencias.
* **Comportamiento externo de PostgreSQL (Motor Relacional):**
  * `EXPLAIN ANALYZE` ejecuta físicamente la sentencia en el motor.
  * El bloque `BEGIN ... ROLLBACK` revierte únicamente las mutaciones transaccionales sobre las tuplas de datos (heap tuples en tablas modificadas por DML). No revierte ni anula efectos que persisten fuera de esa semántica transaccional:
    * No revierte el avance de secuencias (`nextval` consumidos quedan permanentemente descartados).
    * No anula efectos secundarios externos de funciones clasificadas como `VOLATILE` (ej. llamadas a APIs externas vía extensiones de red o PLs).
    * Adquiere bloqueos de tabla y fila reales durante la ventana de ejecución de la consulta. Si un lock entra en conflicto con transacciones concurrentes, compite y encola sesiones productivas; sin un timeout explícito, la sesión del auditor espera indefinidamente.
    * Genera escritura de registros WAL y bloat temporal en índices/tablas durante la ejecución previa al rollback.
    * Consume CPU, memoria de trabajo (`work_mem`) y cuota de I/O en el servidor productivo de forma proporcional al costo real de la consulta.
* **Inferencias:**
  * Inferencia operacional: que usuarios o modelos LLM integrados vía MCP envíen sentencias DML masivas o DDL inadvertidamente a `pg_explain` en entornos de producción.
  * Inferencia de driver: que cadenas multi-sentencia separadas por `;` puedan desacoplar el plan analizado de sentencias posteriores en `psycopg2` (posibilidad teórica no probada con tests en el repo).

---

#### 2.1.2 Comparativa de Opciones de Diseño

##### Opción A — Auditoría estrictamente pasiva
Eliminar la capacidad de ejecutar `EXPLAIN ANALYZE` y conservar exclusivamente el análisis estático mediante `EXPLAIN` sin ejecución.

1. **Qué capacidad conserva:**
   * Análisis de costo planificado (`Startup Cost`, `Total Cost`).
   * Detección de `Seq Scan` sobre tablas grandes basado en las estimaciones del optimizador.
   * Jerarquía y tipo de nodos del plan (joins, agregaciones, ordenamientos).
   * Generación de findings de optimización derivados de las estimaciones del catálogo.
2. **Qué capacidad se pierde:**
   * Métricas de tiempo real de ejecución (`Actual Startup Time`, `Actual Total Time`, `Execution Time`).
   * Conteo de filas reales procesadas (`Actual Rows` vs `Plan Rows`), perdiendo la capacidad de detectar divergencia de estimación del planificador (*estimation drift* o estadísticas desactualizadas).
   * Métricas de buffer I/O reales (`Shared Hit`, `Shared Read`, `Shared Dirtied`, `Shared Written`, `Temp Read/Written`).
3. **Qué riesgos elimina:**
   * Elimina la ejecución física de sentencias DML analizadas por parte del auditor.
   * Elimina el consumo de secuencias numéricas derivado del análisis.
   * Elimina la invocación de funciones con efectos colaterales externos durante la obtención del plan.
   * Elimina la adquisición de bloqueos de modificación (fila/tabla) por parte de la consulta suministrada.
   * Elimina el riesgo de que la herramienta quede retenida esperando cerrojos de modificación durante la inspección de planes.
   * Elimina la necesidad de implementar y mantener lógica de timeouts transaccionales en los clientes.
4. **Qué riesgos permanecen:**
   * La fase de optimización/planificación de `EXPLAIN` continúa consumiendo CPU y memoria en el motor.
   * En consultas con un número masivo de joins, el cálculo del árbol de costos puede demorar tiempo perceptible.
   * El cliente sigue requiriendo una conexión activa a la base de datos (`max_connections`).
5. **Qué cambios técnicos serían necesarios:**
   * En [`audit_pg.py`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py): eliminar argumento `--analyze` de argparse (`L730-731`), remover el parámetro `analyze` de `get_query_plan` y `run_plan_audit`, y suprimir el bloque condicional `BEGIN ... ROLLBACK`.
   * En [`mcp_pg_auditor.py`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/mcp_pg_auditor.py): suprimir el parámetro `analyze: bool = False` de la herramienta `pg_explain` y el bloque `conn.transaction()`.
   * En [`plan_service.py`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/plan_service.py): simplificar o deprecar el flag `is_analyzed` en [`PlanReport`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/domain/models.py#L38).
6. **Impacto en "Evidence over Assumptions" y carácter observacional:**
   * *Carácter observacional:* Limita la interacción a la inspección de catálogos y cálculo estático de planes. Ofrece la garantía verificable de que la herramienta no ejecuta físicamente la sentencia suministrada para obtener el plan de ejecución.
   * *Evidence over Assumptions:* Limita la evidencia a las *estimaciones teóricas* del optimizador de PostgreSQL; no contrasta con *evidencia empírica real* si las estimaciones de costo coinciden con la ejecución física.
7. **Impacto sobre CLI y MCP:**
   * CLI: Se simplifica la interfaz de línea de comandos eliminando el flag `--analyze`.
   * MCP: Se reduce la superficie del tool `pg_explain` (se elimina el parámetro `analyze: bool`), haciéndolo predecible y seguro para invocación desatendida por LLMs.
8. **Tests necesarios:**
   * Ajustar [`tests/test_plan_engine.py`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/tests/test_plan_engine.py) para validar que el servicio opera correctamente sin depender de campos `Actual *` ni `Buffers`.
   * Test de CLI y MCP verificando que el parámetro `analyze` ya no existe ni es aceptado.
9. **Implicaciones para README.md y SKILL.md:**
   * Se debe declarar formalmente que el auditor es estrictamente pasivo / read-only.
   * Se deben remover todos los ejemplos y referencias a tiempos de ejecución reales o flags `--analyze`.

---

##### Opción B — Auditoría con capacidad activa controlada
Mantener `EXPLAIN ANALYZE`, delimitándolo explícitamente como una capacidad activa de simulación que requiere controles defensivos rigurosos.

1. **Qué capacidad conserva:**
   * Todas las capacidades de la Opción A.
   * Detección precisa de discrepancias entre filas estimadas y filas reales (*estimation skew*), clave para detectar tablas que requieren `ANALYZE` inmediato.
   * Medición exacta de tiempos de ejecución (`Execution Time`) y latencia de nodos.
   * Identificación de cuellos de botella de disco mediante métricas reales de lectura/escritura de buffers y uso de memoria temporal (`Temp`).
2. **Qué capacidad se pierde:**
   * Ninguna capacidad de análisis; amplía la profundidad diagnóstica sobre el plan de ejecución.
3. **Qué riesgos acota o mitiga:**
   * Mediante `statement_timeout` y `lock_timeout`: reduce y acota la duración máxima de bloqueos y consumo de recursos, previniendo sesiones colgadas indefinidamente ante contención de bloqueos o consultas de duración desmedida. No elimina el impacto transitorio ni la contención mientras los timeouts no hayan expirado.
   * Mediante validación estructural: reduce el riesgo de inyección o ejecución incidental de secuencias multi-sentencia concatenadas.
4. **Qué riesgos permanecen:**
   * Invasividad intrínseca: la sentencia analizada se ejecuta físicamente en el servidor de base de datos.
   * Consumo permanente de valores en secuencias (`nextval`) en sentencias DML con campos autoincrementales.
   * Ejecución irrevocable de funciones `VOLATILE` con llamadas o efectos colaterales fuera de la base de datos.
   * Impacto transitorio de concurrencia y encolamiento de locks: la sentencia analizada adquiere bloqueos reales durante su ventana de ejecución (acotada pero no nula), pudiendo degradar temporalmente transacciones productivas concurrentes.
   * Consumo de recursos físicos de la máquina (CPU, I/O, memoria) proporcional al costo real de la sentencia durante el lapso de ejecución.
5. **Qué cambios técnicos serían necesarios:**
   * En [`audit_pg.py`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py) y [`mcp_pg_auditor.py`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/mcp_pg_auditor.py):
     * Configurar obligatoriamente parámetros de sesión dentro del bloque transaccional:
       `SET LOCAL statement_timeout = '<ms>';` y `SET LOCAL lock_timeout = '<ms>';` antes de invocar `EXPLAIN (ANALYZE, ...)`.
     * Implementar validación sintáctica y estructural que asegure que la entrada corresponda estrictamente a una única sentencia SQL ejecutable.
     * Exponer flags opcionales con valores por defecto defensivos (ej. `--statement-timeout 5000`, `--lock-timeout 1000`).
     * Mostrar advertencias explícitas en consola y en el resultado MCP alertando sobre la ejecución física de la sentencia.
6. **Impacto en "Evidence over Assumptions" y carácter observacional:**
   * *Evidence over Assumptions:* Maximiza la fidelidad de la evidencia empírica: el reporte no se basa únicamente en lo que el optimizador *asume*, sino en lo que el hardware y el motor *ejecutaron físicamente*.
   * *Carácter observacional:* Rompe la frontera de inspección pasiva, transformando formalmente al auditor en una herramienta híbrida con un componente de ejecución activa controlada.
7. **Impacto sobre CLI y MCP:**
   * CLI: Mantiene `--analyze`, añadiendo parámetros de control defensivo (`--statement-timeout`, `--lock-timeout`) y mensajes de confirmación/aviso en salida estándar.
   * MCP: `pg_explain` mantiene `analyze: bool`, pero su especificación JSON debe describir taxativamente que `analyze=True` ejecuta la sentencia en el motor, advirtiendo sobre el impacto en DML y garantizando timeouts automáticos.
8. **Tests necesarios:**
   * Tests unitarios con mocks de DB-API verificando que cuando `analyze=True`, se ejecutan indefectiblemente las sentencias `SET LOCAL statement_timeout` y `SET LOCAL lock_timeout`.
   * Test de validación sintáctica verificando que sentencias multi-query sean rechazadas antes de llegar a la base de datos.
   * Tests de manejo de timeout (`QueryCanceled`, `LockNotAvailable`) asegurando que la herramienta retorne un finding o error estructurado y no un crash no capturado.
9. **Implicaciones para README.md y SKILL.md:**
   * Debe documentarse claramente la distinción entre inspección de catálogos (100% pasiva) y benchmarking de planes (`EXPLAIN ANALYZE`, capacidad activa).
   * Deben detallarse las salvaguardas técnicas incorporadas (timeouts locales, rollback automático) y los límites no mitigables (avance de secuencias, funciones externas).

---

#### 2.1.3 Resolución de la Decisión
* **Estado:** `RESOLVED`
* **Decisión del propietario:** `OPCION_A` (Auditoría estrictamente pasiva).
* **Definición formal:**
  1. Eliminar `EXPLAIN ANALYZE` del contrato de `sql-audit-mcp`.
  2. Mantener `EXPLAIN` estático como capacidad de análisis de planes.
  3. No introducir todavía ninguna implementación alternativa de benchmarking.
  4. Si en el futuro se necesita ejecución activa para benchmarking, deberá tratarse como una capacidad separada con una frontera de confianza explícita, no como un simple booleano dentro del auditor.

---

#### 2.1.4 Consecuencias Concretas para las Siguientes Fases
* **CLI (`audit_pg.py`):**
  * Eliminar el flag `--analyze` del parser de argumentos (`argparse`).
  * Eliminar el parámetro `analyze` de `get_query_plan` y `run_plan_audit`.
  * Eliminar el bloque condicional transaccional `BEGIN ... ROLLBACK` asociado a `analyze=True`.
* **MCP (`mcp_pg_auditor.py`):**
  * Eliminar el parámetro `analyze: bool = False` de la herramienta `pg_explain`.
  * Eliminar la lógica de transacciones asíncronas (`conn.transaction()`) asociadas a `analyze`.
  * Actualizar el schema del tool para declarar su naturaleza puramente estática.
* **Dominio (`models.py`, `plan_service.py`):**
  * Simplificar `PlanReport`: deprecar o eliminar `is_analyzed` (o fijarlo invariablemente en `False`).
  * Desactivar o ajustar la búsqueda de nodos `Actual Rows`, `Actual Total Time` o `Buffers` en el parsing del árbol de plan.
* **Tests:**
  * Remover cualquier fixture o test que espere que `analyze=True` ejecute transacciones.
  * Agregar test unitario para CLI y MCP confirmando que el flag/parámetro `analyze` ya no es reconocido.
  * Preservar los tests de `test_plan_engine.py` adaptándolos para validar el cálculo de findings basado únicamente en costos estimados y tipos de escaneo.
* **Documentación (`README.md` y `SKILL.md`):**
  * Ratificar al auditor como una herramienta 100% no invasiva / de solo lectura de catálogos y estimaciones de planes.
  * Remover cualquier referencia, comando de ejemplo o tabla de parámetros que mencione `--analyze` o métricas de tiempo real de ejecución de consultas.

### 2.2 Decision Record — T-02: Contrato Canónico CLI vs. MCP

#### 2.2.1 Pregunta de Diseño Fundamental
> ¿CLI y MCP representan dos interfaces del mismo auditor con un único contrato semántico canónico —debiendo compartir configuración, alcance y umbrales por defecto—, o constituyen adaptadores especializados con políticas operativas deliberadamente divergentes que deben documentarse y protegerse contractualmente como tales?

---

#### 2.2.2 Distinción de Capas y Responsabilidades
Para evaluar rigurosamente esta decisión, se delimitan cuatro dimensiones arquitectónicas:
1. **Contrato semántico común:** Las reglas del dominio que definen qué constituye una anomalía de salud en PostgreSQL, cómo se estructuran los hallazgos y evidencias (`AuditReport`), y la equivalencia conceptual del diagnóstico.
2. **Configuración canónica:** Los umbrales oficiales que rigen el alcance de la auditoría (`min_table_rows`, `min_size_bytes`, selección de esquemas por defecto) compartidos por el motor.
3. **Configuración específica del adaptador:** Parámetros de transporte o conexión propios de cada entorno (ej. flags de CLI como `--env-file` vs. argumentos MCP como `db_alias`, timeouts de conexión).
4. **Presentación / Transporte:** El formato final en el que se entrega el reporte (texto plano en consola, JSON estándar, `AuditReport` canónico, o payloads JSON-RPC/schemas Pydantic para el cliente MCP), siempre que no alteren silenciosamente la semántica de la auditoría.

---

#### 2.2.3 Análisis Forense de Diferencias Identificadas

A continuación se auditan las discrepancias concretas observadas entre el adaptador CLI ([`audit_pg.py`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py)) y el adaptador MCP ([`mcp_pg_auditor.py`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/mcp_pg_auditor.py)):

##### 1. Umbral de Filas Vivas (`min_table_rows`)
* **Código:** CLI default `10000` ([`audit_pg.py:784`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L784)); MCP default `1000` ([`mcp_pg_auditor.py:363`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/mcp_pg_auditor.py#L363)).
* **Tests:** [`tests/test_cli.py:57`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/tests/test_cli.py#L57) valida explícitamente `assert args.min_table_rows == 10000`. En MCP, el default está en la signatura pública del tool `pg_health_audit`.
* **Intención documentada:** Ninguna justificación en docs. En contradicción directa, el ejemplo de CLI en [`README.md:82`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/README.md#L82) ilustra `--min-table-rows 1000` (el valor de MCP).
* **Impacto en auditoría:** Una tabla con 5.000 filas con HOT roto es omitida por el CLI pero reportada como issue por MCP sobre la misma base de datos.
* **Clasificación:**
  * Demostrado por código: SÍ.
  * Demostrado por tests: SÍ.
  * Documentado como intención: NO (discrepancia no explicada).
  * Inferencia: Hipótesis de desarrollo en momentos distintos sin sincronización de constantes.
  * Decisión de producto pendiente: Definir el valor canónico unificado.

##### 2. Umbral de Tamaño Mínimo de Índice (`min_size` / `min_size_bytes`)
* **Código:** CLI default `0` bytes ([`audit_pg.py:734`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L734)); MCP default `10_000_000` bytes / 10 MB ([`mcp_pg_auditor.py:364`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/mcp_pg_auditor.py#L364)).
* **Tests:** [`tests/test_cli.py:49`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/tests/test_cli.py#L49) valida explícitamente `assert args.min_size == 0`.
* **Intención documentada:** No documentada. En [`README.md:82`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/README.md#L82), el ejemplo de CLI sugiere `--min-size 10000000`.
* **Impacto en auditoría:** Un índice redundante de 4 MB es reportado por CLI pero completamente ignorado por MCP.
* **Clasificación:**
  * Demostrado por código: SÍ.
  * Demostrado por tests: SÍ.
  * Documentado como intención: NO.
  * Inferencia: Posible conjetura de que en MCP se buscó reducir el tamaño de respuestas para asistentes, pero no existe evidencia documental ni comentarios en el código que confirmen dicha motivación.
  * Decisión de producto pendiente: Definir el valor canónico unificado.

##### 3. Esquemas Auditados por Defecto (`schemas`)
* **Código:** CLI default `None` ([`audit_pg.py:727`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L727)), lo que en SQL ejecuta las variantes `*_ALL_PSYCOPG` filtrando únicamente catálogos del sistema (`NOT IN ('pg_catalog', 'information_schema', 'pg_toast')`); MCP default `["public"]` ([`mcp_pg_auditor.py:371`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/mcp_pg_auditor.py#L371)).
* **Tests:** [`tests/test_cli.py:48`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/tests/test_cli.py#L48) valida `assert args.schema is None`. [`tests/test_mcp_auditor.py:292`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/tests/test_mcp_auditor.py#L292) valida `schemas_audited=["public"]`.
* **Intención documentada:** No explicada. [`README.md:121`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/README.md#L121) describe `pg_health_audit` como "Full evaluation of up to 6 performance anti-patterns" sin advertir que restringe el universo a `public`.
* **Impacto en auditoría:** En bases de datos multi-tenant o con esquemas dedicados (`auth`, `billing`, `analytics`), MCP ignora silenciosamente todas las tablas fuera de `public`, arrojando una divergencia de cobertura respecto a la CLI.
* **Clasificación:**
  * Demostrado por código: SÍ.
  * Demostrado por tests: SÍ.
  * Documentado como intención: NO.
  * Inferencia: MCP asumió `public` como convención simplificada en su diseño inicial.
  * Decisión de producto pendiente: Definir la regla canónica de alcance territorial por defecto.

##### 4. Resolución de Conexión / DSN
* **Código:**
  * CLI ([`audit_pg.py:657-695`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L657-L695)): acepta `--url`, `--env-file`, auto-descubre archivo `.env`, lee `DATABASE_URL` y resuelve variables estándar de libpq (`PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`).
  * MCP ([`mcp_pg_auditor.py:127-149`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/mcp_pg_auditor.py#L127-L149)): acepta `db_alias` para leer `DB_{ALIAS.upper()}_URL`, o `DATABASE_URL` / `POSTGRES_URL` si el alias es `default`. No resuelve variables libpq `PG*` ni procesa `--env-file`.
* **Tests:** [`tests/test_mcp_auditor.py:60-84`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/tests/test_mcp_auditor.py#L60-L84) cubre resolución por alias en MCP; [`tests/test_config.py`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/tests/test_config.py) cubre CLI.
* **Intención documentada:** [`README.md:22`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/README.md#L22) menciona "Zero-Trust Credential Security", pero no especifica la diferencia de capacidades entre ambos adaptadores.
* **Impacto en auditoría:** Pertenece a la capa de *Configuración específica del adaptador*; no altera la semántica de la auditoría una vez establecida la conexión.
* **Clasificación:** Demostrado por código y tests como diferencia de transporte/adaptador.

##### 5. Modelos de Salida (DTOs vs. Dominio Canónico)
* **Código:**
  * CLI retorna `DatabaseHealthReport` (dataclass interno), renderiza texto formateado o emite `AuditReport` canónico si se usa `--canonical-json`.
  * MCP retorna `PostgresHealthReport` (Pydantic BaseModel estructurado por listas de issues específicas). Posee un método `to_audit_report()`, pero **ninguna herramienta MCP lo invoca**; retorna directamente el DTO Pydantic.
* **Tests:** [`tests/test_canonical_contract.py:52-172`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/tests/test_canonical_contract.py#L52-L172) demuestra que ambos reportes pueden traducirse a `AuditReport` y producir exactamente los mismos `finding_id` y `evidence_id`.
* **Intención documentada:** [`README.md:19`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/README.md#L19) declara como pilar de arquitectura el "Canonical Domain Contract: Standardized AuditReport, Finding, and Evidence schema".
* **Impacto en auditoría:** Pertenece a la capa de *Presentación / Transporte*. Mientras el conjunto de hallazgos subyacente sea idéntico, la forma de serialización puede adaptarse al consumidor.
* **Clasificación:** Demostrado por código y tests.

##### 6. Nomenclatura y Selección de Checks
* **Código:**
  * CLI usa strings en kebab-case/abreviados: `hot`, `redundant`, `low-usage`, `invalid`, `unindexed-fks`, `dead-tuples` ([`audit_pg.py:206`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L206)).
  * MCP usa un Enum con strings en snake_case largo: `CheckName.HOT_FILLFACTOR = "hot_fillfactor"`, `CheckName.LOW_USAGE_INDEXES = "low_usage_indexes"`, etc. ([`mcp_pg_auditor.py:46`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/mcp_pg_auditor.py#L46)).
* **Clasificación:** Demostrado por código; inconsistencia de nombres en la capa de adaptación.

---

#### 2.2.4 Comparativa de Opciones de Contrato

##### Opción A — Contrato Semántico Unificado
CLI y MCP son dos adaptadores del **mismo motor canónico**. A iguales entradas sobre la misma base de datos, ambos adaptadores deben producir conceptualmente los mismos hallazgos.

1. **Configuración canónica requerida:**
   * Módulo canónico de configuración definiendo los valores por defecto oficiales para `min_table_rows`, `min_size_bytes`, `schemas` y nomenclatura de checks.
   * Ambos adaptadores leen e inyectan estos mismos defaults canónicos.
2. **Diferencias que desaparecen:**
   * La divergencia de findings entre ejecutar en terminal e invocar vía MCP.
   * La discrepancia no documentada de cobertura de esquemas entre adaptadores.
   * La duplicación arbitraria de defaults en el código.
3. **Preservación de diferencias legítimas:**
   * La CLI mantiene sus flags de terminal, su soporte para archivos `.env` locales y su presentación en texto/JSON.
   * MCP mantiene su interfaz JSON-RPC, su soporte multi-alias (`db_alias`) y sus esquemas Pydantic para el cliente.
4. **Impacto sobre compatibilidad:**
   * Modificar los defaults actuales requerirá actualizar los tests existentes en [`tests/test_cli.py`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/tests/test_cli.py) o [`tests/test_mcp_auditor.py`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/tests/test_mcp_auditor.py) una vez que se definan los valores canónicos definitivos.

##### Opción B — Divergencia Explícita y Contractual
CLI y MCP se formalizan como adaptadores con **perfiles de operación especializados y deliberadamente diferentes**:
* Esta alternativa **no está libre de impacto**: mantener dos perfiles implica formalizar un contrato dual adicional, mayor superficie de documentación y mantenimiento, y riesgo permanente de drift entre políticas.

---

#### 2.2.5 Consecuencias Concretas de Cada Elección

| Dimensión | Opción A (Contrato Unificado) | Opción B (Divergencia Contractual) |
|---|---|---|
| **Principio de Mínima Sorpresa** | Máximo: una auditoría arroja exactamente los mismos hallazgos sea por CLI o por MCP. | Condicionado: el usuario debe conocer que el CLI y el MCP aplican filtros de severidad distintos. |
| **Divergencia de Cobertura por Defaults** | Eliminada: ambos adaptadores consultan el mismo universo por defecto. | Mantenida por diseño: requiere advertencias permanentes para evitar falsas sensaciones de seguridad. |
| **Carga de Mantenimiento** | Menor: una única fuente de verdad para la lógica y parámetros de auditoría. | Mayor: dos perfiles que deben documentarse, versionarse y probarse por separado. |
| **Superficie de Contrato** | Simplificada: un único contrato canónico (`AuditReport`). | Expandida: contrato dual con reglas de mapeo entre perfiles. |

---

#### 2.2.6 Resolución de la Decisión
* **Estado:** `RESOLVED`
* **Decisión del propietario:** `OPCION_A` (Contrato Semántico Unificado).
* **Definición formal:**
  1. CLI y MCP son dos adaptadores del mismo auditor canónico.
  2. Una misma configuración efectiva debe representar el mismo universo semántico de auditoría independientemente del adaptador.
  3. Los defaults de auditoría deben ser canónicos y compartidos, no definidos independientemente en CLI y MCP.
  4. `AuditReport` debe permanecer como contrato canónico del dominio/aplicación.
  5. Las diferencias propias del transporte/adaptador (DSN input, formato de salida, CLI flags, MCP schema, presentación, etc.) pueden permanecer específicas siempre que no alteren silenciosamente la semántica de la auditoría.

---

#### 2.2.7 Consecuencias Concretas para las Siguientes Fases
1. **Fuente única de verdad:** Extraer o definir los parámetros canónicos de auditoría en un único módulo compartido por ambos adaptadores.
2. **Eliminación de duplicación:** Suprimir los valores por defecto cableados independientemente en [`audit_pg.py:734,784`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L734) y [`mcp_pg_auditor.py:363-364`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/mcp_pg_auditor.py#L363).
3. **Preservación de diferencias de transporte:** Mantener intactas las particularidades legítimas de interfaz (flags en CLI, schemas Pydantic en MCP, `db_alias`, etc.).
4. **Actualización de tests de contrato:** Adecuar las suites de tests para validar que, ante parámetros por defecto, CLI y MCP auditan exactamente el mismo alcance de datos.
5. **Alineación documental:** Reflejar en `README.md` y `SKILL.md` que la herramienta opera bajo un único contrato unificado sin discrepancias ocultas.

---

#### 2.2.8 Decisiones Técnicas Pendientes para la Fase de Implementación
Habiéndose resuelto el principio de unificación semántica, la determinación de los valores específicos queda diferida a la fase de implementación (Fase 2), requiriendo resolver:
* **D-T02.1 (`min_table_rows`):** Definir el valor canónico definitivo a compartir (entre 10.000, 1.000 u otro valor respaldado por evidencia).
* **D-T02.2 (`min_size_bytes`):** Definir el valor canónico definitivo a compartir (entre 0, 10 MB u otro umbral justificado).
* **D-T02.3 (`schemas`):** Definir la regla canónica de alcance territorial por defecto (todos los esquemas de usuario excluyendo catálogos del sistema vs. lista explícita).
* **D-T02.4 (Identificadores de Checks):** Armonizar la nomenclatura de selección de checks (`CheckName` vs. strings kebab-case) manteniendo compatibilidad hacia atrás.

### 2.3 Decision Record — T-03: Semántica Contractual de la Auditoría Parcial y Política de Errores

#### 2.3.1 Pregunta de Diseño Fundamental
> Si uno o más checks individuales fallan pero otros producen evidencia válida sobre la base de datos, ¿el auditor debe abortar toda la ejecución descartando los hallazgos previos, o debe admitir un `AuditReport` parcial resiliente que preserve la evidencia recolectada y comunique formalmente su condición de incompleto?

---

#### 2.3.2 Decisión Final
* **Estado:** `RESOLVED`
* **Decisión del propietario:** `OPCION_B` (Auditoría Parcial Resiliente).
* **Definición formal:**
  1. Un fallo localizado de un check independiente **no invalida automáticamente** la evidencia válida obtenida por otros checks.
  2. Los checks que puedan ejecutarse exitosamente deben **conservar sus resultados** en el reporte.
  3. Un reporte parcial **no puede ser semánticamente equivalente** a una auditoría completa sin errores.
  4. La condición de `findings: []` en una auditoría parcial **no debe poder interpretarse como "base de datos sana"**.
  5. Debe existir una **señal contractual inequívoca** a nivel raíz del reporte que indique que la auditoría quedó incompleta o degradada.
  6. Debe existir **trazabilidad suficiente** para distinguir entre checks planificados, checks completados con éxito y checks fallidos, asociando cada falla con su check de origen.
  7. Los fallos globales de infraestructura, conexión, configuración o integridad interna **continúan siendo fatales** cuando impidan producir un reporte técnicamente defendible.
  8. La **independencia real** entre checks determina qué errores son recuperables; no se asume que cualquier error de SQL sea automáticamente recuperable.
  9. La implementación concreta de estos conceptos (modelos, campos, enums y códigos de salida) **queda diferida** a las tareas de implementación (Fase 2) y no se predefine en este registro.

---

#### 2.3.3 Motivación
* **Resiliencia operativa en producción:** En arquitecturas reales, los roles de base de datos asignados a herramientas de auditoría frecuentemente tienen permisos segmentados (por ejemplo, acceso a catálogos de índices y restricciones `pg_class`, `pg_index`, pero sin privilegios sobre vistas estadísticas de usuario `pg_stat_user_tables`). Abortar la totalidad de la auditoría ante la primera denegación de permisos inutiliza por completo la herramienta e impide detectar problemas críticos como índices inválidos o foreign keys desindexadas.
* **Preservación de evidencia:** Descartar evidencia válida de checks que ya corrieron con éxito ante un timeout o error local de un check tardío representa un desperdicio de cómputo y priva al operador de información diagnóstica útil.

---

#### 2.3.4 Semántica Contractual Resultante
La adopción de la auditoría parcial establece las siguientes garantías para el contrato del sistema:
* **No ambigüedad ante la ausencia de hallazgos:** Se neutraliza el riesgo de falso negativo. Si los checks ejecutados no detectaron problemas pero hubo checks no ejecutados, el consumidor (agente LLM o pipeline de CI/CD) no puede deducir la ausencia de anomalías en los dominios no evaluados.
* **Desacople de completitud vs. severidad:** La completitud de la ejecución se desacopla formalmente del flag de severidad de hallazgos (`has_critical_issues`), garantizando que la omisión de un check crítico no sea enmascarada como ausencia de problemas.
* **Trazabilidad granular del alcance:** El consumidor puede auditar con precisión qué comprobaciones se completaron y cuáles fallaron, junto con la causa de cada omisión.

---

#### 2.3.5 Qué Queda Explícitamente Fuera de esta Decisión
* **No se predefinen estructuras de código:** No se crean ni se fijan en este paso nombres de clases, enums, atributos Pydantic ni campos concretos del contrato.
* **No se asume un mecanismo de implementación único:** No se presupone que el aislamiento deba resolverse exclusivamente mediante `try/except + rollback`; la arquitectura concreta de ejecución se evaluará en su tarea específica.
* **No se cambia el tratamiento de errores fatales:** Caídas de conexión, credenciales inválidas o fallos de configuración siguen interrumpiendo el proceso sin generar reportes espurios.
* **No se implementa código ni tests en esta fase:** El código fuente permanece inalterado hasta completar la totalidad de las decisiones de Fase 0.

---

#### 2.3.6 Requisitos para la Fase de Implementación Posterior
Durante la Fase 2, la implementación técnica de la Opción B deberá resolver:
1. **Modelado canónico:** Incorporar al contrato de dominio la señal inequívoca de completitud/integridad y la asociación estructurada entre checks y sus correspondientes fallos.
2. **Adaptador CLI:** Definir la política de códigos de salida (`exit code`) ante auditorías parciales, distinguiendo entre auditorías parciales sin hallazgos y con hallazgos de severidad.
3. **Adaptador MCP:** Estructurar la respuesta del tool para que los agentes LLM reciban explícitamente el estado parcial de la auditoría y no asuman salud general ante la ausencia de findings.
4. **Gestión de sesión de infraestructura:** Garantizar que un fallo en una consulta de catálogo no contamine la sesión ni cause errores en cascada (`InFailedSqlTransaction`) en los checks subsiguientes.

---

#### 2.3.7 Evidencia Fáctica que Fundamenta la Decisión
1. **Comportamiento actual en código:** [`audit_pg.py:209-243`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L209-L243) y [`mcp_pg_auditor.py:170-205`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/mcp_pg_auditor.py#L170-L205) ejecutan los checks secuencialmente sin aislamiento; ante una excepción, el proceso aborta perdiendo todo dato previo. Este comportamiento refleja la ausencia de lógica de recuperación, no una intención de diseño deliberada de atomicidad.
2. **Carácter decorativo de `AuditReport.errors`:** El campo `errors: list[str]` en [`src/sql_audit/domain/models.py:92`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/domain/models.py#L92) existe en el modelo pero jamás se puebla ni se testea en ejecución real ([`mapping.py:366`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/mapping.py#L366)), demostrando que una lista plana de strings no constituye un contrato suficiente para auditorías parciales.
3. **Riesgo en el cálculo actual:** En [`mapping.py:285`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/mapping.py#L285), `has_critical_issues` se calcula únicamente sobre `findings`; si un check crítico no corre y no aporta findings, el sistema reporta `has_critical_issues: False`, demostrando la necesidad de una señal explícita de integridad.
4. **Independencia en catálogos:** Los checks de índices, FKs y estadísticas consultan vistas relacionales desacopladas; un fallo en `pg_stat_user_tables` no impide físicamente consultar `pg_index`.

### 2.4 Decision Record — T-04: Semántica de `evidence` en DiffEngine

#### 2.4.1 Pregunta de Diseño Fundamental
> ¿Qué representa semánticamente la colección `Finding.evidence: list[Evidence]` dentro del contrato de dominio y, por consiguiente, cómo debe evaluar `DiffEngine` la igualdad o mutación de evidencias entre dos auditorías sucesivas?
>
> **Opción A — Colección semánticamente no ordenada:** `evidence` representa un conjunto de observaciones fácticas concurrentes que sustentan el hallazgo en el momento de la auditoría. La posición física dentro de la lista no añade significado, por lo que una permutación en el orden de las evidencias no constituye un cambio de estado (`UNCHANGED`).
>
> **Opción B — Secuencia ordenada con significado posicional:** La posición de cada `Evidence` dentro de `evidence: list[Evidence]` posee significado contractual formal (por ejemplo: jerarquía de relevancia, procedencia diagnóstica o secuencia ordenada). Alterar el orden relativo entre dos auditorías constituye una mutación fáctica (`CHANGED`).

---

#### 2.4.2 Distinción Crítica: Orden Interno vs. Temporalidad de Snapshots
Para evitar falacias de diseño, se establece una separación conceptual estricta:
1. **La temporalidad es inter-reporte, no intra-finding:** El carácter temporal del sistema reside en la comparación entre dos reportes distintos en el tiempo ([`compare_audit_reports(previous, current)`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/diff_service.py#L9)). Cada `AuditReport` es un snapshot estático e instantáneo capturado en su `observed_at`.
2. **`Finding.evidence` no es un log cronológico de eventos:** Un `Finding` representa una anomalía diagnosticada en un instante determinado. Las evidencias asociadas son las pruebas de catálogo o estadísticas que respaldan ese diagnóstico puntual. La presencia de `observed_at` en [`Evidence`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/domain/models.py#L39) registra el instante de captura del dato en la base, no una línea de tiempo secuencial dentro del hallazgo.

---

#### 2.4.3 Análisis Forense del Estado Actual del Repositorio

##### 1. Modelo de Dominio e Identidad de Evidencia
* **Contrato de datos ([`models.py:33-64`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/domain/models.py#L33-L64)):**
  * `Finding.evidence: list[Evidence] = Field(default_factory=list)`. Se utiliza el tipo `list`, lo cual provee orden físico de inserción en Python y JSON, pero no define semántica de ordenación del dominio. Permite listas de 0, 1 o $N$ elementos.
  * `Evidence` contiene: `source`, `observed_at`, `server_version`, `query_name`, `values: dict[str, JsonValue]` y `evidence_id: str`. No contiene ningún campo de índice, posición, prioridad o peso.
* **Cálculo de `evidence_id` ([`hasher.py:34-47`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/domain/hasher.py#L34-L47)):**
  ```python
  payload = {
      "source": source,
      "query_name": query_name,
      "values": values,
  }
  encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
  digest = hashlib.sha256(encoded).hexdigest()
  return f"sha256:{digest}"
  ```
  * `evidence_id` representa exclusivamente el digest SHA-256 de `source`, `query_name` y `values`.
  * **Campos excluidos del hash:** `observed_at` y `server_version` **no forman parte del digest**. Si los contadores y valores son idénticos, dos auditorías ejecutadas en días distintos producen exactamente el mismo `evidence_id`.
  * Los diccionarios en `values` se serializan con `sort_keys=True`, garantizando determinismo independientemente del orden de inserción de las claves.

##### 2. Instanciación en Código de Producción (Frontera Fáctica)
Se auditaron todos los puntos del repositorio donde se construyen instancias de `Finding` y `Evidence`:
* [`mapping.py`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/mapping.py) (6 mappers de catálogo/estadísticas):
  * `map_invalid_index_to_finding`: `evidence=[evidence]` (1 elemento).
  * `map_unindexed_fk_to_finding`: `evidence=[evidence]` (1 elemento).
  * `map_dead_tuples_to_finding`: `evidence=[evidence]` (1 elemento).
  * `map_hot_to_finding`: `evidence=[evidence]` (1 elemento).
  * `map_redundant_index_to_finding`: `evidence=[evidence]` (1 elemento).
  * `map_low_usage_index_to_finding`: `evidence=[evidence]` (1 elemento).
* [`bloat_service.py`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/bloat_service.py) (2 mappers de bloat físico):
  * Bloat de tablas (`line 137`): `evidence=[evidence]` (1 elemento).
  * Bloat de índices (`line 186`): `evidence=[evidence]` (1 elemento).
* [`lock_service.py`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/lock_service.py) (1 mapper de contención de locks):
  * Árbol de bloqueos (`line 229`): `evidence=[evidence]` (1 elemento).
* **Hallazgo concluyente:** En el 100% de los casos reales del motor actual, **cada `Finding` se construye con exactamente una evidencia (`len == 1`)**. No existe en producción ningún generador que emita $N \ge 2$ evidencias ni $N = 0$.

##### 3. El Algoritmo Actual de `DiffEngine` y sus Dependencias
* **Implementación ([`diff_service.py:30-31`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/diff_service.py#L30-L31)):**
  ```python
  prev_eid = prev_f.evidence[0].evidence_id if prev_f.evidence else ""
  curr_eid = curr_f.evidence[0].evidence_id if curr_f.evidence else ""
  if prev_eid == curr_eid:
      unchanged_findings.append(curr_f)
  else:
      changed_findings.append(...)
  ```
* **Modelo `ChangedFinding` ([`diff.py:24-35`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/domain/diff.py#L24-L35)):**
  Contiene `previous_evidence_id: str` y `current_evidence_id: str` como atributos de tipo escalar (`str`), acoplando estructuralmente el modelo de reporte diferencial a la presuposición de una sola evidencia por hallazgo.
* **Uso de `evidence[0]` en la suite de tests:**
  * [`tests/test_canonical_contract.py:171`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/tests/test_canonical_contract.py#L171): `assert cf.evidence[0].evidence_id == mf.evidence[0].evidence_id`.
  * [`tests/test_canonical_contract.py:200`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/tests/test_canonical_contract.py#L200): `assert finding["evidence"][0]["evidence_id"].startswith("sha256:")`.
  * [`tests/test_bloat_engine.py:92, 97`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/tests/test_bloat_engine.py#L92): `assert tbl_f.evidence[0].evidence_id.startswith("sha256:")`.
  * [`tests/test_lock_engine.py:117`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/tests/test_lock_engine.py#L117): `assert finding.evidence[0].evidence_id.startswith("sha256:")`.
  * [`tests/test_domain_models.py:86`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/tests/test_domain_models.py#L86): `assert dumped["findings"][0]["evidence"][0]["evidence_id"] == "sha256:abc123456"`.
  * **Conclusión:** El uso ubicuo de `evidence[0]` en tests y en `DiffEngine` obedece a que todos los fixtures y generadores crean listas unitarias, no a una especificación probada de que `evidence[0]` sea un elemento privilegiado o canónico.

---

#### 2.4.4 Clasificación de la Evidencia Fáctica
* **Demostrado por Código:**
  * `Finding.evidence` está tipado como `list[Evidence]` con default `[]`.
  * Todo mapper en producción construye listas de tamaño 1 (`[evidence]`).
  * `DiffEngine` solo compara `evidence[0].evidence_id` e ignora cualquier elemento en `evidence[1:]`.
  * `ChangedFinding` modela los IDs de evidencia previa y actual como cadenas escalares (`str`).
  * `compute_evidence_id` omite `observed_at` y `server_version`, hasheando únicamente `source`, `query_name` y `values`.
* **Demostrado por Tests:**
  * Todos los tests unitarios y de integración crean y validan findings con listas unitarias (`len == 1`).
  * No existen tests que evalúen findings con 0 evidencias ni con múltiples evidencias ($N \ge 2$).
  * No existen tests que verifiquen el impacto del orden o permutación de evidencias.
* **Documentado como Intención:**
  * [`README.md:19`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/README.md#L19) establece: *"Standardized `AuditReport`, `Finding`, and `Evidence` schema with deterministic SHA-256 digests. Decouples identity (`finding_id`) from transient observation metrics (`evidence_id`), enabling reliable state tracking across time."*
  * [`SKILL.md:35`](file:///C:/Users/cerra/.gemini/config/skills/sql-audit/SKILL.md#L35) describe el diff como: *"Compara estado actual contra snapshot canónico previo (`--canonical-json`). Reporta `+` nuevos, `-` resueltos, `~` cambiados, `=` idénticos."*
  * En ninguna parte de la documentación se describe `evidence` como una secuencia temporal ni se atribuye semántica a la posición relativa dentro de la lista.
* **Inferencia:**
  * El autor definió `list[Evidence]` anticipando que un finding complejo podría requerir múltiples piezas de evidencia fáctica en el futuro (ej. estadísticas de tabla + definición de índice + métricas de locks), pero implementó `diff_service.py` con `evidence[0]` como un atajo provisorio bajo el supuesto de que en el presente inmediato solo existía una evidencia por finding.
* **Decisión de Producto Pendiente:**
  * Adoptar formalmente la Opción A (conjunto/bag no ordenado) o la Opción B (secuencia posicional ordenada), y redefinir el contrato de `DiffEngine` y `ChangedFinding` en concordancia.

---

#### 2.4.5 Análisis Riguroso de Escenarios Contractuales (para una misma `finding_id`)

| # | Escenario Fáctico | Comportamiento Actual (`evidence[0]`) | Semántica Esperada bajo Opción A (No ordenada) | Semántica Esperada bajo Opción B (Ordenada) |
|---|---|---|---|---|
| **1** | **Misma evidence completa** (mismos IDs) | `prev[0] == curr[0]` $\to$ `UNCHANGED`. Correcto. | `UNCHANGED` | `UNCHANGED` |
| **2** | **Cambia solamente `evidence[0]`** | `prev[0] != curr[0]` $\to$ `CHANGED`. Correcto. | `CHANGED` | `CHANGED` |
| **3** | **Cambia solamente `evidence[1]`** (con `len >= 2`) | `prev[0] == curr[0]` $\to$ **`UNCHANGED` (Falso Negativo)**. El motor ignora la mutación de la segunda evidencia. | `CHANGED` (la totalidad del conjunto de evidencias cambió). | `CHANGED` (la posición 1 mutó). |
| **4** | **Se agrega una nueva evidence** (`len`: $1 \to 2$) | Si se agrega al final: `prev[0] == curr[0]` $\to$ **`UNCHANGED` (Falso Negativo)**. Si se agrega al inicio: `CHANGED`. | `CHANGED` (el soporte fáctico del finding se expandió). | `CHANGED` (la longitud o estructura de la secuencia cambió). |
| **5** | **Se elimina una evidence** (`len`: $2 \to 1$) | Si se elimina la última: **`UNCHANGED` (Falso Negativo)**. Si se elimina la primera: `CHANGED`. | `CHANGED` (se redujo el soporte fáctico). | `CHANGED` (la secuencia cambió). |
| **6** | **Mismas evidencias en distinto orden** (`[E1, E2]` vs `[E2, E1]`) | `E1 != E2` $\to$ **`CHANGED` (Falso Positivo)**. | **`UNCHANGED`** (el conjunto fáctico de pruebas es idéntico; el orden físico de serialización es irrelevante). | **`CHANGED`** (el orden relativo es contractual y se alteró). |
| **7** | **Evidencias duplicadas** (`[E1, E1]`) | Compara solo `prev[0]` con `curr[0]`. Pydantic no valida unicidad. | Depende de si se modela como *Set* (se deduplica $\to$ equivale a `[E1]`) o *Multiset* (la cardinalidad importa). | `CHANGED` si se compara contra `[E1]` (difiere en longitud). |
| **8** | **Colisión de `evidence_id` entre evidencias distintas** | Compara hashes iguales $\to$ `UNCHANGED`. Ocurre si dos evidencias difieren **únicamente en `observed_at` o `server_version`** (campos omitidos por `compute_evidence_id`). | `UNCHANGED` si el contrato define que la evidencia está determinada por sus valores de catálogo; `CHANGED` si la metadata de servidor debe distinguirlas. | Igual que Opción A respecto a los datos hasheados. |
| **9** | **Evidencia con contenido volátil** (métrica fluctúa levemente) | Cualquier cambio en `values` altera el SHA-256 $\to$ `CHANGED`. | `CHANGED`. El motor reporta persistencia con variación métrica. | `CHANGED`. |
| **10** | **`evidence = []`** (lista vacía) | Si ambos están vacíos: `"" == ""` $\to$ `UNCHANGED`. Si uno está vacío y el otro no: `"" != "sha256:..."` $\to$ `CHANGED`. | El modelo actual lo permite sintácticamente, pero representa una anomalía fáctica: un finding sin evidencia contradice la filosofía *"Evidence over Assumptions"*. | Igual que Opción A a nivel algorítmico. |

---

#### 2.4.6 Comparativa de Opciones de Producto

```
                    ┌──────────────────────────────────────────────┐
                    │    Finding.evidence: list[Evidence]          │
                    └──────────────────────┬───────────────────────┘
                                           │
                 ┌─────────────────────────┴─────────────────────────┐
                 ▼                                                   ▼
   ┌───────────────────────────┐                       ┌───────────────────────────┐
   │ Opción A: No Ordenada     │                       │ Opción B: Ordenada        │
   │ (Conjunto de Observaciones│                       │ (Secuencia Posicional con │
   │ Concurrentes)             │                       │ Significado Semántico)    │
   ├───────────────────────────┤                       ├───────────────────────────┤
   │ * [E1, E2] == [E2, E1]    │                       │ * [E1, E2] != [E2, E1]    │
   │ * Sin falsos positivos por│                       │ * Requiere fijar reglas de│
   │   orden de consulta SQL.  │                       │   ordenamiento estricto.  │
   │ * Alineado a naturaleza   │                       │ * Útil solo si la posición│
   │   relacional de catálogos.│                       │   indica jerarquía/tiempo.│
   └───────────────────────────┘                       └───────────────────────────┘
```

##### Opción A — Colección Semánticamente No Ordenada (Conjunto Fáctico)
* **Semántica:** Las evidencias son pruebas que coexisten para justificar el hallazgo en el snapshot auditado. Que una fila del catálogo se lea antes o después no cambia la realidad física de la base de datos.
* **Ventajas:**
  * Inmune a fluctuaciones no deterministas en el orden en que PostgreSQL devuelve filas cuando no hay un `ORDER BY` exhaustivo.
  * Modela fielmente la realidad del dominio: las evidencias de un snapshot son concurrentes.
  * Comparación conceptualmente limpia: `set(prev_eids) == set(curr_eids)`.
* **Desventajas / Impacto:**
  * Requiere definir la semántica ante duplicados (¿conjunto matemático puro donde `[E1, E1] == [E1]` o multiset con conteo de ocurrencias?).
  * `ChangedFinding` no puede almacenar un único `previous_evidence_id: str` y `current_evidence_id: str`; debe almacenar colecciones o un resumen representativo de la mutación.

##### Opción B — Secuencia Ordenada con Significado Posicional
* **Semántica:** La posición relativa de cada evidencia importa contractualmente. La primera evidencia (`evidence[0]`) es la evidencia primaria o principal; las subsiguientes son evidencias secundarias, contextuales o históricas.
* **Ventajas:**
  * Preserva la estructura posicional directa de `list` en Python y JSON.
  * Permite definir formalmente que `evidence[0]` es el indicador rector del finding.
* **Desventajas / Impacto:**
  * Obliga a garantizar un ordenamiento determinista estricto en la construcción de evidencias en todos los mappers (si no, un orden aleatorio de consulta generará falsos positivos de `CHANGED`).
  * Introduce complejidad innecesaria dado que actualmente ningún check produce más de una evidencia.
  * Impone una semántica jerárquica no documentada en ninguna parte del proyecto.

---

#### 2.4.7 Respuestas a las 10 Preguntas Clave

1. **¿Qué significa `Finding.evidence` actualmente?**
   Actualmente es un contenedor tipado como `list[Evidence]` que en el 100% del código de producción alberga exactamente un elemento representativo de la fila de catálogo o estadística que disparó la regla de auditoría.
2. **¿Es ordenada o no ordenada?**
   En Python es físicamente ordenada (una lista estándar). En el dominio de negocio carece de semántica de orden: no existe ninguna regla, documentación ni test que defina que la posición relativa represente orden temporal, causal o de prioridad.
3. **¿Existe evidencia de que el orden tenga significado?**
   **No.** No existe evidencia documental ni fáctica de que el orden tenga significado. La única razón por la que `diff_service.py` accede a `evidence[0]` es porque los mappers siempre instancian listas con un solo elemento.
4. **¿Qué representa realmente `evidence_id`?**
   Representa la huella criptográfica SHA-256 de los datos diagnósticos (`source`, `query_name`, `values`). Representa el estado observado exacto de los contadores/métricas, excluyendo intencionalmente el timestamp (`observed_at`) y la versión de servidor (`server_version`) para permitir que dos auditorías en momentos diferentes reconozcan métricas idénticas.
5. **¿Qué casos reales rompe el algoritmo actual?**
   Rompe de forma demostrable cualquier finding con múltiples evidencias:
   * Si cambia `evidence[1]`, reporta falsamente `UNCHANGED` (falso negativo).
   * Si se agrega o quita una evidencia al final, reporta falsamente `UNCHANGED` (falso negativo).
   * Si las mismas evidencias aparecen permutadas, reporta falsamente `CHANGED` (falso positivo).
   * Además, `ChangedFinding` colapsa porque asume cadenas escalares para los IDs.
6. **¿Qué comportamiento debería tener el diff si A fuera la semántica correcta?**
   Debe evaluar la igualdad de evidencias como la equivalencia de conjuntos de `evidence_id` (o de evidencias canónicas). Dos reportes son `UNCHANGED` si y solo si poseen exactamente los mismos identificadores de evidencia, sin importar el orden de serialización. Si hay diferencias en los elementos presentes, se clasifica como `CHANGED`.
7. **¿Qué comportamiento debería tener si B fuera la semántica correcta?**
   Debe evaluar la igualdad elemento a elemento preservando los índices (`prev.evidence[i] == curr.evidence[i]`) y requiriendo longitudes idénticas. Cualquier permutación o desplazamiento de índice, aun con el mismo contenido, debe clasificarse como `CHANGED`.
8. **¿Qué tests actuales respaldan cada interpretación?**
   **Ninguna de las dos.** La suite de tests actual solo cubre el caso trivial donde todas las listas tienen longitud 1 (`len == 1`). No existe un solo test que respalde la Opción A (comparación de conjuntos multi-evidencia) ni la Opción B (sensibilidad al orden).
9. **¿Qué información falta para decidir?**
   Únicamente la decisión de producto del propietario sobre la intención arquitectónica: ¿Se concibe `Finding.evidence` como un conjunto fáctico desacoplado del orden de lectura SQL (Opción A, recomendada por la naturaleza relacional), o se proyecta atribuirle valor posicional o jerárquico a los índices de la lista (Opción B)?
10. **¿Qué deuda/bug queda confirmado independientemente de la decisión?**
    El código de [`diff_service.py:30-31`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/diff_service.py#L30-L31) (`prev_f.evidence[0]`) y [`diff.py:32-33`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/domain/diff.py#L32-L33) (`previous_evidence_id: str`) constituye un **defecto de implementación confirmado**: asume una colección unitaria a pesar de que el contrato público define una lista, quedando ciego ante cualquier hallazgo multi-evidencia.

---

#### 2.4.8 Estado de la Decisión
* **Estado:** `DECIDED`
* **Decisión del propietario:** `UNORDERED_EVIDENCE`
* **Definición formal:**
  1. `Finding.evidence` tiene semántica de colección no ordenada. El orden de almacenamiento no constituye información de dominio.
  2. El `DiffEngine` debe comparar la totalidad de las evidencias relevantes, no únicamente `evidence[0]`, y una permutación de evidencias equivalentes no debe producir `CHANGED`.
  3. La representación concreta (`list`, normalización, comparación por `evidence_id`, tratamiento de duplicados, etc.) queda para la implementación de T-07 y no forma parte de esta decisión.

---

### 2.5 Decision Record — T-05: Identidad de los Findings del Lock Graph

#### 2.5.1 Pregunta de Diseño Fundamental
> ¿La identidad actual `PG-LOCK-CONTENTION:PID_{root_pid}` representa correctamente la identidad semántica de un finding de contención de locks dentro del contrato canónico, o el dominio requiere una identidad basada en el recurso bloqueado (tabla, índice, tupla)?
>
> **Opción A — Identidad a nivel de Proceso / Sesión (`PID`):** La contención de locks se modela como una anomalía operacional viva de una sesión/proceso activo específico (`object_type="process"`).
>
> **Opción B — Identidad a nivel de Recurso en Disputa (Relación/Objeto):** La contención de locks se modela como una anomalía sobre el objeto de la base de datos bloqueado (`object_type="table"` o `"relation"`), requiriendo extender la recolección hacia `pg_locks`.
>
> **Opción C — Delimitación Operacional (Diagnóstico Live vs. Diff Canónico):** La inspección de locks se reconoce como una capacidad forense de diagnóstico en tiempo real (on-demand), excluyéndola del diff canónico de salud estructural o tratándola con una semántica de ciclo de vida puramente transitorio.

---

#### 2.5.2 Qué Representa Realmente el Finding Hoy (Frontera Fáctica del Repositorio)

##### 1. Construcción del Finding en Código
En [`src/sql_audit/application/lock_service.py:212-231`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/lock_service.py#L212-L231):
```python
finding_id = compute_stable_finding_id(
    "PG-LOCK-CONTENTION", "process", f"PID_{tree.root_pid}"
)
findings.append(
    Finding(
        finding_id=finding_id,
        check="lock_contention",
        severity=severity,
        object_type="process",
        object_name=f"PID {tree.root_pid}",
        reason=reason,
        evidence=[evidence],
    )
)
```
* **Identidad fáctica:** El `finding_id` actual es estrictamente `"PG-LOCK-CONTENTION:PID_{root_pid}"`.
* **Objeto auditado:** `object_type="process"` y `object_name="PID {root_pid}"`.
* **Semántica actual del finding:** Representa **el proceso raíz bloqueante (`root_pid`)** que encabeza un árbol de dependencias de bloqueo en el instante del muestreo.

##### 2. Evidencia Recolectada por la Consulta SQL Actual
En [`src/sql_audit/infrastructure/queries.py:353-380`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/infrastructure/queries.py#L353-L380) (`SQL_LOCK_CONTENTION`):
* La consulta hace:
  ```sql
  FROM pg_stat_activity blocked
  CROSS JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS blocking_pids(blocking_pid)
  JOIN pg_stat_activity blocking ON blocking.pid = blocking_pids.blocking_pid
  ```
* **Qué tablas consulta:** Únicamente `pg_stat_activity` y la función interna `pg_blocking_pids()`.
* **Qué tablas NO consulta:** **NO consulta `pg_locks` ni `pg_class`**.
* **Qué información contiene:**
  * PIDs involucrados (`blocked_pid`, `blocking_pid`).
  * Atributos de proceso/conexión: `usename`, `application_name`, `client_addr`, `state`, `query_start`, `xact_start`, `wait_event_type`, `wait_event`, `query`.
* **Qué información NO contiene:**
  * **Nombre de tabla o relación:** Ausente.
  * **Modo de lock solicitado u otorgado** (`AccessExclusiveLock`, `RowShareLock`, etc.): Ausente.
  * **Tipo de lock** (`relation`, `transactionid`, `tuple`, `virtualxid`, `advisory`, `extend`): Solo se observa indirectamente si `wait_event_type = 'Lock'` y `wait_event = 'relation'`, pero sin identificar la relación.
  * **Identificador de tupla o página:** Ausente.

##### 3. Uso Real en CLI y MCP (Desacople Operacional)
* **CLI (`audit_pg.py:892-907`):** El flag `--locks` invoca `auditor.audit_locks()`, el cual devuelve un [`LockContentionReport`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/domain/locks.py#L42) y lo formatea directamente como texto visual ([`render_lock_report_text`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/lock_service.py#L236)) o JSON de dicho modelo. **No genera `Finding` ni `AuditReport`**.
* **MCP (`mcp_pg_auditor.py:397-413`):** El tool `pg_locks` invoca `auditor.audit_locks()` y retorna el `LockContentionReport` tipado. **Tampoco genera `Finding`**.
* **Health Audit General (`audit_pg.py` por defecto y `pg_health_audit`):** Ejecuta los 6 checks de salud estructural (índices, FKs, autovacuum). **NO incluye el check de locks en la auditoría general de salud**.
* **Consumo de `lock_report_to_findings`:** En todo el repositorio, la función que convierte árboles de locks a `Finding` **solo es invocada en [`tests/test_lock_engine.py:111`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/tests/test_lock_engine.py#L111)**. No forma parte de ningún flujo de producción activo actual.

---

#### 2.5.3 Distinciones Conceptuales Estrictas

1. **Identidad del finding (`finding_id`):** Actualmente `"PG-LOCK-CONTENTION:PID_{root_pid}"`. Identifica la instancia del proceso del sistema operativo / backend que sostiene el bloqueo raíz.
2. **Contenido / Evidencia del finding:** Valores runtime en `evidence.values`: `{root_pid, root_user, root_state, root_xact_age_sec, total_blocked, blocked_pids, max_blocked_duration_sec, chain_text}`.
3. **Estado temporal del incidente:**
   * La contención de locks es un fenómeno operacional **estocástico y transitorio de tiempo real** ($O(\text{milisegundos a minutos})$).
   * A diferencia de índices inválidos o FKs sin indexar (que son defectos estáticos y persistentes del catálogo que duran días o meses hasta una migración DDL), los bloqueos desaparecen tan pronto la transacción hace `COMMIT`, `ROLLBACK`, o el cliente se desconecta.
4. **Estabilidad temporal del `finding_id` actual ante diferentes escenarios:**
   * *Mismo incidente con nuevos PIDs (ej. reconexión de pool PgBouncer o reintento de worker):* Si la conexión cae y se reintenta, el nuevo root tiene otro PID (ej. $1050 \to 1080$). Para `DiffEngine`, el incidente previo aparece como `RESOLVED` y el nuevo como `NEW`, ocultando que se trata del mismo cuello de botella recurrente.
   * *Mismo PID con incidente diferente (worker permanente reutilizado):* Un proceso persistente con PID $1050$ que bloqueó una tabla a las 10:00 y horas después bloquea otra tabla diferente por otra query será considerado por `DiffEngine` como el *mismo finding persistente con métricas mutadas* (`CHANGED`), falseando la continuidad del incidente.
   * *Múltiples roots simultáneos:* Se generan múltiples findings (`PID_A`, `PID_B`), cada uno con su subárbol independiente.
   * *Mismo recurso con distintos procesos:* Al no existir el concepto de recurso en la identidad, no hay correlación entre dos bloqueos consecutivos sobre la misma tabla.
   * *Cambios en la cadena de bloqueo:* Si el PID raíz persiste pero se bloquean más procesos, el `finding_id` se mantiene y la evidencia muta $\to$ `CHANGED`.
   * *Ciclos de bloqueo:* Si ocurre un ciclo antes de que actúe el detector de deadlocks de PostgreSQL, el código actual selecciona arbitrariamente el primer PID de `blocking_to_blocked` como raíz provisional.

---

#### 2.5.4 Clasificación de la Evidencia Fáctica

* **Demostrado por Código:**
  * `SQL_LOCK_CONTENTION` no consulta `pg_locks` ni obtiene nombres de tablas ni modos de bloqueo.
  * `lock_service.py` genera `finding_id = "PG-LOCK-CONTENTION:PID_{root_pid}"` con `object_type="process"`.
  * Ningún adaptador de producción (CLI ni MCP) vuelca locks dentro del contrato canónico `AuditReport`; ambos exponen `LockContentionReport` como flujo independiente.
  * `lock_report_to_findings` existe como función aislada consumida únicamente por su test unitario.
* **Demostrado por Tests:**
  * `tests/test_lock_engine.py` valida la reconstrucción de árboles, la selección de `root_pid` y la asignación del `finding_id` basado en PID.
  * No existen tests que evalúen persistencia diferencial (`DiffEngine`) sobre findings de tipo lock.
* **Documentado como Intención:**
  * [`README.md:31`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/README.md#L31) y [`SKILL.md:32`](file:///C:/Users/cerra/.gemini/config/skills/sql-audit/SKILL.md#L32) describen el módulo como: *"Lock Contention Tree: Grafo recursivo de dependencias en `pg_locks`, detección del PID raíz bloqueante y árbol visual ASCII."* (Nótese que la documentación afirma usar `pg_locks`, aunque el código usa `pg_stat_activity + pg_blocking_pids`).
* **Inferencia:**
  * El autor diseñó la detección de locks primordialmente como una herramienta forense de emergencia para respuesta a incidentes en vivo (`audit_pg.py --locks`), no como una métrica de drift acumulativo entre auditorías mensuales/semanales. La función `lock_report_to_findings` se creó para homogeneizar el modelo hacia `Finding`, pero se usó el PID porque era la única clave primaria disponible en `pg_stat_activity`.
* **Decisión de Producto Pendiente:**
  * Definir si el contrato canónico de locks debe modelarse como un proceso activo (manteniendo `PID`), como un recurso (lo que exige reescribir la query de infraestructura con `pg_locks`), o si debe formalizarse como una capacidad puramente de diagnóstico en tiempo real separada del snapshot diferencial de salud de base de datos.

---

#### 2.5.5 Análisis de Opciones de Producto y Tradeoffs

| Dimensión | Opción A: Mantener Identidad por Proceso (`PID`) | Opción B: Extender Query a Recurso (`pg_locks`) | Opción C: Delimitar como Herramienta Live (On-Demand) |
|---|---|---|---|
| **Definición de Identidad** | `PG-LOCK-CONTENTION:PID_{root_pid}` (`object_type="process"`). | `PG-LOCK-CONTENTION:{schema}.{table}` o tipo de recurso. | No emite `Finding` canónico de drift; emite `LockContentionReport` para inspección viva. |
| **Cambios en SQL** | **Ninguno.** Preserva la query actual sobre `pg_stat_activity`. | **Alto.** Requiere JOIN complejo con `pg_locks`, `pg_class`, `pg_namespace`, etc. | **Ninguno.** |
| **Comportamiento en Diff** | PIDs rotan $\to$ eventos se marcan como `NEW` / `RESOLVED`. Falsa mutación ante reutilización de PID. | Permite rastrear contención persistente sobre una misma tabla a lo largo del tiempo. | Locks no participan en el snapshot diferencial canónico (o se marcan como efímeros). |
| **Problemas de Dominio** | PIDs son efímeros y no representan objetos del esquema. | Un bloqueo puede ser a nivel de tupla, xid o advisory lock (sin tabla asociada). Un proceso puede bloquear 10 tablas simultáneamente. | Requiere formalizar en la documentación que los locks no son parte del health report persistente. |
| **Sobrecarga en Producción** | Mínima (solo `pg_stat_activity`). | Mayor contención al consultar `pg_locks` durante tormentas de bloqueos masivos. | Mínima. |

---

#### 2.5.6 Información Faltante para Decidir
Para evaluar si la Opción B es viable y conveniente, se debe responder a las siguientes restricciones técnicas:
1. **Multiplicidad de recursos:** Si una transacción bloquea 5 tablas en un lote, ¿el proceso genera 5 findings independientes o un solo finding?
2. **Locks no relacionales:** Si el bloqueo es por contención de fila (`tuple`) o transacción (`transactionid`), no existe un nombre de tabla directo.
3. **Propósito del auditor:** ¿El usuario espera que `audit_pg.py --diff baseline.json` alerte sobre bloqueos ocurridos hace 3 días, o la contención de locks es estrictamente una herramienta interactiva para cuando la base de datos "está trabada"?

---

#### 2.5.7 Estado de la Decisión
* **Estado:** `DECIDED`
* **Decisión del propietario:** `LIVE_ON_DEMAND_LOCK_DIAGNOSTIC`
* **Definición formal:**
  1. La contención de locks se mantiene como una capacidad forense live/on-demand y no forma parte actualmente del snapshot diferencial canónico de `AuditReport`.
  2. La identidad `PG-LOCK-CONTENTION:PID_<root_pid>` se considera válida dentro del contexto operacional de una observación live, donde identifica la sesión/proceso raíz observado en ese instante. No se debe interpretar como identidad histórica estable de un incidente.
  3. No se modifica actualmente la infraestructura para convertir la identidad en una identidad basada en recurso, relación o lock.
  4. Tampoco se incorpora el lock graph al flujo canónico de `AuditReport`/`DiffEngine` en esta tarea.
  5. Las opciones de identidad basada en recurso (`pg_locks`, relación, lock mode, tuple, XID, advisory lock, etc.) quedan explícitamente fuera del alcance porque la capacidad actual no recoge suficiente evidencia para establecer una identidad semántica universal y porque locks no constituye actualmente parte del snapshot diferencial canónico.
  6. La función `lock_report_to_findings` no se elimina únicamente por esta decisión; su futuro uso o eliminación se tratará como deuda técnica separada.

---

### 2.6 Decision Record — T-06: Terminación del Grafo de Locks ante Ciclos

#### 2.6.1 Pregunta de Diseño Fundamental
> ¿Qué garantías algorítmicas de terminación debe ofrecer el recorrido del árbol de bloqueos `_traverse_blocking_tree` y cómo debe comportarse el motor semántica y estructuralmente cuando el grafo de dependencias de bloqueos contiene aristas circulares ($A \to B \to A$)?
>
> **Opción A — Recorrido defensivo con detección de aristas de retroceso (Back-edges) y rotulación explícita:** El recorrido DFS rastrea la ruta activa de recursión (`active_path`), detiene la recursión ante cualquier nodo ya presente en la ruta actual, preserva la cardinalidad de procesos únicos en `all_blocked` y emite una marca visual explícita de dependencia circular/deadlock en el árbol de diagnóstico.
>
> **Opción B — Corte silencioso mediante conjunto de visitados global:** Se mantiene un conjunto global `visited: set[int]`; si un nodo ya fue visitado en cualquier rama previa o ciclo, se ignora silenciosamente sin advertencia diagnóstica diferenciada.
>
> **Opción C — Pre-validación topológica / Desacople de ciclos:** Se pre-procesa el grafo con detección de componentes fuertemente conexas (algoritmo de Tarjan o similar) antes de intentar armar representaciones jerárquicas en árbol.

---

#### 2.6.2 Análisis Forense del Recorrido Recursivo Actual

##### 1. Implementación en Código
En [`src/sql_audit/application/lock_service.py:17-54`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/lock_service.py#L17-L54):
```python
def _traverse_blocking_tree(
    parent_pid: int,
    blocking_to_blocked: dict[int, list[int]],
    processes_by_pid: dict[int, LockProcess],
    all_blocked: list[int],
    tree_lines: list[str],
    indent: str = "  ",
) -> float:
    max_duration = 0.0
    children = blocking_to_blocked.get(parent_pid, [])
    for child_pid in children:
        all_blocked.append(child_pid)
        ...
        child_max = _traverse_blocking_tree(
            parent_pid=child_pid,
            blocking_to_blocked=blocking_to_blocked,
            processes_by_pid=processes_by_pid,
            all_blocked=all_blocked,
            tree_lines=tree_lines,
            indent=indent + "    ",
        )
        if child_max > max_duration:
            max_duration = child_max
    return max_duration
```
* **Ausencia de control de estados:** La función no recibe ningún conjunto de nodos visitados.
* **Comportamiento ante ciclos:** Si el diccionario contiene dependencias recíprocas (ej. $PID_A$ bloquea a $PID_B$ y $PID_B$ bloquea a $PID_A$), la llamada entra en recursión infinita no acotada hasta alcanzar `sys.getrecursionlimit()` (~1000 frames), colapsando el proceso con `RecursionError`.
* **Corrupción de colecciones mutables:** En cada iteración del bucle infinito antes del colapso, `all_blocked.append(child_pid)` y `tree_lines.append(...)` continúan acumulando memoria innecesaria.

##### 2. La Paradoja de Selección de Raíces en Código
En [`src/sql_audit/application/lock_service.py:123-128`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/lock_service.py#L123-L128):
```python
# Root blockers: PIDs that are blocking others, but are NOT blocked themselves
root_pids = [pid for pid in blocking_to_blocked if pid not in blocked_to_blocking]

# In case of cycles, fallback to any unvisited blocking pid
if not root_pids and blocking_to_blocked:
    root_pids = list(blocking_to_blocked.keys())[:1]
```
* **Evidencia factual crucial:** El autor del código **anticipó explícitamente la posibilidad de ciclos** (como demuestra el comentario en la línea 126).
* En un ciclo puro ($A \leftrightarrow B$), ningún nodo cumple la condición de no estar bloqueado (`pid not in blocked_to_blocking`), por lo que `root_pids` queda vacío.
* El código activa su fallback (`list(blocking_to_blocked.keys())[:1]`) para elegir un PID arbitrario del ciclo como raíz provisional.
* **La falla de diseño:** El código implementó la selección de raíz para ciclos, pero **omitió implementar el mecanismo de parada en la función recursiva que procesa esa raíz**, garantizando el colapso por `RecursionError` tan pronto el fallback se ejecuta.

---

#### 2.6.3 Distinción Conceptual: Ciclo Algorítmico vs. Ciclo en Producción vs. Deadlock Real

1. **Deadlock real en PostgreSQL:**
   * Es una condición física en el gestor de bloqueos del motor donde dos o más transacciones mantienen bloqueos incompatibles y cada una espera que la otra libere el suyo.
   * PostgreSQL cuenta con un proceso detector de deadlocks que despierta periódicamente según `deadlock_timeout` (por defecto $1000\,\text{ms}$).
   * Si el ciclo persiste tras el timeout, el motor aborta una de las transacciones con el error `40P01 (deadlock_detected)`.
2. **Ciclo observable en producción:**
   * Durante el intervalo de tiempo entre que se cierra la dependencia circular y transcurre el `deadlock_timeout` (hasta 1 segundo por defecto, o mayor en configuraciones específicas), las transacciones conviven físicamente en espera mutua.
   * Si la consulta `SQL_LOCK_CONTENTION` muestrea `pg_stat_activity` y `pg_blocking_pids()` en esa ventana de tiempo, **PostgreSQL reportará filas con dependencias mutuas circulares válidas**.
   * Adicionalmente, transacciones preparadas en dos fases (`PREPARED TRANSACTION`) o bloqueos distribuidos pueden quedar trabadas sin que el detector de deadlocks local las interrumpa automáticamente.
3. **Ciclo algorítmico:**
   * Es una propiedad formal de la estructura de datos del grafo dirigido $G = (V, E)$.
   * El principio de software determinista exige que un algoritmo de inspección ofrezca **garantías formales de terminación en $O(V + E)$** ante cualquier grafo dirigido, independientemente de la frecuencia con la que PostgreSQL entregue o resuelva un ciclo en la práctica.

---

#### 2.6.4 Cobertura de Tests Existente

* **Suite actual ([`tests/test_lock_engine.py`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/tests/test_lock_engine.py)):**
  1. `test_lock_engine_empty_when_no_blocks`: Valida retorno limpio ante lista vacía.
  2. `test_lock_engine_multi_level_blocking_chain`: Valida un grafo acíclico lineal estricto de 4 procesos ($4312 \to 4388 \to 4410 \to 4421$).
  3. `test_cli_locks_contention_flow`: Valida la salida CLI mockeada con un bloqueo simple.
* **Casos NO cubiertos por tests:**
  * Ciclo directo $A \leftrightarrow B$.
  * Ciclo indirecto $A \to B \to C \to A$.
  * Auto-bloqueo $A \to A$.
  * Grafos confluentes acíclicos en diamante ($A \to B \to D$ y $A \to C \to D$).

---

#### 2.6.5 Análisis Semántico y Estructural de la Solución Técnica

Un conjunto de visitados no es una simple bandera de corte; afecta la semántica del diagnóstico:

1. **Distinción entre Confluencia en DAG vs. Ciclo Real:**
   * En PostgreSQL, un proceso bloqueado puede estar esperando simultáneamente por múltiples PIDs (por ejemplo, `pg_blocking_pids()` devuelve un array con varios IDs si requiere un lock exclusivo sobre una tabla donde hay múltiples transacciones concurrentes con locks compartidos).
   * Por ende, un proceso $D$ puede ser hijo en el grafo de $B$ y de $C$. Esto es un DAG acíclico legítimo (diamante), no un ciclo.
   * Si se implementa un `visited` global ciego, el proceso $D$ se procesará en la rama de $B$, pero al llegar por la rama de $C$ se cortará sin mostrar que $C$ también lo bloqueaba.
   * **Requisito algorítmico:** Para detectar ciclos genuinos sin romper confluencias legítimas de DAGs, se debe rastrear la **ruta activa de recursión (ancestros en el call stack actual)**, detectando aristas de retroceso (*back-edges*).
2. **Preservación de Información en el Reporte:**
   * Cuando se detecta un ciclo, el árbol ASCII no debe limitarse a cortar silenciosamente la línea. Debe rotular explícitamente:
     `↓ blocks PID {child_pid} [CYCLE / MUTUAL BLOCK DETECTED]`
   * Esto proporciona alto valor diagnóstico al operador humano o agente LLM, señalando que la base de datos se encontraba en un deadlock transitorio en el instante del muestreo.
3. **Cardinalidad y Métricas:**
   * `all_blocked`: Debe evitar duplicados (o usar un set para cardinalidad) de modo que un proceso involucrado en un ciclo no se contabilice múltiples veces en `tree.total_blocked`.
   * `max_blocked_duration_sec`: La duración máxima debe evaluarse sin caer en sumas o cálculos recursivos redundantes sobre nodos en ciclo.

---

#### 2.6.6 Clasificación de la Evidencia Fáctica

* **Demostrado por Código:**
  * `_traverse_blocking_tree` no mantiene ningún control de visitados y recursa indefinidamente ante ciclos.
  * `build_lock_contention_report` contiene lógica explícita para capturar raíces cíclicas (`# In case of cycles...`), confirmando la intención fáctica de soportar ciclos.
* **Demostrado por Tests:**
  * Todos los tests actuales evalúan únicamente grafos acíclicos lineales o listas vacías. No hay cobertura de grafos cíclicos.
* **Documentado como Intención:**
  * No existe especificación de comportamiento de deadlocks en README ni SKILL.
* **Inferencia:**
  * El autor diseñó la detección de raíces con `root_pids` y contempló el caso cíclico en la selección, pero olvidó propagar la estructura de corte hacia la función recursiva.
* **Decisión de Producto Pendiente:**
  * Formalizar la opción de terminación defensiva y la semántica de representación de ciclos en el árbol de diagnóstico.

---

#### 2.6.7 Estado de la Decisión
* **Estado:** `RESOLVED`
* **Decisión del propietario:** `CYCLE_SAFE_GRAPH_TRAVERSAL` (Opción A).
* **Definición formal:**
  1. `_traverse_blocking_tree` debe garantizar terminación formal ante cualquier grafo dirigido, acíclico o cíclico.
  2. La detección de ciclo debe basarse estrictamente en la pertenencia del siguiente PID a la **ruta activa de recursión (`active_path` / call stack)**, y **no** en un conjunto `visited` global que suprima de forma ciega cualquier PID repetido.
  3. Un PID que converge legítimamente desde dos ramas distintas de un grafo acíclico dirigido (DAG en diamante) no debe interpretarse como ciclo ni provocar el corte de la segunda rama.
  4. Cuando se detecte una arista de retroceso (*back-edge*), la rama debe cortarse de forma controlada y el reporte debe conservar una indicación explícita del ciclo observado en el texto del árbol, en lugar de ocultarlo silenciosamente.
  5. El conteo de procesos bloqueados (`total_blocked`) debe evitar duplicaciones derivadas de ciclos o convergencias (mediante unicidad de PIDs).
  6. La detección algorítmica de un ciclo debe describirse como **"ciclo detectado en el grafo de bloqueo observado"**, y **no** debe elevarse automáticamente a la afirmación categórica de que PostgreSQL confirmó un deadlock en su lock manager.
  7. La existencia o frecuencia real de esos ciclos en producción permanece como cuestión empírica separada; la garantía de terminación algorítmica en $O(V + E)$ no depende de que el evento sea frecuente en la práctica.
  8. **No** se introduce Tarjan/SCC ni infraestructura de detección global de componentes fuertemente conexas, por no estar justificado para la escala y naturaleza del problema actual.
  9. **No** se modifican código, tests, SQL, contratos MCP, `AuditReport`, DiffEngine ni decisiones anteriores en esta fase. T-05 permanece cerrado como `LIVE_ON_DEMAND_LOCK_DIAGNOSTIC`.
  10. **Motivación técnica:** Robustez algorítmica combinada con preservación de la semántica diagnóstica: el objetivo no es meramente evitar `RecursionError`, sino garantizar la terminación sin confundir convergencias legítimas de DAG con ciclos.

---

### 2.7 Decision Record — T-07: Comparación Multi-Evidencia en DiffEngine

#### 2.7.1 Pregunta de Diseño Fundamental
> En virtud de la resolución de T-04 (`UNORDERED_EVIDENCE`), ¿cómo debe implementar `DiffEngine` la comparación de colecciones de `Evidence` entre dos auditorías sucesivas, y cómo debe modelarse el resultado en `ChangedFinding`, `AuditDiff` y `render_diff_text`?
>
> **Opción A — Comparación por Multiset de Identificadores (`sorted(evidence_ids)`):** Se extraen los `evidence_id` de cada finding y se comparan como multiset ordenado. Dos findings son `UNCHANGED` si contienen exactamente las mismas evidencias con igual cardinalidad, independientemente del orden de inserción. `ChangedFinding` se adapta para registrar colecciones de evidencias (`list[str]`) en lugar de escalares individuales.
>
> **Opción B — Comparación por Conjunto Matemático Dedupicado (`set(evidence_ids)`):** Se comparan los identificadores deduplicados. Las evidencias duplicadas dentro de un finding se consideran redundantes y no alteran el estado si los elementos únicos coinciden.
>
> **Opción C — Digest Compuesto de Evidencia:** Se calcula un hash SHA-256 consolidado sobre la lista canónicamente ordenada de `evidence_id` del finding. `ChangedFinding` conserva atributos escalares comparando los dos digests compuestos.

---

#### 2.7.2 Análisis Forense del Estado Actual en Código

##### 1. Defecto Técnico en `diff_service.py:30-31`
```python
prev_eid = prev_f.evidence[0].evidence_id if prev_f.evidence else ""
curr_eid = curr_f.evidence[0].evidence_id if curr_f.evidence else ""

if prev_eid == curr_eid:
    unchanged_findings.append(curr_f)
else:
    changed_findings.append(
        ChangedFinding(
            finding_id=fid,
            check=curr_f.check,
            object_name=curr_f.object_name,
            previous_evidence_id=prev_eid,
            current_evidence_id=curr_eid,
            current_finding=curr_f,
        )
    )
```
* **Ceguera ante evidencias subsiguientes:** Si un finding tiene $N \ge 2$ evidencias y muta `evidence[1]`, el algoritmo evalúa `prev_eid == curr_eid` (sobre `evidence[0]`) y reporta falsamente `UNCHANGED` (falso negativo).
* **Falso positivo ante permutaciones:** Si un finding recibe las mismas evidencias en orden físico distinto (`[A, B]` vs `[B, A]`), `prev_eid` ($A$) difiere de `curr_eid` ($B$), reportando falsamente `CHANGED` (violación directa del contrato de colección no ordenada resuelto en T-04).
* **Ceguera ante adición o remoción:** Si un finding pasa de 1 a 2 evidencias agregando la segunda al final, el algoritmo reporta `UNCHANGED`.

##### 2. Acoplamiento Estructural en `ChangedFinding` ([`diff.py:24-35`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/domain/diff.py#L24-L35))
```python
class ChangedFinding(BaseModel):
    finding_id: str
    check: str
    object_name: str
    previous_evidence_id: str
    current_evidence_id: str
    current_finding: Finding
```
* Los campos `previous_evidence_id: str` y `current_evidence_id: str` están tipados rígidamente como cadenas escalares únicas.
* Este modelado presupone de forma forzada que cada finding solo puede tener una única evidencia asociada, impidiendo representar cambios en hallazgos multi-evidencia.

##### 3. Visualización en `render_diff_text` ([`diff_service.py:94-100`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/diff_service.py#L94-L100))
```python
if diff.changed_findings:
    lines.append("\n[~] PERSISTING FINDINGS WITH CHANGED EVIDENCE:")
    for cf in diff.changed_findings:
        lines.append(f"  ~ {cf.check}: {cf.object_name}")
        lines.append(f"      prev: {cf.previous_evidence_id}")
        lines.append(f"      curr: {cf.current_evidence_id}")
```
* Asume que la visualización textual de una mutación consiste en comparar dos hashes escalares simples.

---

#### 2.7.3 Casos Borde y Semántica de Comparación

1. **Findings sin evidencia (`evidence = []`):**
   * Si ambos findings carecen de evidencia, la comparación de listas vacías debe evaluar como `UNCHANGED`.
   * Si uno tiene evidencia y el otro no, debe evaluar como `CHANGED`.
2. **Evidencias Duplicadas (`[E1, E1]` vs `[E1]`):**
   * Si se utiliza comparación de *multiset* (`sorted(eids)`), `[E1, E1]` difiere de `[E1]` $\to$ `CHANGED`. Esto preserva la información fáctica de que la cardinalidad de observaciones de catálogo aumentó o disminuyó.
   * Si se utiliza *set* deduplicado, `set([E1, E1]) == set([E1])` $\to$ `UNCHANGED`.
3. **Escalabilidad y Rendimiento:**
   * El número de evidencias $K$ asociadas a un finding individual en auditorías de PostgreSQL es pequeño ($K \le 10$, y actualmente en producción $K = 1$).
   * La normalización mediante `sorted([e.evidence_id for e in f.evidence])` tiene un costo temporal despreciable de $O(K \log K)$ y costo espacial $O(K)$.

---

#### 2.7.4 Comparativa de Alternativas Técnicas

| Dimensión | Opción A: Multiset Ordenado (`sorted(ids)`) | Opción B: Conjunto Dedupicado (`set(ids)`) | Opción C: Digest Compuesto (Hash de Hashes) |
|---|---|---|---|
| **Semántica ante Permutaciones** | Inmune a orden: `[A, B]` equivale a `[B, A]`. | Inmune a orden. | Inmune a orden (hashea lista ordenada). |
| **Tratamiento de Duplicados** | Preserva cardinalidad fáctica (`[A, A] != [A]`). | Ignora cardinalidad (`[A, A] == [A]`). | Preserva cardinalidad según la lista de entrada. |
| **Modelo `ChangedFinding`** | Requiere `previous_evidence_ids: list[str]`. | Requiere `previous_evidence_ids: list[str]`. | Preserva escalares `previous_evidence_digest: str`. |
| **Trazabilidad Diagnóstica** | **Alta:** Permite saber con precisión qué IDs de evidencia cambiaron, se agregaron o se quitaron. | Media: Pierde conteo de duplicados. | Baja: Solo informa que el digest global cambió, sin detalle directo. |
| **Complejidad de Implementación** | Mínima (`sorted(eids)`). | Mínima (`set(eids)`). | Requiere función auxiliar de digest compuesto. |

---

#### 2.7.5 Clasificación de la Evidencia Fáctica

* **Demostrado por Código:**
  * `diff_service.py:30-31` accede exclusivamente a `evidence[0]`.
  * `ChangedFinding` modela los identificadores de evidencia como cadenas escalares (`str`).
  * Todos los mappers actuales instancian `evidence` con un solo elemento.
* **Demostrado por Tests:**
  * La suite de `test_diff_engine.py` solo prueba findings con una única evidencia (`_make_finding` usa `evidence=[ev]`). No hay tests de comparación multi-evidencia.
* **Documentado como Intención:**
  * T-04 resolvió formalmente que `Finding.evidence` es una colección no ordenada y que `DiffEngine` debe evaluar la totalidad de las evidencias sin ser sensible al orden de almacenamiento.
* **Inferencia:**
  * El autor diseñó `ChangedFinding` con campos escalares porque en el momento inicial todo finding tenía exactamente una evidencia.
* **Decisión de Producto Pendiente:**
  * Adoptar la estrategia concreta de comparación de colecciones (Multiset vs. Set vs. Digest) y definir la evolución de los campos de `ChangedFinding`.

---

#### 2.7.6 Estado de la Decisión
* **Estado:** `RESOLVED`
* **Decisión del propietario:** `CANONICAL_EVIDENCE_MULTISET` (Opción A).
* **Definición formal:**
  1. `Finding.evidence` es una colección no ordenada cuya **cardinalidad es significativa**. El orden de almacenamiento no tiene significado de dominio, pero las repeticiones no deben eliminarse implícitamente.
  2. La comparación técnica se realiza mediante los `evidence_id` canónicamente ordenados (`sorted(eids)`), preservando la cardinalidad fáctica.
  3. `DiffEngine` debe comparar **todas** las evidencias de un finding, no únicamente `evidence[0]`.
  4. La comparación es formalmente independiente del orden:
     * `[A, B]` frente a `[B, A]` $\to$ `UNCHANGED`.
     * `[A]` frente a `[A, B]` $\to$ `CHANGED`.
     * `[A, A]` frente a `[A]` $\to$ `CHANGED` (la cardinalidad forma parte de la representación observada).
     * `[]` frente a `[]` $\to$ `UNCHANGED`.
     * `[A, B]` frente a `[A, C]` $\to$ `CHANGED`.
  5. **No** se utiliza `set` como representación semántica porque introduciría, sin evidencia suficiente, la regla no demostrada de que las evidencias duplicadas son irrelevantes.
  6. **No** se introduce un digest compuesto SHA-256 como mecanismo principal de comparación: añadiría una capa de abstracción innecesaria y reduciría la trazabilidad diagnóstica disponible en `ChangedFinding`.
  7. `ChangedFinding` debe dejar de asumir conceptualmente un único `previous_evidence_id` / `current_evidence_id` escalar y pasar a representar colecciones de identificadores compatibles con la semántica multi-evidencia. El diseño exacto de dichos campos queda como detalle de implementación de la siguiente fase.
  8. **No** se modifica todavía la signatura de `Finding.evidence: list[Evidence]` únicamente por esta decisión.
  9. **Relación con T-04 (`UNORDERED_EVIDENCE`):** T-04 definió que el orden no tiene significado de dominio; T-07 define la regla operativa de cómo `DiffEngine` compara dicha colección preservando la cardinalidad.

---

### 2.8 Decision Record — T-08: Timeouts y Límites de Ejecución en Consultas (EXPLAIN / Catálogos)

#### 2.8.1 Pregunta de Diseño Fundamental
> Habiéndose resuelto en T-01 la eliminación de `EXPLAIN ANALYZE` (auditoría estrictamente pasiva), ¿la tarea T-08 queda formalmente resuelta como `NOT_APPLICABLE` por desaparecer la ejecución activa de consultas arbitrarias, o subsiste un riesgo operacional que justifique configurar límites de tiempo (`statement_timeout`, `lock_timeout`) sobre las consultas de catálogos y el `EXPLAIN` estático pasivo?
>
> **Opción A — T-08 formalmente `NOT_APPLICABLE` (Alcance original cerrado):** La motivación original de T-08 era mitigar consultas lentas o destructivas ejecutadas con `ANALYZE`. Al retirarse dicha capacidad en T-01, no se introducen parámetros ni configuración de timeouts adicionales; el auditor hereda los timeouts predeterminados del servidor PostgreSQL o de libpq/asyncpg.
>
> **Opción B — Configuración de Timeouts Defensivos a Nivel de Sesión:** Aun en auditoría puramente estática, un `EXPLAIN` pasivo requiere adquirir `AccessShareLock` y puede bloquearse si hay un DDL concurrente con `ACCESS EXCLUSIVE`. Se fijan defensivamente `statement_timeout` y `lock_timeout` en la conexión para garantizar que ninguna consulta de catálogo o simulación cuelgue indefinidamente el proceso o el servidor MCP.
>
> **Opción C — Timeouts Opcionales Parametrizables:** Exponer parámetros configurables en CLI (`--statement-timeout`, `--lock-timeout`) y MCP para que el operador controle los límites si su entorno de producción lo requiere.

---

#### 2.8.2 Análisis Forense del Estado Actual en Código

##### 1. Configuración de Conexión en CLI (`audit_pg.py:311,664`)
* En [`audit_pg.py:311`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L311):
  `conn = psycopg2.connect(self.db_url, connect_timeout=self.connect_timeout)`
* En [`audit_pg.py:664`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L664):
  `connect_timeout: int = 10`
* **Observación fáctica:** Solo se configura `connect_timeout` (tiempo máximo para establecer el socket TCP/TLS).
* **Ausencia de timeouts de ejecución:** No se emite `SET statement_timeout` ni `SET lock_timeout`. Si una consulta de catálogo o un `EXPLAIN` pasivo intenta acceder a una tabla bloqueada por un `ALTER TABLE` o `VACUUM FULL`, el proceso de auditoría queda en espera indefinida.

##### 2. Configuración de Conexión en MCP (`mcp_pg_auditor.py:254,420`)
* En [`mcp_pg_auditor.py:254`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/mcp_pg_auditor.py#L254):
  `conn = await asyncpg.connect(self.dsn, timeout=self.connect_timeout)`
* En [`mcp_pg_auditor.py:420`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/mcp_pg_auditor.py#L420):
  `connect_timeout: int = 10`
* **Observación fáctica:** Solo aplica al handshake inicial de conexión. Las operaciones de lectura de catálogos no tienen límite defensivo de tiempo de consulta ni de bloqueo.

##### 3. Comportamiento Fáctico de `EXPLAIN` Estático en PostgreSQL
* `EXPLAIN (BUFFERS, COSTS, VERBOSE, FORMAT JSON) <query>` no ejecuta la sentencia, por lo que elimina el consumo de CPU/I/O de ejecución de datos y la ejecución de DML.
* Sin embargo, el parser y el planner de PostgreSQL **requieren abrir las relaciones** para leer metadatos, columnas y estadísticas. Esto adquiere un bloqueo `AccessShareLock` sobre cada tabla involucrada.
* Si otra sesión mantiene un bloqueo exclusivo (`ACCESS EXCLUSIVE`, como durante `TRUNCATE`, `VACUUM FULL` o ciertas migraciones DDL), `EXPLAIN` entrará en la cola de espera de locks y quedará trabado hasta que la otra transacción libere el recurso.

---

#### 2.8.3 Clasificación de la Evidencia Fáctica

* **Demostrado por Código:**
  * Ni CLI ni MCP configuran `statement_timeout` o `lock_timeout` en sus sesiones.
  * Solo existe `connect_timeout` (default 10 segundos).
  * `EXPLAIN` estático ejecuta `EXPLAIN (BUFFERS, COSTS, VERBOSE, FORMAT JSON) {query}` sin envoltorio transaccional ni límites de tiempo de sentencia.
* **Demostrado por Tests:**
  * No existen tests que verifiquen timeouts de ejecución o adquisición de locks.
* **Documentado como Intención:**
  * La matriz de control original anotó: *"T-08: Timeouts en EXPLAIN ANALYZE -> Descartado al eliminarse EXPLAIN ANALYZE -> NOT_APPLICABLE"*.
* **Inferencia:**
  * El diseño original asociaba los timeouts exclusivamente al peligro de `EXPLAIN ANALYZE`. Al cerrarse T-01 como pasivo, el riesgo principal quedó neutralizado, pero subsiste la pregunta de robustez operacional para `EXPLAIN` pasivo y catálogos.
* **Decisión de Producto Pendiente:**
  * Definir si T-08 se ratifica formalmente como `NOT_APPLICABLE` (cerrando el tema sin cambios de timeouts) o si se adopta una política de timeouts defensivos para la conexión pasiva.

---

#### 2.8.4 Estado de la Decisión
* **Estado:** `RESOLVED`
* **Decisión del propietario:** `DEFENSIVE_SESSION_TIMEOUTS` (Opción B).
* **Definición formal:**
  1. **Distinción respecto a T-01:** T-01 resolvió la seguridad semántica de la auditoría garantizando que el auditor no ejecute activamente consultas arbitrarias mediante `EXPLAIN ANALYZE`. T-08 aborda un riesgo residual diferente que persiste tras T-01: incluso el `EXPLAIN` estático y las consultas de catálogos pueden quedar esperando locks o bloqueados indefinidamente por transacciones externas.
  2. **Insuficiencia de `connect_timeout`:** `connect_timeout` acota únicamente la negociación del socket de red; no acota la vida de sentencias que ya se encuentran en ejecución o esperando locks dentro de PostgreSQL. La ausencia de `EXPLAIN ANALYZE` no hace que los límites de ejecución sean innecesarios.
  3. **Objetivo de los límites:** Garantizar una **terminación operacional acotada** de la sesión del auditor, evitando que un proceso CLI o servidor MCP quede trabado indefinidamente por condiciones externas de la base de datos. No se pretende ofrecer una garantía absoluta de disponibilidad global, sino acotar la sesión propia del auditor.
  4. **Unificación contractual:** Los límites deben definirse de forma **centralizada y coherente con el contrato canónico del auditor**, evitando que CLI y MCP desarrollen políticas o comportamientos de timeout divergentes (alineado con T-02).
  5. **Desacople de transporte:** No se introducen en esta fase flags o parámetros independientes en CLI/MCP; la parametrización por transporte se considera una cuestión posterior de configuración.
  6. **Sin números arbitrarios:** No se fijan constantes numéricas rígidas en esta decisión sin justificación documental; los valores concretos, configuración y mecanismo técnico se definirán en la fase de implementación.
  7. **Tratamiento ante expiración:** Si una consulta excede el timeout defensivo, debe tratarse como un fallo de ejecución del check y alinearse con la semántica de auditoría parcial resiliente (T-03); **nunca** debe interpretarse silenciosamente como ausencia de problemas.
  8. **Relación con decisiones anteriores:**
     * **T-01:** Auditoría estrictamente pasiva sin ejecución de sentencias arbitrarias.
     * **T-02:** Contrato semántico unificado entre CLI y MCP.
     * **T-03:** Fallos localizados de checks producen auditoría parcial explícitamente degradada.
     * **T-08:** Límites defensivos de sesión compartidos por ambos adaptadores.

---

### 2.9 Decision Record — T-09: Política de Recomendaciones Operativas (`VACUUM FULL`, `CONCURRENTLY`)

#### 2.9.1 Pregunta de Diseño Fundamental
> ¿Cuál debe ser la política rectora del auditor respecto a la emisión de sugerencias de remediación operacionales y sentencias DDL en sus salidas textuales y documentación, particularmente respecto a comandos altamente bloqueantes (`VACUUM FULL`, `DROP INDEX` sin `CONCURRENTLY`) y restricciones de contexto transaccional?
>
> **Opción A — Política Estricta de Cero Impacto Operacional (Zero-Downtime):** Prohibir de forma terminante sugerir `VACUUM FULL` en cualquier salida del sistema. Reemplazarlo por recomendaciones de ajuste de parámetros de autovacuum (`autovacuum_vacuum_scale_factor`) o mención de herramientas de reescritura online fuera de línea (`pg_repack`). Exigir que todo comando DDL de índices incluya obligatoriamente la cláusula `CONCURRENTLY` junto con la advertencia explícita de que no puede ejecutarse dentro de bloques de transacción (`BEGIN ... COMMIT`).
>
> **Opción B — Sugerencias Condicionadas con Advertencia Severa de Bloqueo:** Permitir la sugerencia de comandos bloqueantes como `VACUUM FULL`, pero acompañándolos imperativamente de una advertencia destacada sobre el bloqueo exclusivo de lecturas y escrituras (`ACCESS EXCLUSIVE`) y la necesidad imperiosa de ejecutarlo dentro de una ventana de mantenimiento programada.
>
> **Opción C — Supresión de Comandos SQL de Remediación en CLI (Desacople Puro):** Limitar las sugerencias textuales del auditor exclusivamente al diagnóstico fáctico y a la explicación del principio de base de datos afectado, absteniéndose de emitir cadenas de comandos SQL ejecutables para evitar que operadores o agentes LLM los ejecuten de forma ciega en producción.

---

#### 2.9.2 Frontera Fáctica del Repositorio

##### 1. Código Actual en `audit_pg.py`
* En [`audit_pg.py:527-529`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L527-L529) (HOT updates & fillfactor):
  ```python
  lines.append(
      f"    - Suggestion: ALTER TABLE {issue.table_name} "
      f"SET (fillfactor = 85); VACUUM FULL {issue.table_name};"
  )
  ```
  * **Comportamiento observado:** Recomienda textualmente `VACUUM FULL {table};` sin advertencia de bloqueo.
* En [`audit_pg.py:504`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L504) (Índices redundantes):
  ```python
  lines.append(f"    - Suggestion: Consider DROP INDEX {idx.redundant_index};")
  ```
  * **Comportamiento observado:** Sugiere `DROP INDEX` omitiendo la cláusula `CONCURRENTLY`.
* En [`audit_pg.py:456`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L456) (Índices inválidos):
  ```python
  lines.append(f"    - Suggestion: DROP INDEX CONCURRENTLY {inv.invalid_index}; ...")
  ```
  * **Comportamiento observado:** Aquí sí incluye `CONCURRENTLY`.
* En [`audit_pg.py:471`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L471) (FKs sin indexar):
  ```python
  lines.append("    - Suggestion: Consider CREATE INDEX CONCURRENTLY to prevent table locks.")
  ```
  * **Comportamiento observado:** Advierte sobre locks y sugiere `CONCURRENTLY`.

##### 2. Discrepancia Documental y de UX
* En [`README.md:188`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/README.md#L188), el ejemplo de salida textual documenta:
  `- Suggestion: Consider DROP INDEX CONCURRENTLY ix_pedidos_cliente_id;`
  En contradicción directa con el código real de [`audit_pg.py:504`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L504) que emite `DROP INDEX` sin `CONCURRENTLY`.
* En [`SKILL.md:105`](file:///C:/Users/cerra/.gemini/config/skills/sql-audit/SKILL.md#L105) (Guía de Remediación Zero-Downtime):
  * Recomienda: *"Ajuste de autovacuum_vacuum_scale_factor a valores más agresivos (ej. 0.05) o pg_repack online."*
  * **En ningún lugar de `SKILL.md` se sugiere o autoriza `VACUUM FULL`**.
* En [`models.py:4`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/domain/models.py#L4):
  * Se establece como principio de diseño: *"Decouples diagnostic evidence from remediation actions"*. Ni `Finding` ni `Evidence` poseen atributos de remediación en el contrato de dominio canónico.

---

#### 2.9.3 Semántica Técnica de las Operaciones en PostgreSQL

1. **`VACUUM FULL`:**
   * **Bloqueo:** Adquiere bloqueo `ACCESS EXCLUSIVE` sobre la tabla y todos sus índices asociados.
   * **Impacto en concurrencia:** Bloquea de forma absoluta todas las lecturas (`SELECT`) y todas las escrituras (`INSERT`, `UPDATE`, `DELETE`).
   * **Duración y riesgo:** En tablas con millones de filas o de varios gigabytes, la reescritura física completa toma de minutos a horas. Si un operador o asistente ejecuta este comando en un entorno productivo activo, provoca una **caída de servicio inmediata (outage) por denegación de servicio transaccional**.
   * **Espacio en disco:** Requiere aproximadamente el doble del espacio de la tabla mientras se genera la copia física.
2. **`DROP INDEX CONCURRENTLY` / `CREATE INDEX CONCURRENTLY`:**
   * **Bloqueo:** Adquiere bloqueo `SHARE UPDATE EXCLUSIVE`.
   * **Impacto en concurrencia:** Permite que las operaciones de lectura y modificación concurrente sobre la tabla continúen sin interrupción.
   * **Restricciones transaccionales:** **No puede ejecutarse dentro de una transacción** (`ERROR: DROP INDEX CONCURRENTLY cannot run inside a transaction block`). Además, debe esperar a que concluyan todas las transacciones concurrentes activas que contengan un snapshot que pudiera ver el índice.
3. **Alternativas Online para Compactación de Bloat:**
   * Ajuste de parámetros de autovacuum (`autovacuum_vacuum_scale_factor`, `autovacuum_vacuum_cost_limit`).
   * Extensión / herramienta externa online como `pg_repack`.

---

#### 2.9.4 Comparativa de Opciones de Política

| Criterio | Opción A: Política Zero-Downtime Estricta | Opción B: Sugerencias con Advertencia Severa | Opción C: Supresión de DDL en Salidas |
|---|---|---|---|
| **Tratamiento de `VACUUM FULL`** | **Eliminado por completo.** Se reemplaza por ajuste de autovacuum o mención de `pg_repack`. | Se mantiene pero con advertencia en mayúsculas de bloqueo `ACCESS EXCLUSIVE`. | Se elimina toda sentencia DDL directa. |
| **Tratamiento de `DROP INDEX`** | Se añade obligatoriamente `CONCURRENTLY` y advertencia de no usar en transacciones. | Se sugiere `CONCURRENTLY` como recomendación secundaria. | No se emite sentencia SQL. |
| **Seguridad para Agentes / Automatización** | **Máxima:** Previene que un LLM copie comandos bloqueantes en un playbook o shell. | Media: Depende de que el agente lea e interprete la advertencia textual. | Alta: El agente debe deducir la remediación según su propio conocimiento. |
| **Alineación con SKILL.md y README** | Total: Unifica el código con la guía de remediación de `SKILL.md` y el ejemplo de `README.md`. | Parcial: Mantiene divergencia con la filosofía de `SKILL.md`. | Rompe la UX actual de la CLI textual. |

---

#### 2.9.5 Clasificación de la Evidencia Fáctica

* **Demostrado por Código:**
  * `audit_pg.py:528` emite `VACUUM FULL {table};` sin advertencia alguna.
  * `audit_pg.py:504` emite `DROP INDEX` sin `CONCURRENTLY`.
  * `models.py` desacopla el dominio de remediaciones (las remediaciones solo residen en `render_text`).
* **Demostrado por Tests:**
  * `tests/test_render.py:30` valida que la cadena `ALTER TABLE orders SET (fillfactor = 85)` esté presente, pero no valida ni restringe la presencia de `VACUUM FULL`.
* **Documentado como Intención:**
  * `SKILL.md:94-112` declara explícitamente una filosofía de *"Remediación Zero-Downtime"*, exigiendo `CONCURRENTLY` y vetando `VACUUM FULL` a favor de `autovacuum` o `pg_repack`.
  * `README.md:188` ilustra `DROP INDEX CONCURRENTLY`.
* **Inferencia:**
  * La presencia de `VACUUM FULL` en `audit_pg.py` fue una sugerencia rápida de prototipado que contradice los principios de seguridad en producción y zero-downtime enunciados en el resto del proyecto.
* **Decisión de Producto Pendiente:**
  * Definir la política oficial para `render_text` en el CLI (Opción A, B o C).

---

#### 2.9.6 Estado de la Decisión
* **Estado:** `RESOLVED`
* **Decisión del propietario:** `SUPPRESS_EXECUTABLE_REMEDIATION` (Opción C).
* **Definición formal:**
  1. `sql-audit-mcp` es formalmente una herramienta de **diagnóstico y generación de evidencia fáctica**, no un motor de remediación operacional.
  2. Las salidas y recomendaciones del auditor **no deben emitir comandos SQL ejecutables** que impliquen modificar o alterar el estado de PostgreSQL (`VACUUM FULL`, `ALTER TABLE ... SET`, `DROP INDEX`, `DROP INDEX CONCURRENTLY` u otras operaciones DDL/DML equivalentes).
  3. La herramienta sí puede y debe explicar el problema detectado, el principio de arquitectura de PostgreSQL involucrado, los riesgos operativos conocidos y las alternativas conceptuales que el operador debe evaluar.
  4. La ausencia de instrucciones SQL ejecutables no implica ocultar la remediación: el objetivo rector es separar de forma estricta **diagnóstico/evidencia** de **decisión/ejecución de remediación**.
  5. Esta definición se alinea explícitamente con el principio fundacional del dominio ([`models.py:4`](file:///c:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/domain/models.py#L4)): *"Decouples diagnostic evidence from remediation actions"*.
  6. No corresponde que el auditor determine unilateralmente si una organización debe aplicar `VACUUM FULL`, `pg_repack`, ajustes de escala de autovacuum o `DROP INDEX CONCURRENTLY`. Dicha elección depende del contexto de negocio, ventanas de mantenimiento y políticas de infraestructura que el auditor no conoce ni debe presuponer.
  7. No se adopta como contrato general una política universal de "zero-downtime" que asuma erróneamente que todas las organizaciones comparten las mismas restricciones y posibilidades operativas.
  8. Tampoco se adopta una política de emitir comandos peligrosos acompañados únicamente de advertencias textuales: una advertencia no convierte una instrucción en segura ni exime de la separación de responsabilidades.
  9. La discrepancia histórica entre el código (`DROP INDEX`) y el ejemplo de `README.md` (`DROP INDEX CONCURRENTLY`) queda resuelta conceptualmente: el contrato del auditor ya no emitirá ninguno de dichos comandos como instrucción ejecutable.
  10. Esta política afecta exclusivamente a la **capa de presentación y recomendación** (`render_text`), sin alterar el modelo canónico. `AuditReport` y `Finding` continúan representando hechos observados y evidencias deterministas, no acciones.
  11. **No** se introduce ningún motor de remediación (*remediation engine*), motor de políticas ni capas de abstracción adicionales: la decisión fija un límite defensivo de responsabilidad.
  12. **Relación con decisiones anteriores:**
      * **T-01:** Auditoría estrictamente pasiva.
      * **T-02:** CLI y MCP comparten el mismo contrato semántico canónico.
      * **T-03:** Fallos de checks producen reportes parciales explícitamente degradados.
      * **T-08:** Límites defensivos compartidos para la ejecución de consultas.
      * **T-09:** El auditor diagnostica y documenta evidencia fáctica, pero no prescribe comandos ejecutables de remediación.

---

### 2.10 Decision Record — T-10: Alineación y Veracidad Documental (README.md / SKILL.md)

#### 2.10.1 Pregunta de Diseño Fundamental
> ¿Cómo deben actualizarse los artefactos documentales del proyecto ([`README.md`](file:///c:/Users/cerra/codigo/Herraminetas_Sql/README.md) y [`SKILL.md`](file:///c:/Users/cerra/.gemini/config/skills/sql-audit/SKILL.md)) para reflejar con estricta veracidad las decisiones de arquitectura de Fase 0 (T-01 a T-09) y erradicar afirmaciones absolutas o divergencias fácticas con el código real?
>
> **Opción A — Sincronización Rigurosa con Veracidad Fáctica (Evidence over Claims):** Corregir todas las afirmaciones categóricas no demostrables (ej. "exact bloat", "O(1) catalog queries", "zero-downtime remediation", "pg_locks"), eliminar toda mención a `--analyze`, sincronizar los flags y umbrales de ejemplos con el contrato canónico unificado, y adaptar la guía de remediación a principios explicativos sin comandos de consola ciegos.
>
> **Opción B — Actualización Mínima Conservadora:** Limitar los cambios documentales a remover `--analyze` y ajustar los comandos modificados, preservando las descripciones comerciales o heurísticas actuales ("sub-second O(1)", "exact bloat").

---

#### 2.10.2 Análisis Forense de Afirmaciones Documentales vs. Evidencia en Código

##### 1. Retiro de `EXPLAIN ANALYZE` (Consecuencia de T-01)
* **README.md:** Líneas 44, 62, 107 y tabla de capacidades describen `--analyze` como "Safe execution via auto-rollback transactions".
* **SKILL.md:** Líneas 34 y 62 describen `--analyze` con `ROLLBACK` automático.
* **Frontera fáctica:** T-01 resolvió eliminar `EXPLAIN ANALYZE` del contrato por no ser pasivo y por los riesgos no acotados de transacciones mutantes y triggers. Ambas documentaciones deben purgar toda referencia a `--analyze`.

##### 2. Afirmación de "Exact Bloat Percentages" (Frontera Heurística)
* **README.md:39:** *"Provides exact bloat percentages and bytes wasted"*.
* **Código fáctico ([`queries.py:404,656`](file:///c:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/infrastructure/queries.py#L404)):** Las consultas calculan una estimación basada en `pg_stats.avg_width` (que es una muestra probabilística de ANALYZE) y asumen un fillfactor fijo de 0.70 sin inspeccionar tuplas físicas (no usa `pgstattuple`).
* **Ajuste requerido:** Declarar honestamente que es una estimación estadística de densidad de páginas esperadas vs. reales basada en metadatos de catálogo.

##### 3. Afirmación de "sub-second O(1) catalog queries"
* **README.md:12** y **SKILL.md:12:** *"diagnóstico determinista... en O(1) sin alucinaciones"*.
* **Código fáctico ([`queries.py:218,606`](file:///c:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/infrastructure/queries.py#L218)):** Las consultas cruzan catálogos relacionales (`pg_index`, `pg_class`, `pg_stat_user_tables`) cuyo tiempo es proporcional al número de tablas e índices del esquema ($O(N)$ o $O(N \log N)$).
* **Ajuste requerido:** Sustituir la afirmación teórica de $O(1)$ por una descripción precisa de consultas directas de solo lectura sobre catálogos del sistema sin consumo de VRAM.

##### 4. Afirmación de "pg_locks" en Detección de Bloqueos
* **README.md:31** y **SKILL.md:32:** *"Grafo recursivo de dependencias en pg_locks..."*.
* **Código fáctico ([`queries.py:353`](file:///c:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/infrastructure/queries.py#L353)):** La consulta no accede a `pg_locks`; consulta `pg_stat_activity` y `pg_blocking_pids()`.
* **Ajuste requerido:** Corregir la referencia para declarar que reconstruye árboles de contención mediante `pg_blocking_pids()` y `pg_stat_activity`.

##### 5. Divergencia en Umbrales de Ejemplos CLI vs. Defaults del Motor (Consecuencia de T-02)
* **README.md:82:** `--min-size 10000000 --min-table-rows 1000`.
* **Código CLI ([`audit_pg.py:734,784`](file:///c:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L734)):** Defaults reales son `min_size=0` y `min_table_rows=10000`.
* **Ajuste requerido:** Sincronizar los ejemplos con los valores canónicos unificados resueltos en T-02.

##### 6. Supresión de Comandos Ejecutables en Salidas (Consecuencia de T-09)
* **README.md:188:** Muestra `- Suggestion: Consider DROP INDEX CONCURRENTLY ix_pedidos_cliente_id;`.
* **SKILL.md:94-112:** Enuncia comandos DDL directos en la sección de remediación.
* **Ajuste requerido:** Alinear ejemplos y redacción con T-09, explicando los principios diagnósticos y riesgos sin emitir recetas SQL ciegas.

---

#### 2.10.3 Clasificación de la Evidencia Fáctica

* **Demostrado por Código y Docs:** Todas las discrepancias citadas están demostradas por líneas exactas de código y documentación activa en el repositorio.
* **Decisión de Producto Pendiente:**
  * Aprobar formalmente el alcance de la sincronización documental (Opción A vs. Opción B).

---

#### 2.10.4 Estado de la Decisión
* **Estado:** `RESOLVED`
* **Decisión del propietario:** `EVIDENCE_ALIGNED_DOCUMENTATION` (Opción A).
* **Definición formal:**
  1. **Alineación rigurosa con la evidencia (Evidence over Claims):** La documentación del proyecto ([`README.md`](file:///c:/Users/cerra/codigo/Herraminetas_Sql/README.md) y [`SKILL.md`](file:///c:/Users/cerra/.gemini/config/skills/sql-audit/SKILL.md)) debe describir con veracidad fáctica el comportamiento verificable del código y el contrato de dominio, absteniéndose de enunciar capacidades no soportadas o afirmaciones más fuertes que la evidencia disponible.
  2. **EXPLAIN ANALYZE:** Eliminar de README y SKILL todas las instrucciones, flags, ejemplos y afirmaciones que presenten `--analyze` como capacidad disponible. La documentación debe reflejar estrictamente la decisión T-01: auditoría puramente pasiva y observacional. Queda prohibido documentar el rollback transaccional como un mecanismo que convierta una ejecución activa en una operación de solo lectura.
  3. **Estimación de Bloat:** Eliminar afirmaciones categóricas como *"exact bloat percentages and bytes wasted"*. Describir el cálculo con precisión técnica como una estimación estadística/heurística basada en densidad de páginas esperadas vs. reales a partir de `pg_stats.avg_width` (muestra probabilística de ANALYZE) y factores de relleno de catálogo, sin inspección de tuplas físicas ni uso de extensiones intrusivas.
  4. **Rendimiento:** Eliminar claims categóricos de "O(1)" y "sub-second" desligados del tamaño del catálogo. Describir las consultas como lecturas SQL directas y optimizadas sobre metadatos relacionales (`pg_catalog`, `pg_stat`), reconociendo que su tiempo de respuesta depende del volumen de relaciones e índices del esquema ($O(N)$ o $O(N \log N)$).
  5. **Detección de Bloqueos / Grafo de Locks:** Corregir la afirmación de que el grafo de locks inspecciona `pg_locks`. La documentación debe reflejar que el diagnóstico live actual se construye a partir de `pg_stat_activity` y `pg_blocking_pids()`.
  6. **Umbrales Canónicos en Ejemplos:** Sincronizar todos los ejemplos de la documentación (línea de comandos, JSON, llamadas a herramientas MCP) para que utilicen los umbrales canónicos unificados acordados en T-02, eliminando divergencias entre ejemplos y valores predeterminados reales.
  7. **Recomendaciones Operativas:** Alinear README y SKILL con la decisión T-09: suprimir la emisión de comandos SQL ejecutables (`VACUUM FULL`, `DROP INDEX`, etc.) y presentar las recomendaciones como principios arquitectónicos, riesgos operacionales y alternativas a evaluar por el operador.
  8. **Auditorías Parciales y Degradadas:** Reflejar que la presencia de errores en la ejecución de checks produce un reporte formalmente parcial y degradado (T-03), impidiendo la interpretación errónea de `findings=[]` como base de datos sana cuando la auditoría estuvo incompleta.
  9. **Timeouts Defensivos:** Documentar la existencia de timeouts defensivos a nivel de sesión (`statement_timeout`, `lock_timeout`) como salvaguarda operacional pasiva (T-08).

---

### 2.11 Decision Record — T-11: Parámetro `object_type` en Hasher (Identidad de Findings)

#### 2.11.1 Pregunta de Diseño Fundamental
> En [`src/sql_audit/domain/hasher.py:15-32`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/domain/hasher.py#L15-L32), la función `compute_stable_finding_id(check_code, object_type, object_name, sub_object=None)` recibe el parámetro `object_type: str`, pero dicho parámetro no se utiliza en absoluto en el formato de salida (`{CHECK_CODE}:{object_name}[:{sub_object}]`) ni en su docstring. Además, en los llamadores de [`mapping.py`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/mapping.py), se pasa `"table"` como `object_type` incluso para hallazgos cuyo `Finding.object_type` real es `"index"` o `"constraint"`.
> ¿Cómo debe resolverse este parámetro en la arquitectura del auditor?
>
> **Opción A — Eliminar el parámetro huérfano `object_type` de la signatura:** Modificar la signatura canónica a `compute_stable_finding_id(check_code: str, object_name: str, sub_object: str | None = None)`. Limpiar los call sites en `mapping.py`, `bloat_service.py`, `lock_service.py` y tests. Preservar intacto el formato determinista existente `{CHECK_CODE}:{object_name}[:{sub_object}]`, eliminando la inconsistencia y el código engañoso sin alterar los `finding_id` generados históricamente.
>
> **Opción B — Incorporar `object_type` en el formato canónico de `finding_id`:** Modificar la generación para incluir `{CHECK_CODE}:{object_type}:{object_name}[:{sub_object}]`. Corregir las llamadas en `mapping.py` que pasan erróneamente `"table"` en vez del tipo real (`"index"` o `"constraint"`). Modificar los tests de estabilidad de ID para reflejar el nuevo formato.
>
> **Opción C — Mantener `object_type` como parámetro no utilizado (deprecado/ignorado):** Mantener la signatura actual por compatibilidad hacia atrás conservadora (usando `_object_type: str | None = None`), documentando explícitamente en el docstring que no forma parte de la clave de identidad de dominio.

---

#### 2.11.2 Análisis Forense del Estado Actual en Código

##### 1. Implementación en `hasher.py:15-32`
```python
def compute_stable_finding_id(
    check_code: str,
    object_type: str,
    object_name: str,
    sub_object: str | None = None,
) -> str:
    """Computes a stable, counter-independent identity for a finding.

    Format:
        {CHECK_CODE}:{object_name} or {CHECK_CODE}:{object_name}:{sub_object}
    """
    clean_code = check_code.strip().upper()
    clean_obj = object_name.strip()
    if sub_object:
        clean_sub = sub_object.strip()
        return f"{clean_code}:{clean_obj}:{clean_sub}"
    return f"{clean_code}:{clean_obj}"
```
* **Observación fáctica:** `object_type` se recibe en la signatura pero jamás se referencia en el cuerpo de la función. El docstring formalmente documenta que el formato es `{CHECK_CODE}:{object_name}` o `{CHECK_CODE}:{object_name}:{sub_object}`.

##### 2. Llamadores y Discrepancias en `mapping.py`
* En [`mapping.py:43`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/mapping.py#L43) (`map_invalid_index_to_finding`):
  `compute_stable_finding_id("PG-INDEX-INVALID", "table", child_table, invalid_index)`
  * Pasa `"table"` como `object_type`, pero construye `Finding(..., object_type="index", object_name=f"{child_table}.{invalid_index}")`.
* En [`mapping.py:80`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/mapping.py#L80) (`map_unindexed_fk_to_finding`):
  `compute_stable_finding_id("PG-FK-UNINDEXED", "table", child_table, fk_name)`
  * Pasa `"table"` como `object_type`, pero construye `Finding(..., object_type="constraint", object_name=f"{child_table}.{fk_name}")`.
* En [`mapping.py:214`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/mapping.py#L214) (`map_redundant_index_to_finding`):
  `compute_stable_finding_id("PG-INDEX-REDUNDANT", "table", table_name, redundant_index)`
  * Pasa `"table"`, pero `Finding(..., object_type="index")`.
* En [`mapping.py:259`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/mapping.py#L259) (`map_low_usage_index_to_finding`):
  `compute_stable_finding_id("PG-INDEX-LOW-USAGE", "table", table_name, index_name)`
  * Pasa `"table"`, pero `Finding(..., object_type="index")`.

##### 3. Llamadores en Servicios y Tests
* En [`bloat_service.py:122,170`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/bloat_service.py#L122):
  Pasa `"table"` para `PG-BLOAT-TABLE` y `"index"` para `PG-BLOAT-INDEX`.
* En [`lock_service.py:212`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/lock_service.py#L212):
  Pasa `"process"` para `PG-LOCK-CONTENTION`.
* En [`tests/test_domain_models.py:18-20`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/tests/test_domain_models.py#L18-L20):
  Valida expresamente: `assert fid_run1 == "PG-VACUUM-DEAD-TUPLES:public.events"`.

##### 4. Evaluación de Riesgo de Colisión
* ¿Aporta `object_type` entropía o desambiguación a la identidad del finding?
* Cada `check_code` (`PG-INDEX-INVALID`, `PG-FK-UNINDEXED`, `PG-VACUUM-DEAD-TUPLES`, `PG-HOT-FILLFACTOR`, `PG-INDEX-REDUNDANT`, `PG-INDEX-LOW-USAGE`, `PG-BLOAT-TABLE`, `PG-BLOAT-INDEX`, `PG-LOCK-CONTENTION`) califica intrínsecamente el tipo de chequeo y el tipo de objeto auditado.
* Dentro de un `check_code` dado, el nombre del objeto (`clean_obj` + `clean_sub`) identifica de manera unívoca la relación, índice o proceso en el catálogo de PostgreSQL.
* Por lo tanto, no existe riesgo de colisión al omitir `object_type` del formato de `finding_id`.

---

#### 2.11.3 Clasificación de la Evidencia Fáctica

* **Demostrado por Código:**
  * `object_type` no se usa en `hasher.py:15-32`.
  * Los llamadores de `mapping.py` pasan valores incoherentes con el `Finding.object_type` final (pasan `"table"` para índices y restricciones).
* **Demostrado por Tests:**
  * `test_domain_models.py:20` demuestra que el contrato esperado de salida no incluye `object_type`.
* **Documentado como Intención:**
  * El docstring de `compute_stable_finding_id` documenta explícitamente el formato `{CHECK_CODE}:{object_name}[:{sub_object}]`.
* **Inferencia:**
  * El parámetro fue diseñado preliminarmente como parte de una signatura tentativa y luego abandonado sin limpiar la signatura ni los llamadores.
* **Decisión de Producto Pendiente:**
  * Aprobar la estrategia de resolución de la signatura (Opción A, B o C).

---

#### 2.11.4 Estado de la Decisión
* **Estado:** `RESOLVED`
* **Decisión del propietario:** `REMOVE_ORPHANED_OBJECT_TYPE` (Opción A).
* **Definición formal:**
  1. `object_type` no forma parte actualmente de la identidad estable del finding.
  2. El parámetro debe eliminarse de la signatura de `compute_stable_finding_id` porque es un argumento recibido pero semánticamente ignorado tanto en la implementación como en las pruebas de dominio.
  3. El contrato canónico de la función queda formalmente establecido como:
     ```python
     def compute_stable_finding_id(
         check_code: str,
         object_name: str,
         sub_object: str | None = None,
     ) -> str:
     ```
  4. El formato de salida permanece exactamente como:
     `{CHECK_CODE}:{object_name}` o `{CHECK_CODE}:{object_name}:{sub_object}`
  5. **No** se modifica el formato de `finding_id`.
  6. **No** se incorpora `object_type` al hash/ID ni se migran o regeneran IDs históricos.
  7. Se corregirán todos los call sites afectados durante la fase de implementación ([`mapping.py`](file:///c:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/mapping.py), [`bloat_service.py`](file:///c:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/bloat_service.py), [`lock_service.py`](file:///c:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/lock_service.py), tests y cualquier otro consumidor encontrado).
  8. Se mantiene `Finding.object_type` como atributo descriptivo de primer nivel del finding en el modelo [`Finding`](file:///c:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/domain/models.py): eliminar el parámetro huérfano de `compute_stable_finding_id` no implica eliminar el campo del modelo de dominio, ya que conserva plena utilidad para presentación, filtrado y semántica diagnóstica.
  9. Se actualizará el docstring de `compute_stable_finding_id` para que describa con exactitud los argumentos que realmente participan en la identidad.
  10. Se conservarán y ampliarán los tests necesarios para demostrar que el contrato del ID permanece estable tras la retirada del parámetro.
  11. **Justificación de diseño:**
      * No se conserva `object_type` como parámetro ignorado por supuesta compatibilidad hacia atrás cuando no existe evidencia de consumidores externos que dependan de esa signatura interna. Mantenerlo introduce una falsa señal semántica y promueve que desarrolladores futuros crean erróneamente que modifica la identidad.
      * No se selecciona la **Opción B** porque incorporaría `object_type` a un identificador declarado estable, rompiendo la identidad histórica sin ninguna necesidad funcional demostrada ni riesgo de colisión.
      * No se selecciona la **Opción C** porque perpetúa una API engañosa donde el parámetro continúa existiendo sin ningún efecto.
  12. **Relación con T-10 (`EVIDENCE_ALIGNED_DOCUMENTATION`):** La documentación y el código deben describir el mismo contrato fáctico. Un parámetro que no participa en la identidad no debe aparentar formar parte de ella.

---

### 2.12 Decision Record — T-12: Duplicación DTOs Issue CLI vs MCP

#### 2.12.1 Pregunta de Diseño Fundamental
> ¿La duplicación estructural de los DTOs de `Issue` entre el CLI ([`audit_pg.py:79-137`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L79-L137)) y el servidor MCP ([`mcp_pg_auditor.py:61-127`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/mcp_pg_auditor.py#L61-L127)) representa una divergencia o riesgo semántico para el contrato canónico (`AuditReport`), o constituye una separación legítima de responsabilidades entre adaptadores de transporte desacoplados (`LEGITIMATE_ADAPTER_DUPLICATION`)?
>
> **Opción A — Mantener DTOs separados en sus respectivos adaptadores (`LEGITIMATE_ADAPTER_DUPLICATION` / Recomendada):** Reconocer que cada adaptador maneja la representación de datos idiomática para su runtime (standard library `@dataclass(frozen=True)` para la CLI sync sin dependencias de terceros; `pydantic.BaseModel` para el servidor FastMCP async requerido para generación de esquemas JSON/OpenAPI). La equivalencia semántica queda protegida en la capa de aplicación (`mapping.py`) y demostrada mediante tests de contrato canónico.
>
> **Opción B — Extraer DTOs Pydantic compartidos en módulo de aplicación:** Mover los modelos a `src/sql_audit/application/dtos.py` y hacer que tanto CLI como MCP los consuman. Elimina ~60 líneas de código duplicado, pero impone la dependencia pesada de `pydantic` en el script autónomo de CLI y acopla el núcleo de aplicación a estructuras intermedias legacy.
>
> **Opción C — Reemplazo directo por el contrato canónico (Bypass de DTOs legacy):** Refactorizar los métodos de auditoría en ambos adaptadores para que construyan directamente `Finding` y `Evidence` sin instanciar DTOs intermedios. Elimina los DTOs legacy, pero altera la API pública expuesta por las herramientas FastMCP (`pg_health_audit -> PostgresHealthReport`) y el formato textual legacy de la CLI.

---

#### 2.12.2 Análisis Forense del Estado Actual en Código

##### 1. Inventario Exacto de Modelos Duplicados
* **En CLI ([`audit_pg.py:79-145`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L79-L145)):**
  * `HotUpdateIssue` (`@dataclass(frozen=True)`)
  * `RedundantIndexIssue` (`@dataclass(frozen=True)`)
  * `LowUsageIndexIssue` (`@dataclass(frozen=True)`)
  * `InvalidIndexIssue` (`@dataclass(frozen=True)`)
  * `UnindexedFKIssue` (`@dataclass(frozen=True)`)
  * `DeadTuplesIssue` (`@dataclass(frozen=True)`)
  * Contenedor: `DatabaseHealthReport` (`@dataclass(frozen=True)`)
* **En MCP ([`mcp_pg_auditor.py:61-127`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/mcp_pg_auditor.py#L61-L127)):**
  * `InvalidIndexIssue` (`pydantic.BaseModel`)
  * `UnindexedFKIssue` (`pydantic.BaseModel`)
  * `DeadTuplesIssue` (`pydantic.BaseModel`)
  * `HotUpdateIssue` (`pydantic.BaseModel`)
  * `RedundantIndexIssue` (`pydantic.BaseModel`)
  * `LowUsageIndexIssue` (`pydantic.BaseModel`)
  * Contenedor: `PostgresHealthReport` (`pydantic.BaseModel`)
* **Equivalencia de Atributos:**
  * Los 6 DTOs individuales son **estrictamente idénticos** en nombres de campos, tipos primitivos (`str`, `int`, `float`, `datetime | None`, `bool`) y orden semántico.
  * Los contenedores difieren en detalles de transporte:
    * En CLI: `hot_issues: list[HotUpdateIssue]` vs en MCP: `hot_fillfactor_issues: list[HotUpdateIssue]`.
    * En MCP se incluyen metadatos de transporte de protocolo: `database_alias: str`, `schemas_audited: list[str]`, `execution_time_ms: float = 0.0`.
    * `has_critical_issues`: en CLI es una `@property` calculada; en MCP es un campo booleano serializable.

##### 2. Responsabilidad Arquitectónica y Frontera de Capas
* Ambos conjuntos de DTOs pertenecen estrictamente a la capa de **Adaptadores (Adapters/Entrypoints)**:
  * CLI (`audit_pg.py`) es un adaptador síncrono sobre `psycopg2`.
  * MCP (`mcp_pg_auditor.py`) es un adaptador asíncrono sobre `asyncpg` y el framework `FastMCP`.
* **Ninguno de estos DTOs pertenece al dominio ni al contrato canónico:**
  * El contrato canónico reside en [`src/sql_audit/domain/models.py`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/domain/models.py): `AuditReport`, `Finding`, `Evidence`, `Severity`.
  * Los DTOs de `Issue` son estructuras intermedias legadas de la fase inicial del proyecto que reciben directamente las filas leídas del cursor de base de datos (`dict(r)`).
  * Ninguno contiene reglas de negocio, lógica de dominio ni algoritmos: son meros contenedores de transporte.

##### 3. Relación con T-02 (Contrato Semántico Unificado)
* T-02 exige un contrato semántico unificado: ambos adaptadores deben auditar con idénticos umbrales, criterios de severidad y defaults.
* **Misma semántica $\ne$ Misma representación DTO:**
  * La semántica unificada se expresa a través de la conversión al contrato canónico común vía `to_audit_report()` $\to$ `convert_legacy_report_to_audit_report` ([`mapping.py:319`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/mapping.py#L319)).
  * La función `_extract_row(item)` en `mapping.py:311` maneja polimórficamente tanto `@dataclass` (`dict(item.__dict__)`) como `BaseModel` (`dict(item.model_dump())`), abstrayendo por completo la diferencia tecnológica de los DTOs.
  * No existe divergencia semántica alguna provocada por la existencia de ambos DTOs.

##### 4. Deriva Actual y Riesgo Fáctico
* **Deriva en Issues:** 0% de divergencia en campos, tipos o valores permitidos entre los 6 issues.
* **Deriva en Contenedores:** La diferencia de nombre (`hot_issues` vs `hot_fillfactor_issues`) ya está resuelta explícitamente en el adaptador de aplicación ([`mapping.py:351`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/mapping.py#L351)):
  `hot_list = getattr(report, "hot_issues", None) or getattr(report, "hot_fillfactor_issues", [])`.
* No existen registros de bugs ni divergencias semánticas provocadas por esta separación.

##### 5. Dirección de Dependencias y Razón Tecnológica
* **CLI (`audit_pg.py`):** Diseñado con metadatos PEP 723 (`# /// script`) para ejecución ligera con mínimas dependencias (`psycopg2-binary`, `python-dotenv`). Usar `@dataclass` estándar evita forzar la instalación y carga en memoria de `pydantic` en un script CLI.
* **MCP (`mcp_pg_auditor.py`):** Integrado con `FastMCP`. El protocolo MCP requiere inspeccionar modelos `pydantic.BaseModel` para generar los JSON Schemas de las herramientas en tiempo de inicialización (`tools/list`).
* Una "unificación" forzada forzaría a la CLI a depender de `pydantic`, o forzaría a MCP a prescindir de la generación automática de esquemas de FastMCP.

##### 6. Coste de Refactor vs. Beneficio Real
* **Coste:** Alto. Modificaría archivos críticos en funcionamiento (`audit_pg.py`, `mcp_pg_auditor.py`, `mapping.py`), requiriendo reescribir tests en `tests/test_mcp_auditor.py` y `tests/test_canonical_contract.py`.
* **Beneficio:** Trivial o nulo. Ahorraría unas 60 líneas de definiciones de DTOs en dos adaptadores ya desacoplados, introduciendo acoplamiento cruzado artificial.

##### 7. Cobertura de Tests Existente
* [`tests/test_canonical_contract.py:52-173`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/tests/test_canonical_contract.py#L52-L173) (`test_cli_and_mcp_produce_equivalent_audit_reports`):
  * Construye un `DatabaseHealthReport` (CLI) y un `PostgresHealthReport` (MCP) con datos equivalentes.
  * Convierte ambos a `AuditReport`.
  * Valida formalmente:
    * `assert len(cli_canonical.findings) == len(mcp_canonical.findings) == 6`
    * `assert set(cli_findings.keys()) == set(mcp_findings.keys())`
    * `assert cf.severity == mf.severity`
    * `assert cf.object_name == mf.object_name`
    * `assert cf.evidence[0].evidence_id == mf.evidence[0].evidence_id`
  * Demuestra de manera concluyente que ambos adaptadores producen salidas de dominio indistinguibles.

---

#### 2.12.3 Clasificación de la Evidencia Fáctica

* **Clasificación:** `LEGITIMATE_ADAPTER_DUPLICATION` (Deuda técnica de bajo valor / `LOW_VALUE_TECHNICAL_DEBT`).
* **Justificación:**
  1. `REAL_CONTRACT_RISK` descartado categóricamente: La prueba de regresión formal demuestra equivalencia canónica total.
  2. La duplicación responde a requerimientos idiomáticos legítimos de cada entorno de ejecución (CLI sin dependencias pesadas vs. servidor FastMCP con reflexión de esquemas Pydantic).
  3. No contamina el dominio canónico (`AuditReport`, `Finding`, `Evidence`).

---

#### 2.12.4 Relación con Decisiones Anteriores
* **T-02 (`UNIFIED_SEMANTIC_CONTRACT`):** Compatible. La unificación semántica aplica a las reglas y al contrato canónico, no a la prohibición de adaptadores idiomáticos.
* **T-03 (`AUDITORIA_PARCIAL`):** Compatible. La señalización de fallos opera sobre `AuditReport`.
* **T-04 y T-07:** Compatible. La comparación multi-evidencia opera sobre `Finding.evidence`, ajena a los DTOs de adaptadores.
* **T-10 (`EVIDENCE_ALIGNED_DOCUMENTATION`):** Compatible. Documenta honestamente las interfaces de cada adaptador.

---

#### 2.12.5 Estado de la Decisión
* **Estado:** `DECIDED`
* **Decisión del propietario:** `LEGITIMATE_ADAPTER_DUPLICATION` (Opción A).
* **Definición formal:**
  1. Los seis DTOs de issues duplicados (`HotUpdateIssue`, `RedundantIndexIssue`, `LowUsageIndexIssue`, `InvalidIndexIssue`, `UnindexedFKIssue`, `DeadTuplesIssue`) pertenecen estrictamente a la capa de adaptadores/transporte y no al modelo de dominio.
  2. La duplicación actual es estructuralmente equivalente y no presenta divergencia semántica observable ni riesgos para la integridad de los datos.
  3. CLI y MCP pueden y deben utilizar representaciones de datos diferentes cuando esas representaciones responden a requisitos propios y legítimos de su runtime de transporte (standard library `@dataclass(frozen=True)` para la CLI sync ligera sin dependencias externas vs. `pydantic.BaseModel` para el servidor MCP async con reflexión de JSON Schemas).
  4. La utilización de `dataclass` en CLI y `BaseModel` en MCP no constituye por sí misma una violación de T-02.
  5. La equivalencia relevante se establece y gobierna en el **contrato canónico `AuditReport`** ([`src/sql_audit/domain/models.py`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/domain/models.py)), no mediante identidad de clases DTO intermedias.
  6. [`tests/test_canonical_contract.py`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/tests/test_canonical_contract.py) constituye evidencia empírica existente de que ambos adaptadores convergen sobre el mismo contrato canónico con exactitud bit a bit en identificadores, severidades y evidencias.
  7. La diferencia de nombres en los contenedores (`DatabaseHealthReport.hot_issues` vs `PostgresHealthReport.hot_fillfactor_issues`) queda absorbida formalmente por la función de mapping ([`mapping.py:351`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/mapping.py#L351)) y no representa una divergencia semántica.
  8. **No** se extraen DTOs Pydantic compartidos únicamente para eliminar aproximadamente 60 líneas de código duplicado, ya que ello introduciría acoplamiento innecesario y arrastraría dependencias pesadas de transporte a capas que no las necesitan.
  9. **No** se reemplazan los DTOs por la construcción directa del contrato canónico en esta tarea: constituiría un refactor mayor de elevado coste y riesgo que alteraría APIs públicas de herramientas MCP sin justificación demostrada.
  10. La deuda queda formalmente clasificada como **`LOW_VALUE_TECHNICAL_DEBT`** y no requiere corrección para preservar el contrato del sistema.
  11. Si en el futuro surgiera divergencia real entre los DTOs o fallos en los tests de equivalencia canónica, T-12 podrá reabrirse aportando nueva evidencia fáctica.
  12. **Relación con T-02 (`UNIFIED_SEMANTIC_CONTRACT`):**
      > **T-02 unifica semántica; T-12 permite representaciones específicas de adaptador mientras esa semántica permanezca alineada.**

---

## 3. Bugs Confirmados

Problemas matemáticos o algorítmicos demostrados de forma incontestable por el código y reproducibles mediante tests:

### 3.1 Terminación del Recorrido en Grafo de Locks
* **Ubicación:** [`src/sql_audit/application/lock_service.py:17-54`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/lock_service.py#L17-L54) (`_traverse_blocking_tree`).
* **Comportamiento observado:** Función recursiva que itera `children = blocking_to_blocked.get(parent_pid, [])` sin mantener un conjunto de nodos visitados (`visited`).
* **Consecuencia algorítmica:** Si el diccionario contiene una referencia cíclica ($PID_A \to PID_B \to PID_A$), el código entra en un bucle recursivo no acotado y colapsa con `RecursionError: maximum recursion depth exceeded`.
* **Frontera fáctica:** El defecto algorítmico en Python está confirmado. Si PostgreSQL puede entregar este ciclo en `pg_blocking_pids()` antes de que actúe su detector de deadlocks es un comportamiento externo no demostrado por la suite. La corrección se justifica por principio de terminación defensiva ($O(V + E)$).
* **Cambio mínimo esperado:** Agregar un conjunto `visited_pids` en el recorrido para detectar aristas de retroceso, cortar la rama y garantizar la terminación sin alterar la salida de grafos acíclicos.
* **Test necesario:** Test unitario en `tests/test_lock_engine.py` pasando filas con ciclo cerrado ($100 \leftrightarrow 200$) y verificando terminación limpia.
* **Estado:** `RESOLVED` (Implementado con `active_path` en `lock_service.py` y verificado en `test_lock_engine.py`).

### 3.2 Comparación Incompleta en DiffEngine
* **Ubicación:** [`src/sql_audit/application/diff_service.py:30-31`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/diff_service.py#L30-L31) (`compare_audit_reports`).
* **Comportamiento observado:**
  ```python
  prev_eid = prev_f.evidence[0].evidence_id if prev_f.evidence else ""
  curr_eid = curr_f.evidence[0].evidence_id if curr_f.evidence else ""
  if prev_eid == curr_eid:
      unchanged_findings.append(curr_f)
  ```
* **Consecuencia en contrato:**
  1. Si un finding tiene múltiples evidencias y cambia `evidence[1]`, el motor reporta `UNCHANGED` (falso negativo).
  2. Si las mismas evidencias se entregan en orden invertido (`[A, B]` vs `[B, A]`), reporta `CHANGED` (falso positivo si la colección no es ordenada).
* **Cambio mínimo esperado:** Evaluar la totalidad de las evidencias del finding según la semántica que se defina en la Decisión 2.4, eliminando la suposición de que solo existe `evidence[0]`.
* **Test necesario:** Tests en `tests/test_diff_engine.py` con findings multi-evidencia que validen la comparación de todas las evidencias de acuerdo con la semántica adoptada.
* **Estado:** `RESOLVED` (Implementado con `sorted(evidence_ids)` preservando cardinalidad en `diff_service.py` y `diff.py`, verificado en `tests/test_diff_engine.py`).

---

## 4. Riesgos Condicionados

Modificaciones cuya aplicación depende de las definiciones de la Fase 0:

### 4.1 Límites Defensivos en `EXPLAIN ANALYZE` (Condicional a Decisión 2.1 = B)
* **Ubicación:** [`audit_pg.py:311,319`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L311) y [`mcp_pg_auditor.py:254,258`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/mcp_pg_auditor.py#L254).
* **Riesgo:** Consultas analizadas de alto costo o bloqueadas por transacciones productivas cuelgan la herramienta.
* **Medidas defensivas requeridas:**
  1. `statement_timeout`: Límite máximo de tiempo de ejecución de la sentencia en la base.
  2. `lock_timeout`: Límite máximo de tiempo esperando cerrojos de tabla/fila.
  3. Validación de comando único en CLI: Impedir la ejecución de cadenas con múltiples sentencias separadas por punto y coma antes de invocar `psycopg2`.
* **Estado:** `CONDITIONAL` (a Decisión 2.1).

### 4.2 Sugerencias Operacionales en CLI (UX y Disponibilidad)
* **Ubicación:** [`audit_pg.py:528`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L528) y [`audit_pg.py:504`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L504).
* **Evaluación fáctica:**
  * El auditor no ejecuta DDL; las sugerencias son meras cadenas de texto impresas en consola.
  * Su impacto radica en el riesgo de que un usuario o agente automatizado copie y ejecute el comando en producción sin advertir los bloqueos asociados.
* **Acción esperada:** Ajustar las sugerencias textuales conforme a la política que resuelva la Decisión 2.6.
* **Estado:** `CONDITIONAL` (a Decisión 2.6).

---

## 5. Documentación y UX

Alineación de afirmaciones textuales con el comportamiento real del código:

| Claim Actual en Repo | Evidencia en Código | Clasificación | Ajuste Documental Requerido |
|---|---|---|---|
| *"Provides exact bloat percentages and bytes wasted"* ([`README.md:39`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/README.md#L39)) | [`queries.py:404,656`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/infrastructure/queries.py#L404) calcula estimaciones usando `pg_stats.avg_width` (muestreo probabilístico) y fillfactor heurístico 0.70. | `DOCUMENTACION_INCORRECTA` | Declarar que el cálculo es una estimación estadística de densidad de páginas esperadas vs. reales basada en catálogos y `pg_stats`. |
| *"sub-second O(1) catalog queries"* ([`README.md:12`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/README.md#L12), [`SKILL.md:12`](file:///C:/Users/cerra/.gemini/config/skills/sql-audit/SKILL.md#L12)) | [`queries.py:218,606`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/infrastructure/queries.py#L218) cruza arrays de claves y estadísticas proporcionales al volumen de tablas e índices ($O(N)$). | `DOCUMENTACION_INCORRECTA` | Describir el desempeño en términos de consultas optimizadas de catálogo sobre metadatos, evitando la afirmación teórica de $O(1)$. |
| *"Automatically suggests and prepares safe, production-grade DDL migrations"* ([`README.md:21`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/README.md#L21)) | El motor no genera scripts DDL dinámicos; [`scripts/migrations/`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/scripts/migrations/) contiene scripts manuales estáticos para una base específica (`pedidos`). | `DOCUMENTACION_INCORRECTA` | Aclarar que la herramienta emite recomendaciones textuales y provee scripts de referencia. |
| *"Zero-Downtime Remediation"* ([`README.md:21`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/README.md#L21)) | En [`audit_pg.py:528`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L528) se sugiere `VACUUM FULL` (bloqueo exclusivo). | `DOCUMENTACION_INCORRECTA` | Alinear la documentación con la política de recomendaciones que se defina en 2.6. |

---

## 6. Deuda Técnica (No Bloqueante)

Problemas reales de mantenimiento y tipado que **no bloquean la corrección funcional** y se mantienen en el backlog:

1. **Parámetro huérfano en `compute_stable_finding_id`:**
   * [`src/sql_audit/domain/hasher.py:17`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/domain/hasher.py#L17) recibe `object_type` pero no lo usa en el formato.
   * *Impacto:* No colisiona en los 6 checks actuales, pero es código engañoso. Prioridad: Backlog.
2. **Duplicación de modelos `Issue`:**
   * Dataclasses en [`audit_pg.py:79-137`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L79-L137) vs Pydantic BaseModels en [`mcp_pg_auditor.py:61-113`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/mcp_pg_auditor.py#L61-L113).
   * *Impacto:* Mantenimiento duplicado. No rompe ejecución. Prioridad: Backlog.
3. **Uso de `Any` en cursores y diccionarios internos:**
   * [`audit_pg.py:347`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L347) (`cur: Any`), [`mapping.py:19`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/mapping.py#L19) (`row: dict[str, Any]`), [`bloat_service.py:24`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/bloat_service.py#L24).
   * *Impacto:* Pérdida de chequeo estricto de mypy en capas intermedias. Prioridad: Backlog.
4. **Usos legítimos de `Any` que deben mantenerse:**
   * [`plan_service.py:15`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/plan_service.py#L15) (`raw_plan_data: list[dict[str, Any]]`): Manejo del árbol JSON arbitrario de PostgreSQL.
   * [`audit_pg.py:354`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L354) (`params: list[Any]`): Paso de parámetros heterogéneos a DB-API.

---

## 7. Evidencia Faltante

Puntos que dependen de factores externos y requieren validación empírica antes de considerarse hechos demostrados:

1. **Comportamiento real de `psycopg2` ante multi-sentencia con `EXPLAIN`:**
   * *Estado:* Inferido de conocimiento externo. Falta un test de integración contra PostgreSQL real para demostrar si `cur.execute("EXPLAIN SELECT 1; COMMIT;")` ejecuta el commit o si la gramática de `EXPLAIN` falla.
2. **Escenarios de ciclos en `pg_blocking_pids()`:**
   * *Estado:* Demostrado el fallo algorítmico en Python; no demostrado si PostgreSQL puede entregar un ciclo cerrado en condiciones reales antes del deadlock timeout.
3. **Inexistencia de tests de integración con PostgreSQL real en la suite:**
   * *Estado:* Todos los tests actuales de plan, locks y bloat usan `MagicMock` y `AsyncMock`. Ningún test levanta un contenedor o servicio PostgreSQL real para validar las consultas SQL de infraestructura.

---

## 8. Plan de Ejecución por Fases

```text
FASE 0 — Decisiones de Producto y Contrato (Propietario) [COMPLETED]
  ├── 0.1 Decisión sobre EXPLAIN ANALYZE (Resuelta: Opción A — Retirar EXPLAIN ANALYZE).
  ├── 0.2 Decisión sobre contrato CLI/MCP (Resuelta: Opción A — Contrato Semántico Unificado).
  ├── 0.3 Decisión sobre política de errores (Resuelta: Opción B — Auditoría parcial resiliente).
  ├── 0.4 Decisión sobre semántica de evidence (Resuelta: UNORDERED_EVIDENCE).
  ├── 0.5 Decisión sobre identidad de locks (Resuelta: LIVE_ON_DEMAND_LOCK_DIAGNOSTIC).
  ├── 0.6 Decisión sobre terminación de grafo de locks (Resuelta: CYCLE_SAFE_GRAPH_TRAVERSAL).
  ├── 0.7 Decisión sobre diff multi-evidencia (Resuelta: CANONICAL_EVIDENCE_MULTISET).
  ├── 0.8 Decisión sobre timeouts de sesión (Resuelta: DEFENSIVE_SESSION_TIMEOUTS).
  ├── 0.9 Decisión sobre política de recomendaciones (Resuelta: SUPPRESS_EXECUTABLE_REMEDIATION).
  ├── 0.10 Decisión sobre alineación documental (Resuelta: EVIDENCE_ALIGNED_DOCUMENTATION).
  ├── 0.11 Decisión sobre parámetro object_type (Resuelta: REMOVE_ORPHANED_OBJECT_TYPE).
  └── 0.12 Decisión sobre duplicación DTOs Issue (Resuelta: LEGITIMATE_ADAPTER_DUPLICATION).

FASE 1 — Corrección de Bugs Confirmados (Código & Tests) [COMPLETED]
  ├── 1.1 Implementar detección de visitados en _traverse_blocking_tree (lock_service.py) [COMPLETED].
  │     └── Test: tests/test_lock_engine.py con grafo cíclico.
  └── 1.2 Corregir compare_audit_reports para evaluar todas las evidencias según la semántica definida en 0.4 [COMPLETED].
        └── Test: tests/test_diff_engine.py con findings multi-evidencia.

FASE 2 — Mitigación de Riesgos Condicionados y Cambios de Contrato (Código & Config)
  ├── 2.1 Retirar soporte de EXPLAIN ANALYZE en CLI y MCP conforme a resolución de T-01 [COMPLETED].
  │     └── Tests: tests/test_plan_engine.py (rechazo de flag, signaturas pasivas, SQL estático sin rollback ni buffers).
  ├── 2.2 Aplicar umbrales canónicos unificados en CLI y MCP conforme a resolución de T-02 [COMPLETED].
  │     └── Tests: tests/test_canonical_contract.py (DEFAULT_MIN_SIZE_BYTES=0, DEFAULT_MIN_TABLE_ROWS=10000, equivalencia en queries simuladas y contratos canónicos).
  ├── 2.3 Implementar política de auditoría parcial resiliente en orquestación y modelos según T-03 [COMPLETED].
  │     └── Tests: tests/test_resilient_audit.py (aislamiento de checks, rollback ante error, preservación de evidencia, is_partial, checks_executed vs checks_failed, no equivalencia semántica en diff).
  ├── 2.4 Incorporar límites defensivos de sesión (statement_timeout, lock_timeout) según T-08 [COMPLETED].
  │     └── Tests: tests/test_session_timeouts.py (DEFAULT_STATEMENT_TIMEOUT_MS=15000, DEFAULT_LOCK_TIMEOUT_MS=3000, aplicación a nivel de sesión en options de psycopg2 y server_settings de asyncpg, integración de timeouts con auditoría parcial T-03, fallo global ante caída de conexión).
  ├── 2.5 Aplicar la política de recomendaciones operativas acordada en T-09 sobre render_text [COMPLETED].
  │     └── Tests: tests/test_suppress_remediation.py (supresión de DDL/DML y comandos de terminación en render_text, preservación de contexto diagnóstico y consideraciones operativas, sincronización de README y SKILL).
  └── 2.6 Limpiar parámetro huérfano object_type en hasher.py y sus call sites según T-11 [COMPLETED].
        └── Tests: tests/test_domain_models.py (signatura sin object_type, preservación estricta de IDs históricos con y sin sub_object, atributo Finding.object_type intacto).

FASE 3 — Documentación y Alineación de UX (Markdown) [COMPLETED]
  ├── 3.1 Actualizar README.md reflejando fielmente las decisiones de Fase 0 y la implementación real (T-10) [COMPLETED].
  └── 3.2 Actualizar SKILL.md en concordancia con las capacidades operacionales efectivas (T-10) [COMPLETED].
        └── Tests: tests/test_evidence_aligned_docs.py (purga de claims no verificables O(1), exact bloat, retiro de --analyze/rollback, defaults canónicos y timeouts defensivos).

FASE 4 — Deuda Técnica Remanente (Backlog / Mantenimiento)
  ├── 4.1 Evaluar tipado más estricto con TypedDict en filas de catálogo.
  └── 4.2 Monitorear divergencias en adaptadores CLI/MCP (T-12 cerrado como separación legítima).
```

---

## 9. Matriz de Control

| ID | Tema | Tipo | Evidencia | Decisión Requerida | Dependencias | Prioridad | Estado |
|---|---|---|---|---|---|---|---|
| **T-01** | `EXPLAIN ANALYZE` (Permanencia vs Retiro) | Dominio/Seguridad | [`audit_pg.py:320`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L320), [`mcp_pg_auditor.py:261`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/mcp_pg_auditor.py#L261) | Retirar `EXPLAIN ANALYZE` (Opción A) | Ninguna | P0 | `RESOLVED` |
| **T-02** | Unificación Umbrales CLI vs MCP | Contrato | [`audit_pg.py:784`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L784) vs [`mcp_pg_auditor.py:363`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/mcp_pg_auditor.py#L363) | Contrato Semántico Unificado (Opción A) | Ninguna | P0 | `RESOLVED` |
| **T-03** | Política ante Fallo de Checks | Arquitectura | [`audit_pg.py:217`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L217), [`models.py:92`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/domain/models.py#L92) | Auditoría Parcial Resiliente (Opción B) | Ninguna | P0 | `RESOLVED` |
| **T-04** | Semántica de `evidence` en Diff | Dominio | [`diff_service.py:30`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/diff_service.py#L30), [`models.py:63`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/domain/models.py#L63) | Colección no ordenada (`UNORDERED_EVIDENCE`) | Ninguna | P0 | `DECIDED` |
| **T-05** | Identidad de Locks (PID vs Recurso) | Dominio/SQL | [`lock_service.py:213`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/lock_service.py#L213), [`queries.py:353`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/infrastructure/queries.py#L353) | Diagnóstico live on-demand (`LIVE_ON_DEMAND_LOCK_DIAGNOSTIC`) | Ninguna | P1 | `DECIDED` |
| **T-06** | Ciclos en Grafo de Locks | Algoritmo/Robustez | [`lock_service.py:17`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/lock_service.py#L17) | Terminación con `active_path` (`CYCLE_SAFE_GRAPH_TRAVERSAL`) | Ninguna | P1 | `RESOLVED` |
| **T-07** | Comparación Multi-Evidencia en Diff | Dominio/Algoritmo | [`diff_service.py:30`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/application/diff_service.py#L30), [`diff.py:24`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/domain/diff.py#L24) | Comparación por multiset (`CANONICAL_EVIDENCE_MULTISET`) | T-04 | P1 | `RESOLVED` |
| **T-08** | Timeouts en EXPLAIN / Consultas | Robustez/Sesión | [`audit_pg.py:311`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L311), [`mcp_pg_auditor.py:254`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/mcp_pg_auditor.py#L254) | Timeouts defensivos de sesión (`DEFENSIVE_SESSION_TIMEOUTS`) | T-01, T-02, T-03 | P1 | `RESOLVED` |
| **T-09** | Política de Recomendaciones Operativas | UX / Política | [`audit_pg.py:504,528`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L504) | Supresión de SQL ejecutable (`SUPPRESS_EXECUTABLE_REMEDIATION`) | Ninguna | P2 | `RESOLVED` |
| **T-10** | Alineación Documental README / SKILL | Docs | [`README.md:12,39`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/README.md#L12) | Sincronización fáctica rigurosa | T-01, T-02, T-09 | P2 | `RESOLVED` |
| **T-11** | Parámetro `object_type` en Hasher | Dominio/Deuda Técnica | [`hasher.py:17`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/src/sql_audit/domain/hasher.py#L17) | Remover parámetro huérfano (`REMOVE_ORPHANED_OBJECT_TYPE`) | T-10 | P4 | `RESOLVED` |
| **T-12** | Duplicación DTOs Issue CLI vs MCP | Adaptadores/Deuda Técnica | [`audit_pg.py:79`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/audit_pg.py#L79), [`mcp_pg_auditor.py:61`](file:///C:/Users/cerra/codigo/Herraminetas_Sql/mcp_pg_auditor.py#L61) | Mantener DTOs separados (`LEGITIMATE_ADAPTER_DUPLICATION`) | T-02 | P4 | `DECIDED` |

---

## 10. Definition of Done (Criterios de Cierre)

La implementación de este plan se considerará completada y verificada cuando se cumplan los siguientes criterios:

1. **Resolución formal de decisiones:** Cada una de las decisiones de la Fase 0 (`T-01` a `T-12`) cuenta con una resolución documentada explícitamente por el propietario.
2. **Bugs confirmados corregidos y demostrados por tests:**
   * `_traverse_blocking_tree` cuenta con un test unitario en `tests/test_lock_engine.py` que inyecta dependencias cíclicas y demuestra terminación limpia sin `RecursionError`.
   * `compare_audit_reports` cuenta con tests en `tests/test_diff_engine.py` que demuestran la evaluación de todas las evidencias de acuerdo con la semántica resuelta en `T-04` y `T-07`.
3. **Riesgos condicionados resueltos según decisiones tomadas:** Habiéndose resuelto en `T-01` la eliminación de `EXPLAIN ANALYZE`, se valida mediante tests que ni la CLI ni el MCP aceptan o procesan el flag `analyze`. Las sugerencias textuales en el CLI se han ajustado estrictamente a la política resuelta en `T-09`.
4. **Documentación verídica y consistente:** `README.md` y `SKILL.md` reflejan fielmente las capacidades efectivas del motor, los umbrales canónicos adoptados y la naturaleza estadística de las estimaciones de bloat, sin afirmaciones absolutas no demostrables (T-10).
5. **Deuda técnica encapsulada:** Los elementos de deuda técnica (`T-11`, `T-12`) no bloquean la entrega de las correcciones funcionales ni introducen cambios de signatura pública no planificados.
6. **Integridad de la suite de tests:** Todos los tests existentes (91/91) continúan pasando en verde junto con los nuevos tests de regresión agregados para los bugs confirmados y las nuevas garantías contractuales.
