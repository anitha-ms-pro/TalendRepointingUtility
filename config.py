"""
Configuration for Talend AWS → GCP Repointing Utility
All mapping tables for components, context variables, SQL functions, and labels.
"""

import pandas as pd
import os

# =============================================================================
# EXCEL-BASED CONTEXT VARIABLE MAPPINGS
# =============================================================================
EXCEL_CONTEXT_FILE = r"D:\context-sheets\context_c360.xlsx"  # Default path
USE_EXCEL_MAPPINGS = True  # Set to False to use hardcoded mappings only

def load_excel_context_mappings(excel_file_path=None):
    """
    Load context variable mappings from Excel file.

    Args:
        excel_file_path: Optional path to Excel file. If not provided, uses EXCEL_CONTEXT_FILE.

    Returns:
        Tuple of (context_renames, variable_renames, context_variable_map)
    """
    excel_path = excel_file_path if excel_file_path else EXCEL_CONTEXT_FILE

    if not USE_EXCEL_MAPPINGS or not os.path.exists(excel_path):
        return {}, {}, {}

    try:
        df = pd.read_excel(excel_path)
        print(f"[INFO] Loading context mappings from: {excel_path}")

        context_renames = {}
        variable_renames = {}
        context_variable_map = {}

        for _, row in df.iterrows():
            old_context = str(row['Context'])
            new_context = str(row['New_Context'])
            old_var = str(row['Variable_name'])
            new_var = str(row['New_Variable_Name'])
            no_change_ctx = row['No_Change_Context']
            no_change_var = row['No_Change_Var_Name']

            # Track context renames
            if not no_change_ctx and old_context != new_context:
                context_renames[old_context] = new_context

            # Track variable renames
            if not no_change_var and old_var != new_var:
                variable_renames[old_var] = new_var
                context_variable_map[f"context.{old_var}"] = f"context.{new_var}"

        print(f"[OK] Loaded {len(context_renames)} context renames from Excel")
        print(f"[OK] Loaded {len(variable_renames)} variable renames from Excel")
        print(f"[OK] Loaded {len(context_variable_map)} context.variable mappings from Excel")

        return context_renames, variable_renames, context_variable_map
    except Exception as e:
        print(f"[WARN] Error loading Excel: {e}")
        return {}, {}, {}

# Load mappings on import (will be reloaded in main() if --excel-context-file is provided)
CONTEXT_RENAMES, VARIABLE_RENAMES, EXCEL_CONTEXT_VARIABLE_MAP = load_excel_context_mappings()

def reload_excel_mappings(excel_file_path):
    """
    Reload Excel mappings from a different file path.
    Call this from main() after parsing command-line arguments.
    """
    global CONTEXT_RENAMES, VARIABLE_RENAMES, EXCEL_CONTEXT_VARIABLE_MAP, CONTEXT_VARIABLE_REPLACEMENTS, CONTEXT_PARAM_NAME_REPLACEMENTS

    CONTEXT_RENAMES, VARIABLE_RENAMES, EXCEL_CONTEXT_VARIABLE_MAP = load_excel_context_mappings(excel_file_path)

    # Update the replacement dictionaries
    if USE_EXCEL_MAPPINGS and EXCEL_CONTEXT_VARIABLE_MAP:
        CONTEXT_VARIABLE_REPLACEMENTS = EXCEL_CONTEXT_VARIABLE_MAP
    if USE_EXCEL_MAPPINGS and VARIABLE_RENAMES:
        CONTEXT_PARAM_NAME_REPLACEMENTS = VARIABLE_RENAMES

