# Talend AWS → GCP Repointing Utility

A Python CLI utility that processes Talend job folders and repoints **child jobs** from AWS (Redshift/S3) to GCP (BigQuery/GCS).

## Requirements

- Python 3.7+ (no external packages needed)

## What It Does

1. **Identifies child jobs** — Skips GrandMaster, Master, and ABAC jobs; only processes child-level `.item` files
2. **Backs up originals** — Renames original folder with `_backup` suffix
3. **Creates repointed copy** — New folder (original name) with GCP-repointed child jobs
4. **Replaces components** — `tRedshiftConnection` → `tBigQueryConnection`, `tS3Connection` → `tGSConnection`, etc.
5. **Converts SQL** — Redshift functions (GETDATE, dateadd, trunc, etc.) → BigQuery equivalents
6. **Updates context variables** — AWS context vars → GCP equivalents (supports Excel-based mappings)
7. **Processes context files** — Updates `.item` context files when using Excel mappings
8. **Renames labels** — `S3_Connection` → `GCS_Connection`, `RS_Connection` → `BQ_Connection`
9. **Generates log report** — Detailed log with all changes and manual review flags

## Usage

### Dry Run (Preview Changes — Recommended First)
```bash
python talend_repoint.py "D:\Talend-Studio-20231027_1100-V8.0.1\Talend-Studio-20231027_1100-V8.0.1\workspace\TALEND-KPI_CUSTOMER360-1765774989\KPI_CUSTOMER360\process\Jobs" --dry-run
```

### Live Run (Apply Changes)
```bash
python talend_repoint.py "D:\Talend-Studio-20231027_1100-V8.0.1\Talend-Studio-20231027_1100-V8.0.1\workspace\TALEND-KPI_CUSTOMER360-1765774989\KPI_CUSTOMER360\process\Jobs"
```

### Process Specific Folders Only
```bash
python talend_repoint.py "D:\...\process\Jobs" --specific-folders CUST360_KIOSK CUST360_CCP_LOADS
```

### Use Excel File for Context Variable Mappings
```bash
python talend_repoint.py "D:\...\process\Jobs" --excel-context-file "D:\context-sheets\context_c360.xlsx"
```

### Combined: Specific Folders + Excel Mappings
```bash
python talend_repoint.py "D:\...\process\Jobs" --specific-folders CUST360_KIOSK --excel-context-file "D:\context-sheets\context_c360.xlsx"
```

## Component Replacement Map

| AWS/Database Component | GCP Replacement |
|---|---|
| **S3 → GCS** | |
| tS3Connection | tGSConnection |
| tS3Configuration | tGSConfiguration |
| tS3Put | tGSPut |
| tS3Get | tGSGet |
| tS3Copy | tGSCopy |
| tS3List | tGSList |
| tS3Delete | tGSDelete |
| tS3Close | tGSClose |
| **Redshift → BigQuery** | |
| tRedshiftConnection | tBigQueryConnection |
| tRedshiftInput | tBigQueryInput |
| tRedshiftOutput | tBigQueryOutput |
| tRedshiftRow | tBigQuerySQLRow |
| tRedshiftUnload | tBigQueryInput |
| tRedshiftClose | *REMOVED* |
| **Snowflake → BigQuery (Standard)** | |
| tSnowflakeConnection | tBigQueryConnection |
| tSnowflakeInput | tBigQueryInput |
| tSnowflakeOutput | tBigQueryOutput |
| tSnowflakeRow | tBigQuerySQLRow |
| tSnowflakeClose | *REMOVED* |
| **Snowflake → BigQuery (Custom/Joblet)** | |
| SnowflakeConnection | *REMOVED* |
| SnowflakeInput | tBigQueryInput |
| SnowflakeOutput | tBigQueryOutput |
| SnowflakeRow | tBigQuerySQLRow |
| SnowflakeClose | *REMOVED* |
| **MSSQL → BigQuery** | |
| tMSSqlConnection | tBigQueryConnection |
| tMSSqlInput | tBigQueryInput |
| tMSSqlOutput | tBigQueryOutput |
| tMSSqlRow | tBigQuerySQLRow |
| tMSSqlClose | *REMOVED* |
| **Oracle → BigQuery** | |
| tOracleConnection | tBigQueryConnection |
| tOracleInput | tBigQueryInput |
| tOracleOutput | tBigQueryOutput |
| tOracleRow | tBigQuerySQLRow |
| tOracleClose | *REMOVED* |

