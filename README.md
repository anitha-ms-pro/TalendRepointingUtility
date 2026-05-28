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
6. **Updates context variables** — AWS context vars → GCP equivalents
7. **Renames labels** — `S3_Connection` → `GCS_Connection`, `RS_Connection` → `BQ_Connection`
8. **Generates log report** — Detailed log with all changes and manual review flags

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

## Component Replacement Map

| AWS Component | GCP Replacement |
|---|---|
| tS3Connection | tGSConnection |
| tS3Configuration | tGSConfiguration |
| tS3Put | tGSPut |
| tS3Get | tGSGet |
| tS3Copy | tGSCopy |
| tS3List | tGSList |
| tS3Delete | tGSDelete |
| tS3Close | tGSClose |
| tRedshiftConnection | tBigQueryConnection |
| tRedshiftInput | tBigQueryInput |
| tRedshiftOutput | tBigQueryOutput |
| tRedshiftRow | tBigQuerySQLRow |
| tRedshiftUnload | tBigQueryInput |
| tRedshiftClose | *REMOVED* |
| tSnowflakeConnection | tBigQueryConnection |
| tSnowflakeInput | tBigQueryInput |
| tSnowflakeOutput | tBigQueryOutput |
| tSnowflakeRow | tBigQuerySQLRow |
| tSnowflakeClose | *REMOVED* |
| tMSSqlConnection | tBigQueryConnection |
| tMSSqlInput | tBigQueryInput |
| tMSSqlOutput | tBigQueryOutput |
| tMSSqlRow | tBigQuerySQLRow |
| tMSSqlClose | *REMOVED* |
| tOracleConnection | tBigQueryConnection |
| tOracleInput | tBigQueryInput |
| tOracleOutput | tBigQueryOutput |
| tOracleRow | tBigQuerySQLRow |
| tOracleClose | *REMOVED* |

## SQL Conversion Examples

| Redshift | BigQuery |
|---|---|
| `GETDATE()` | `CURRENT_TIMESTAMP()` |
| `dateadd(day, -7, CURRENT_DATE)` | `DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)` |
| `trunc(column)` | `DATE(column)` |
| `NVL(a, b)` | `IFNULL(a, b)` |
| `ISNULL(a, b)` | `IFNULL(a, b)` |
| `LEN(str)` | `LENGTH(str)` |
| `s3://bucket` | `gs://bucket` |
| `CREATE TEMP TABLE t (LIKE schema.tbl)` | `CREATE TEMP TABLE t AS SELECT * FROM schema.tbl WHERE 1=0` |

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

## Customization

Edit `config.py` to:
- Add new component mappings
- Add new context variable replacements
- Customize label transformations
- Add new GCP context parameters

Edit `sql_converter.py` to:
- Add new SQL function conversions
- Add new manual review patterns