# =============================================================================
# COMPONENT NAME REPLACEMENTS
# Maps AWS Talend component names to their GCP equivalents.
# Value of None means the component should be REMOVED entirely.
# =============================================================================
COMPONENT_REPLACEMENTS = {
    # S3 → GCS (standard components)
    "tS3Connection":        "tGSConnection",
    "tS3Configuration":     "tGSConfiguration",
    "tS3Put":               "tGSPut",
    "tS3Get":               "tGSGet",
    "tS3Copy":              "tGSCopy",
    "tS3List":              "tGSList",
    "tS3Delete":            "tGSDelete",
    "tS3Close":             "tGSClose",

    # S3 → GCS (Joblet/custom components without 't' prefix)
    "S3Put":                "tGSPut",
    "S3Get":                "tGSGet",
    "S3Copy":               "tGSCopy",
    "S3List":               "tGSList",
    "S3Delete":             "tGSDelete",
    "S3Close":              "tGSClose",
    "S3Connection":         "tGSConnection",

    # Redshift → BigQuery
    "tRedshiftConnection":  None,  # REMOVE - BQ is stateless
    "tRedshiftInput":       "tBigQueryInput",
    "tRedshiftOutput":      "tBigQueryOutput",
    "tRedshiftRow":         "tBigQuerySQLRow",
    "tRedshiftUnload":      "tBigQueryInput",
    "tRedshiftClose":       None,  # REMOVE - BQ is stateless

    # Snowflake → BigQuery
    "tSnowflakeConnection": None,  # REMOVE
    "tSnowflakeInput":      "tBigQueryInput",
    "tSnowflakeOutput":     "tBigQueryOutput",
    "tSnowflakeRow":        "tBigQuerySQLRow",
    "tSnowflakeClose":      None,  # REMOVE

    # MSSQL → BigQuery
    "tMSSqlConnection":     None,  # REMOVE
    "tMSSqlInput":          "tBigQueryInput",
    "tMSSqlOutput":         "tBigQueryOutput",
    "tMSSqlRow":            "tBigQuerySQLRow",
    "tMSSqlOutputBulk":     "tBigQueryOutputBulk",
    "tMSSqlSCD":            "tBigQuerySQLRow",
    "tMSSqlClose":          None,  # REMOVE

    # Oracle → BigQuery (if present in child jobs)
    "tOracleConnection":    None,  # REMOVE
    "tOracleInput":         "tBigQueryInput",
    "tOracleOutput":        "tBigQueryOutput",
    "tOracleRow":           "tBigQuerySQLRow",
    "tOracleClose":         None,  # REMOVE

    # Generic DB connections / close / operations
    "tDBConnection":        None,  # REMOVE
    "tDBClose":             None,  # REMOVE
    "tDBRow":               "tBigQuerySQLRow",  # Generic DB row operation (used as alias sometimes)
    "tDBInput":             "tBigQueryInput",   # Generic DB input (used as alias sometimes)
}

# Components that should be completely removed (node + connections + subjob)
COMPONENTS_TO_REMOVE = {k for k, v in COMPONENT_REPLACEMENTS.items() if v is None}

# =============================================================================
# UNIQUE_NAME PREFIX REPLACEMENTS
# When a component type changes, its UNIQUE_NAME prefix also changes.
# e.g. tS3Connection_1 → tGSConnection_1
# =============================================================================
UNIQUE_NAME_PREFIX_REPLACEMENTS = {
    # Standard tS3* components
    "tS3Connection":        "tGSConnection",
    "tS3Configuration":     "tGSConfiguration",
    "tS3Put":               "tGSPut",
    "tS3Get":               "tGSGet",
    "tS3Copy":              "tGSCopy",
    "tS3List":              "tGSList",
    "tS3Delete":            "tGSDelete",
    "tS3Close":             "tGSClose",

    # Joblet/custom S3* components (without 't' prefix)
    "S3Put":                "tGSPut",
    "S3Get":                "tGSGet",
    "S3Copy":               "tGSCopy",
    "S3List":               "tGSList",
    "S3Delete":             "tGSDelete",
    "S3Close":              "tGSClose",
    "S3Connection":         "tGSConnection",
    "tRedshiftConnection":  "tBigQueryConnection",
    "tRedshiftInput":       "tBigQueryInput",
    "tRedshiftOutput":      "tBigQueryOutput",
    "tRedshiftRow":         "tBigQuerySQLRow",
    "tRedshiftUnload":      "tBigQueryInput",
    "tSnowflakeConnection": "tBigQueryConnection",
    "tSnowflakeInput":      "tBigQueryInput",
    "tSnowflakeOutput":     "tBigQueryOutput",
    "tSnowflakeRow":        "tBigQuerySQLRow",
    "tMSSqlConnection":     "tBigQueryConnection",
    "tMSSqlInput":          "tBigQueryInput",
    "tMSSqlOutput":         "tBigQueryOutput",
    "tMSSqlRow":            "tBigQuerySQLRow",
    "tMSSqlOutputBulk":     "tBigQueryOutputBulk",
    "tMSSqlSCD":            "tBigQuerySQLRow",
    "tOracleConnection":    "tBigQueryConnection",
    "tOracleInput":         "tBigQueryInput",
    "tOracleOutput":        "tBigQueryOutput",
    "tOracleRow":           "tBigQuerySQLRow",
    # tDBRow is used as UNIQUE_NAME alias for tRedshiftRow sometimes
    "tDBRow":               "tBigQuerySQLRow",
    "tDBInput":             "tBigQueryInput",
}