## SQL Conversion

The utility uses **GCP BigQuery Translation API** to convert SQL queries from various dialects (Redshift, Snowflake, SQL Server, Oracle, Teradata) to BigQuery syntax. If API translation fails, it falls back to local regex-based conversion rules.

### Supported SQL Dialects
- Redshift (Generally Available)
- Snowflake (Generally Available)
- SQL Server (Preview)
- Oracle (Preview)
- Teradata (Preview)

### SQL Conversion Examples

| Source SQL | BigQuery |
|---|---|
| **Redshift Functions** | |
| `GETDATE()` | `CURRENT_TIMESTAMP()` |
| `dateadd(day, -7, CURRENT_DATE)` | `DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)` |
| `trunc(column)` | `DATE(column)` |
| `NVL(a, b)` | `IFNULL(a, b)` |
| `ISNULL(a, b)` | `IFNULL(a, b)` |
| `LEN(str)` | `LENGTH(str)` |
| **Snowflake Functions** | |
| `DATE_PART('HOUR', timestamp_col)` | `EXTRACT(HOUR FROM timestamp_col)` |
| `EXTRACT(DOW FROM date_col)` | `EXTRACT(DAYOFWEEK FROM date_col)` |
| `to_char(date_col, 'YYYYMMDD')` | `FORMAT_DATE('%Y%m%d', date_col)` |
| `DATEADD(day, -7, CURRENT_DATE)` | `DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)` |
| **Common Conversions** | |
| `s3://bucket` | `gs://bucket` |
| `CREATE TEMP TABLE t (LIKE schema.tbl)` | `CREATE TEMP TABLE t AS SELECT * FROM schema.tbl WHERE 1=0` |

### Known Limitations
- **Snowflake COPY INTO** commands cannot be auto-converted (requires manual job redesign)
- Complex proprietary SQL may require manual review after migration

## Folder Structure After Repointing

```
Jobs/
├── CUST360_KIOSK/                    ← New (repointed) folder
│   ├── C360_KIOSK_TRN_HDR_LOAD_0.1.item       ← REPOINTED (child job)
│   ├── C360_KIOSK_TRN_HDR_GrandMaster_0.1.item ← Unchanged
│   ├── C360_KIOSK_TRN_HDR_Master_0.1.item       ← Unchanged
│   └── C360_KIOSK_TRN_HDR_ABAC_0.1.item         ← Unchanged
│
├── CUST360_KIOSK_backup/             ← Backup of original
│   ├── C360_KIOSK_TRN_HDR_LOAD_0.1.item       ← Original (untouched)
│   └── ...
```

## Logs

Detailed logs are saved in the `logs/` directory with timestamps:
- `logs/repoint_20260523_150000.log`

## Excel-Based Context Mappings

Instead of hardcoding context variable mappings in `config.py`, you can use an Excel file with the following columns:

| Column | Description |
|--------|-------------|
| `Context` | Original context name (e.g., `Redshift_CustDB_MKT`) |
| `New_Context` | New context name (e.g., `BQ_CustDB_MKT`) |
| `Variable_name` | Original variable name (e.g., `Redshift_CustDB_MKT_Dataset`) |
| `New_Variable_Name` | New variable name (e.g., `BQ_CustDB_MKT_Dataset`) |
| `No_Change_Context` | `TRUE` if context name stays the same |
| `No_Change_Var_Name` | `TRUE` if variable name stays the same |

**Example:**
```
Context              | New_Context       | Variable_name                    | New_Variable_Name              | No_Change_Context | No_Change_Var_Name
---------------------|-------------------|----------------------------------|--------------------------------|-------------------|-------------------
Redshift_CustDB_MKT  | BQ_CustDB_MKT     | Redshift_CustDB_MKT_Dataset      | BQ_CustDB_MKT_Dataset          | FALSE             | FALSE
Redshift_CustDB_MKT  | BQ_CustDB_MKT     | Redshift_CustDB_MKT_Project      | BQ_CustDB_MKT_Project          | FALSE             | FALSE
```

The utility will:
- Replace context names in job files
- Replace variable names in context files (`.item` files in the `context/` directory)
- Add BigQuery project prefixes to dataset references automatically

## Customization

Edit `config.py` to:
- Add new component mappings
- Add new context variable replacements
- Customize label transformations
- Add new GCP context parameters

Edit `sql_converter.py` to:
- Add new SQL function conversions
- Add new manual review patterns