# =============================================================================
# CONTEXT VARIABLE REPLACEMENTS (COMMENTED OUT - NOW USING EXCEL)
# Replaces AWS context references in .item XML content.
# Both as context parameter names and as inline references.
# NOTE: This is now loaded from Excel file. Keeping for reference only.
# =============================================================================
"""
CONTEXT_VARIABLE_REPLACEMENTS = {
    # S3 path contexts → GCS
    "context.s3inputpath":                          "context.gcsinputpath",
    "context.s3lookuppath":                         "context.gcslookuppath",
    "context.s3rejectpath":                         "context.gcsrejectpath",
    "context.c360_s3_static_path":                  "context.c360_gcs_static_path",
    "context.c360_s3_lookup_path":                  "context.c360_gcs_lookup_path",

    # NOTE: Redshift/AWS/S3 schema/server/database variables are now handled by pattern-based conversion
    # Pattern: Redshift_* -> BQ_*, AWS_* -> GCP_*, S3_* -> GCS_* (CASE PRESERVED)
    # Example: Redshift_CustDB_MKT_Schema -> BQ_CustDB_MKT_Schema
    # Table names in SQL are lowercased separately during SQL conversion

    # S3 bucket names → GCS (lowercase versions of same names + _gcp suffix)
    "context.Globaldatalake_Bucket_name":            "context.globaldatalake_bucket_name_gcp",
    "context.Globaldatalake_Access_Key_Id":          "context.globaldatalake_bucket_name_gcp",
    "context.Globaldatalake_Secret_Access_Key":      "context.globaldatalake_bucket_name_gcp",
    "context.Globalraw_Bucket_Name":                 "context.globalraw_bucket_name_gcp",
    "context.Globalraw_Access_Key_Id":               "context.globalraw_bucket_name_gcp",
    "context.Globalraw_Secret_Access_Key":           "context.globalraw_bucket_name_gcp",
    "context.Globaloutbound_Bucket_Name":            "context.globaloutbound_bucket_name_gcp",
    "context.Globaloutbound_Access_Key_Id":          "context.globaloutbound_bucket_name_gcp",
    "context.Globaloutbound_Secret_Access_Key":      "context.globaloutbound_bucket_name_gcp",
    "context.GlobalDataWork_Bucket_name":            "context.globaldatawork_bucket_name_gcp",

    # S3 bucket paths → GCS
    "context.Globaldatalake_Path":                   "context.globaldatalake_path",
    "context.Globaldatalake_Stage_Path":             "context.globaldatalake_stage_path",
    "context.Globaldatalake_SubjectArea":            "context.globaldatalake_subjectarea",
    "context.Globaldatalake_Terr":                   "context.globaldatalake_terr",
    "context.Globaldatalake_Date":                   "context.globaldatalake_date",
    "context.Globalraw_Path":                        "context.globalraw_path",
    "context.Globalraw_Archive_Path":                "context.globalraw_archive_path",
    "context.Globalraw_Archive_Subject":             "context.globalraw_archive_subject",
    "context.Globalraw_SubjectArea":                 "context.globalraw_subjectarea",
    "context.Globalraw_Terr":                        "context.globalraw_terr",
    "context.Globalraw_Date":                        "context.globalraw_date",
    "context.Globaloutbound_Path":                   "context.globaloutbound_path",
    "context.Globaloutbound_SubjectArea":            "context.globaloutbound_subjectarea",
    "context.Globaloutbound_Terr":                   "context.globaloutbound_terr",
    "context.Globaloutbound_Date":                   "context.globaloutbound_date",

    # S3 bucket specific
    "context.S3_Bucket_GDAP_rejects":               "context.gcs_bucket_gdap_rejects_gcp",
    "context.S3_Bucket_GDAP_lookups":               "context.gcs_bucket_gdap_lookups_gcp",
    "context.S3_Bucket_GDAP_archive":               "context.gcs_bucket_gdap_archive_gcp",
    "context.GlobalLandingZone":                     "context.globallandingzone",

    # AWS specific
    "context.aws_region":                            "context.gcp_region",
    "context.AWS_Param_Store_Region":                "context.gcp_region",
}
"""

# Use Excel mappings if available, otherwise fall back to hardcoded (for backward compatibility)
if USE_EXCEL_MAPPINGS and EXCEL_CONTEXT_VARIABLE_MAP:
    CONTEXT_VARIABLE_REPLACEMENTS = EXCEL_CONTEXT_VARIABLE_MAP
else:
    # Fallback to hardcoded mappings (uncomment the above if Excel not available)
    CONTEXT_VARIABLE_REPLACEMENTS = {}

"""
# Context parameter NAME replacements (for the name="" attribute in contextParameter)
# NOTE: Redshift/AWS/S3 prefixed variables are now handled by pattern-based conversion
# Pattern applies automatically: Redshift_* -> BQ_*, AWS_* -> GCP_*, S3_* -> GCS_* (CASE PRESERVED)
# NOTE: This is now loaded from Excel file. Keeping for reference only.
CONTEXT_PARAM_NAME_REPLACEMENTS = {
    "s3inputpath":                          "gcsinputpath",
    "s3lookuppath":                         "gcslookuppath",
    "s3rejectpath":                         "gcsrejectpath",
    "c360_s3_static_path":                  "c360_gcs_static_path",
    "c360_s3_lookup_path":                  "c360_gcs_lookup_path",
    "Globaldatalake_Bucket_name":           "globaldatalake_bucket_name_gcp",
    "Globaldatalake_Access_Key_Id":         "globaldatalake_bucket_name_gcp",
    "Globaldatalake_Secret_Access_Key":     "globaldatalake_bucket_name_gcp",
    "Globalraw_Bucket_Name":                "globalraw_bucket_name_gcp",
    "Globalraw_Access_Key_Id":              "globalraw_bucket_name_gcp",
    "Globalraw_Secret_Access_Key":          "globalraw_bucket_name_gcp",
    "Globaloutbound_Bucket_Name":           "globaloutbound_bucket_name_gcp",
    "Globaloutbound_Access_Key_Id":         "globaloutbound_bucket_name_gcp",
    "Globaloutbound_Secret_Access_Key":     "globaloutbound_bucket_name_gcp",
    "GlobalDataWork_Bucket_name":           "globaldatawork_bucket_name_gcp",
    "S3_Bucket_GDAP_rejects":              "gcs_bucket_gdap_rejects_gcp",
    "S3_Bucket_GDAP_lookups":              "gcs_bucket_gdap_lookups_gcp",
    "S3_Bucket_GDAP_archive":              "gcs_bucket_gdap_archive_gcp",
    "aws_region":                           "gcp_region",
    "AWS_Param_Store_Region":               "gcp_region",
}
"""

# Use Excel mappings if available
if USE_EXCEL_MAPPINGS and VARIABLE_RENAMES:
    CONTEXT_PARAM_NAME_REPLACEMENTS = VARIABLE_RENAMES
else:
    CONTEXT_PARAM_NAME_REPLACEMENTS = {}

# =============================================================================
# LABEL REPLACEMENTS
# Replaces label text in component LABEL fields.
# =============================================================================
LABEL_REPLACEMENTS = {
    # S3 → GCS
    "S3_Connection":    "GCS_Connection",
    "S3_connection":    "GCS_connection",
    "s3_connection":    "gcs_connection",
    "S3_Close":         "GCS_Close",
    "S3_close":         "GCS_close",
    "s3_close":         "gcs_close",
    "S3_Get":           "GCS_Get",
    "S3_Put":           "GCS_Put",
    "S3_Copy":          "GCS_Copy",
    "S3_List":          "GCS_List",
    "S3_Delete":        "GCS_Delete",
    "s3_metadata":      "GCS_metadata",
    "S3_metadata":      "GCS_metadata",
    "s3_Metadata":      "GCS_Metadata",

    # Redshift → BigQuery
    "RS_Connection":    "BQ_Connection",
    "RS_connection":    "BQ_connection",
    "rs_connection":    "bq_connection",
    "RS_Close":         "BQ_Close",
    "rs_close":         "bq_close",

    # Generic S3/RS text in labels
    "S3_TO_RS":         "GCS_TO_BQ",
    "s3_to_rs":         "gcs_to_bq",
    "RS_STG":           "BQ_STG",
    "rs_stg":           "bq_stg",
    "COPY_S3":          "LOAD_GCS",
    "copy_s3":          "load_gcs",
}

# =============================================================================
# NEW GCP CONTEXT PARAMETERS TO ADD
# These are injected into all context blocks of child jobs.
# =============================================================================
NEW_GCP_CONTEXT_PARAMS = {
    "gcp_bq_prod_dataset":   {"type": "id_String", "value": "prod_tables",                  "comment": "BigQuery production dataset"},
    "gcp_tgt_project":       {"type": "id_String", "value": "prj-cp-dac360usstr-dev01",     "comment": "GCP target project"},
    "gcp_bq_bronze_dataset": {"type": "id_String", "value": "prod_bronze",                  "comment": "BigQuery bronze dataset"},
    "gcp_src_project":       {"type": "id_String", "value": "prj-cp-dac360usvws-dev01",     "comment": "GCP source project"},
    "gcsinputpath":          {"type": "id_String", "value": "",                              "comment": "GCS input path"},
    "gcslookuppath":         {"type": "id_String", "value": "",                              "comment": "GCS lookup path"},
    "gcsrejectpath":         {"type": "id_String", "value": "",                              "comment": "GCS reject path"},
}

# =============================================================================
# SKIP PATTERNS - Job names matching these are NOT child jobs
# =============================================================================
SKIP_PATTERNS = ["GrandMaster", "Master", "ABAC", "Grandmaster", "grandmaster", "master"]
# We use case-insensitive matching in the actual code

# =============================================================================
# GCP BIGQUERY SQL BATCH TRANSLATION API CONFIGURATION
# =============================================================================
USE_BQ_BATCH_API = True  # Set to True to enable GCS-based Batch Translation API
GCS_BUCKET_NAME = "dmgcp-del-155-raw"  # GCS Bucket for temp translation files
GCS_INPUT_PREFIX = "talend-repointing/input"    # GCS folder for uploading Redshift queries
GCS_OUTPUT_PREFIX = "talend-repointing/output"  # GCS folder for downloading BigQuery queries
GCP_TRANSLATION_PROJECT_ID = "dmgcp-del-155"  # GCP Project to charge for translation API calls
GCP_TRANSLATION_LOCATION = "us"  # GCP Location for the translation service

# =============================================================================
# SQL CONVERSION CONFIGURATION
# =============================================================================
INJECT_GCP_PROJECT_IN_SCHEMAS = False  # Set to True to inject context.gcp_src_project/gcp_tgt_project before schema names
                                       # True: "+context.gcp_src_project+"."+context.bq_custdb_mkt_schema+"
                                       # False: "+context.bq_custdb_mkt_schema+"

# =============================================================================
# REPORT GENERATION CONFIGURATION
# =============================================================================
REPORT_FORMAT = "both"  # Report format: "md", "html", or "both"
                        # "md"   - Markdown files, fast generation, easy to edit
                        # "html" - Beautiful HTML reports, open in browser, no dependencies
                        # "both" - Generate both MD and HTML formats

