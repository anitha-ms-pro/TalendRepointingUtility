"""
Talend AWS → GCP Repointing Utility
=====================================
Main script to process Talend job folders and repoint child jobs from
AWS (Redshift/S3) to GCP (BigQuery/GCS).

Usage:
    python talend_repoint.py <jobs_folder_path>
    python talend_repoint.py <jobs_folder_path> --dry-run
    python talend_repoint.py <jobs_folder_path> --specific-folders CUST360_KIOSK CUST360_CCP_LOADS

Examples:
    python talend_repoint.py "D:\\Talend\\workspace\\process\\Jobs"
    python talend_repoint.py "D:\\Talend\\workspace\\process\\Jobs" --dry-run
    python talend_repoint.py "D:\\Talend\\workspace\\process\\Jobs" --specific-folders CUST360_KIOSK
"""

import os
import sys
import re
import shutil
import argparse
import logging
from datetime import datetime
from pathlib import Path

# Add script directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    COMPONENT_REPLACEMENTS,
    COMPONENTS_TO_REMOVE,
    UNIQUE_NAME_PREFIX_REPLACEMENTS,
    CONTEXT_VARIABLE_REPLACEMENTS,
    CONTEXT_PARAM_NAME_REPLACEMENTS,
    LABEL_REPLACEMENTS,
    NEW_GCP_CONTEXT_PARAMS,
    USE_BQ_BATCH_API,
)
from sql_converter import (
    convert_redshift_to_bigquery,
    get_manual_review_flags
)


# =============================================================================
# GCP BATCH TRANSLATION API HELPERS
# =============================================================================
import html

def escape_xml_attr(val: str) -> str:
    """Escapes string value for safe inclusion in an XML attribute."""
    # Escape amp first so we don't double escape
    val = val.replace('&', '&amp;')
    val = val.replace('<', '&lt;')
    val = val.replace('>', '&gt;')
    val = val.replace('"', '&quot;')
    val = val.replace('\r', '&#13;')
    val = val.replace('\n', '&#10;')
    return val


def preprocess_redshift_sql(sql: str) -> str:
    """
    Preprocess Redshift SQL to fix non-standard syntax before translation.

    This function performs multiple transformations to ensure the SQL is compatible
    with GCP Batch Translation API:
    1. Clean backslash escapes
    2. Strip SQL Server square brackets
    3. Normalize DELETE syntax
    4. Replace COPY/UNLOAD with placeholders (they can't be parsed by API)

    Returns the preprocessed SQL, or original SQL if any step fails.
    """
    original_sql = sql

    try:
        # 0. Clean backslash escapes before quotes (e.g. \" -> " and \' -> ')
        # these cause GCP Batch Translation compiler errors: "Unknown token: \"
        sql = sql.replace('\\"', '"')
        sql = sql.replace("\\'", "'")

        # 1. Strip square brackets [table] -> table
        sql = re.sub(r'\[([a-zA-Z0-9_]+)\]', r'\1', sql)

        # 2. Rewrite DELETE target FROM source WHERE -> DELETE FROM target USING source WHERE
        # Handle both simple and aliased table names
        ws = r"\s+"
        delete_pattern = re.compile(
            rf"\bDELETE{ws}(?!FROM{ws}[^;]+{ws}USING\b)(?:FROM{ws})?([^;]+?){ws}FROM{ws}([^;]+?){ws}WHERE\b",
            re.IGNORECASE | re.DOTALL
        )

        def replace_delete(match):
            target = match.group(1).strip()
            source = match.group(2).strip()
            # Remove any alias from target (e.g., "table t" -> "table")
            # Keep only the first word/identifier
            target_parts = target.split()
            target_clean = target_parts[0] if target_parts else target
            return f"DELETE FROM {target_clean} USING {source} WHERE"

        sql = delete_pattern.sub(replace_delete, sql)

        # 3. Replace COPY commands with placeholders containing hex-encoded original SQL
        # Use non-greedy matching to handle escaped semicolons in paths
        copy_pattern = re.compile(
            r"\bCOPY\s+\S+\s+FROM\s+['\"].*?['\"](?:\s+[A-Z]+(?:\s+[^;]+?)?)*?\s*;",
            re.IGNORECASE | re.DOTALL
        )
        def replace_copy(match):
            stmt = match.group(0)
            hex_str = stmt.encode('utf-8').hex()
            return f"SELECT 1; /* COPY_PLACEHOLDER:{hex_str} */"

        sql = copy_pattern.sub(replace_copy, sql)

        # 4. Replace UNLOAD commands with placeholders containing hex-encoded original SQL
        # Use non-greedy matching to properly capture complete UNLOAD statement
        unload_pattern = re.compile(
            r"\bUNLOAD\s*\(\s*['\"].*?['\"].*?\)\s*TO\s+['\"].*?['\"](?:\s+[A-Z]+(?:\s+[^;]+?)?)*?\s*;",
            re.IGNORECASE | re.DOTALL
        )
        def replace_unload(match):
            stmt = match.group(0)
            hex_str = stmt.encode('utf-8').hex()
            return f"SELECT 1; /* UNLOAD_PLACEHOLDER:{hex_str} */"

        sql = unload_pattern.sub(replace_unload, sql)

        return sql

    except Exception as e:
        # If any preprocessing step fails, return the original SQL
        # This ensures we don't break valid SQL due to edge cases
        import sys
        print(f"⚠️  Warning: SQL preprocessing failed: {e}. Using original SQL.", file=sys.stderr)
        return original_sql


def extract_queries_from_item(item_path: str, logger: logging.Logger) -> dict:
    """
    Extracts SQL queries from Talend .item XML content.
    Returns a dict mapping component UNIQUE_NAME -> query_string.
    """
    try:
        with open(item_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        logger.error(f"  Failed to read file {item_path} for SQL extraction: {e}")
        return {}
        
    queries = {}
    # Find all <node ... componentName="...">...</node> blocks
    node_pattern = re.compile(r'<node\s+componentName="([^"]+)"[^>]*>(.*?)</node>', re.DOTALL)
    
    for match in node_pattern.finditer(content):
        comp_name = match.group(1)
        node_content = match.group(2)
        
        # We only care about components that contain SQL queries
        # These are components replaced by BQ components in config
        sql_components = {
            'tRedshiftRow', 'tRedshiftInput', 'tRedshiftUnload',
            'tSnowflakeRow', 'tSnowflakeInput',
            'tMSSqlRow', 'tMSSqlInput', 'tMSSqlSCD',
            'tOracleRow', 'tOracleInput',
            'tDBRow', 'tDBInput',
            'tBigQuerySQLRow', 'tBigQueryInput'
        }
        if comp_name not in sql_components:
            continue
            
        # Extract UNIQUE_NAME
        uname_match = re.search(r'name="UNIQUE_NAME"[^>]*value="([^"]+)"', node_content)
        if not uname_match:
            continue
        uname = uname_match.group(1)
        
        # Extract QUERY value
        query_match = re.search(r'name="QUERY"[^>]*value="([^"]*?)"', node_content, re.DOTALL)
        if query_match:
            query_val = query_match.group(1)
            # Only store if it's not empty/whitespace
            if query_val.strip() and query_val.strip() not in ('&quot;&quot;', '""'):
                # Unescape XML entities for GCP Batch Translation
                unescaped_query = html.unescape(query_val)
                # Preprocess query to fix syntax issues before supplying to GCP Batch Translation
                preprocessed = preprocess_redshift_sql(unescaped_query)
                queries[uname] = preprocessed
                
    return queries


def run_batch_translation(queries_to_translate: dict, logger: logging.Logger) -> dict:
    """
    Runs the GCP BigQuery Batch Translation workflow on all extracted queries.

    Args:
        queries_to_translate: dict of (job_name, component_name) -> original_sql
        logger: Logger instance

    Returns:
        dict of (job_name, component_name) -> detokenized_translated_sql
    """
    import os
    import shutil
    import time
    from google.cloud import storage
    from google.cloud import bigquery_migration_v2
    from config import (
        GCS_BUCKET_NAME,
        GCS_INPUT_PREFIX,
        GCS_OUTPUT_PREFIX,
        GCP_TRANSLATION_PROJECT_ID,
        GCP_TRANSLATION_LOCATION
    )
    from sql_converter import _tokenize_talend_sql, _detokenize_talend_sql

    logger.info(f"\n[Batch Translation] Found {len(queries_to_translate)} query/queries to translate via GCP Batch API")

    # Add timestamp-based isolation to prevent race conditions in parallel runs
    run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    gcs_input_prefix = f"{GCS_INPUT_PREFIX}/{run_timestamp}"
    gcs_output_prefix = f"{GCS_OUTPUT_PREFIX}/{run_timestamp}"
    logger.info(f"  Using isolated GCS paths with timestamp: {run_timestamp}")
    
    # Create temp dirs
    local_in_dir = "temp_sql_repoint"
    local_out_dir = "temp_sql_repoint_translated"
    os.makedirs(local_in_dir, exist_ok=True)
    os.makedirs(local_out_dir, exist_ok=True)
    
    tokenization_metadata = {}
    
    # 1. Tokenize and write locally
    # NOTE: Removed preprocess_placeholders_for_api() - it was too aggressive and caused
    # more parser errors (48% failure rate). API handles raw talendvar placeholders better!
    for (job_name, comp_name), orig_sql in queries_to_translate.items():
        res = _tokenize_talend_sql(orig_sql)
        tok_sql = res[0]

        # Send tokenized SQL directly to API (NO preprocessing)
        # API is smart enough to handle unknown identifiers like talendvar0, talendvar1
        api_ready_sql = tok_sql

        # File key
        file_key = f"{job_name}__{comp_name}.sql"
        local_file = os.path.join(local_in_dir, file_key)

        with open(local_file, "w", encoding="utf-8") as f:
            f.write(api_ready_sql)
            
        tokenization_metadata[file_key] = res
        
    # 2. Upload to GCS
    storage_client = storage.Client()
    bucket = storage_client.bucket(GCS_BUCKET_NAME)
    
    # Clean output GCS directory first (only this timestamp's folder)
    logger.info("  Cleaning GCS output folder...")
    blobs_out = list(bucket.list_blobs(prefix=gcs_output_prefix))
    from concurrent.futures import ThreadPoolExecutor
    if blobs_out:
        def delete_blob(blob):
            try:
                blob.delete()
            except Exception as e:
                logger.warning(f"  Failed to delete output blob {blob.name}: {e}")
        with ThreadPoolExecutor(max_workers=32) as executor:
            list(executor.map(delete_blob, blobs_out))
        
    # Clean input GCS directory first (only this timestamp's folder)
    logger.info("  Cleaning GCS input folder...")
    blobs_in = list(bucket.list_blobs(prefix=gcs_input_prefix))
    if blobs_in:
        def delete_blob(blob):
            try:
                blob.delete()
            except Exception as e:
                logger.warning(f"  Failed to delete input blob {blob.name}: {e}")
        with ThreadPoolExecutor(max_workers=32) as executor:
            list(executor.map(delete_blob, blobs_in))
        
    # Upload new inputs in parallel
    logger.info(f"  Uploading {len(tokenization_metadata)} query file(s) to GCS (gs://{GCS_BUCKET_NAME}/{gcs_input_prefix}/)...")
    from concurrent.futures import ThreadPoolExecutor
    
    def upload_file(file_key):
        local_file = os.path.join(local_in_dir, file_key)
        blob = bucket.blob(f"{gcs_input_prefix}/{file_key}")
        blob.upload_from_filename(local_file)
        
    with ThreadPoolExecutor(max_workers=32) as executor:
        list(executor.map(upload_file, tokenization_metadata.keys()))
        
    logger.info(f"  Successfully uploaded {len(tokenization_metadata)} file(s) to GCS.")
    
    # 3. Trigger Migration Job
    logger.info("  Triggering BigQuery Migration Workflow (Batch Translation)...")
    migration_client = bigquery_migration_v2.MigrationServiceClient()
    parent = f"projects/{GCP_TRANSLATION_PROJECT_ID}/locations/{GCP_TRANSLATION_LOCATION}"
    
    source_dialect = bigquery_migration_v2.Dialect(redshift_dialect=bigquery_migration_v2.RedshiftDialect())
    target_dialect = bigquery_migration_v2.Dialect(bigquery_dialect=bigquery_migration_v2.BigQueryDialect())
    
    translation_config = bigquery_migration_v2.TranslationConfigDetails(
        gcs_source_path=f"gs://{GCS_BUCKET_NAME}/{gcs_input_prefix}/",
        gcs_target_path=f"gs://{GCS_BUCKET_NAME}/{gcs_output_prefix}/",
        source_dialect=source_dialect,
        target_dialect=target_dialect
    )
    
    task = bigquery_migration_v2.MigrationTask(
        type_="Translation_Redshift2BQ",
        translation_config_details=translation_config
    )
    
    workflow = bigquery_migration_v2.MigrationWorkflow(
        tasks={"translation_task": task}
    )
    
    request = bigquery_migration_v2.CreateMigrationWorkflowRequest(
        parent=parent,
        migration_workflow=workflow
    )
    
    job_workflow = migration_client.create_migration_workflow(request=request)
    workflow_name = job_workflow.name
    logger.info(f"  Workflow Created: {workflow_name}")
    
    # 4. Poll for completion with exponential backoff
    logger.info("  Polling job status with exponential backoff...")
    wait_time = 10  # Start with 10 seconds
    max_wait = 60   # Maximum 60 seconds between polls

    while True:
        status_workflow = migration_client.get_migration_workflow(name=workflow_name)
        state = status_workflow.state
        logger.info(f"    Current Workflow State: {state.name}")

        # Check individual tasks
        failed_tasks = []
        for task_name, t_obj in status_workflow.tasks.items():
            logger.info(f"      Task '{task_name}' State: {t_obj.state.name}")
            if t_obj.state == bigquery_migration_v2.MigrationTask.State.FAILED:
                failed_tasks.append((task_name, t_obj))

        if state == bigquery_migration_v2.MigrationWorkflow.State.COMPLETED:
            if failed_tasks:
                logger.error("  ❌ Some tasks failed in the workflow:")
                for name, t_obj in failed_tasks:
                    logger.error(f"    - {name} failed: {t_obj.processing_error}")
                raise RuntimeError("GCP Batch Translation failed.")
            logger.info("  [OK] Translation Workflow Finished Successfully!")
            break
        elif state == bigquery_migration_v2.MigrationWorkflow.State.PAUSED:
            logger.error("  ❌ Workflow paused.")
            raise RuntimeError("GCP Batch Translation paused.")

        # Exponential backoff: increase wait time by 1.5x each iteration, max 60s
        logger.info(f"    Waiting {wait_time}s before next poll (exponential backoff)...")
        time.sleep(wait_time)
        wait_time = min(int(wait_time * 1.5), max_wait)
        
    # 5. Download outputs
    logger.info("  Downloading translated output from GCS...")
    blobs_out = list(bucket.list_blobs(prefix=gcs_output_prefix))
    if not blobs_out:
        logger.error("  ❌ No output files found in GCS output directory!")
        raise RuntimeError("GCP Batch Translation returned no outputs.")
        
    translated_queries = {}
    
    from concurrent.futures import ThreadPoolExecutor
    
    def download_and_process_blob(blob):
        filename = os.path.basename(blob.name)
        if filename == "batch_translation_report.csv":
            return None
            
        local_file = os.path.join(local_out_dir, filename)
        blob.download_to_filename(local_file)
        
        with open(local_file, "r", encoding="utf-8") as f:
            translated_raw = f.read()
            
        # Restore preprocessed COPY and UNLOAD placeholders
        from sql_converter import restore_placeholders
        translated_raw = restore_placeholders(translated_raw)

        # NOTE: No need to restore_placeholders_after_api() since we're not preprocessing anymore!
            
        if "-- ERROR_" in translated_raw:
            logger.warning(f"  ⚠️  GCP Batch Translation returned a parser error for query: {filename}. Falling back to local rules.")
            return None
            
        matched_key = None
        for key in tokenization_metadata.keys():
            if key.lower() == filename.lower() or filename.lower() in key.lower():
                matched_key = key
                break
                
        if not matched_key:
            matched_key = filename
            
        if matched_key in tokenization_metadata:
            res = tokenization_metadata[matched_key]
            # Use *res[1:] to unpack all whitespace and quote variables robustly
            detok_sql = _detokenize_talend_sql(
                translated_raw, *res[1:]
            )
            
            key_no_ext = matched_key[:-4]
            parts = key_no_ext.split("__")
            if len(parts) >= 2:
                job_name = parts[0]
                comp_name = "__".join(parts[1:])
                return (job_name, comp_name, detok_sql)
        return None
        
    logger.info(f"  Downloading and processing {len(blobs_out)} files in parallel...")
    with ThreadPoolExecutor(max_workers=32) as executor:
        results = list(executor.map(download_and_process_blob, blobs_out))
        
    for res in results:
        if res:
            job_name, comp_name, detok_sql = res
            translated_queries[(job_name, comp_name)] = detok_sql
                
    # 6. Cleanup local temp files
    try:
        shutil.rmtree(local_in_dir)
        shutil.rmtree(local_out_dir)
    except Exception as e:
        logger.warning(f"  Failed to clean up temp directories: {e}")
        
    return translated_queries



# =============================================================================
# LOGGING SETUP
# =============================================================================
def setup_logging(log_dir: str) -> logging.Logger:
    """Setup logging to both file and console."""
    os.makedirs(log_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(log_dir, f"repoint_{timestamp}.log")
    
    logger = logging.getLogger("TalendRepoint")
    logger.setLevel(logging.DEBUG)
    
    # File handler (detailed)
    fh = logging.FileHandler(log_file, encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    
    # Console handler (info level)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter('%(message)s'))
    
    logger.addHandler(fh)
    logger.addHandler(ch)
    
    logger.info(f"Log file: {log_file}")
    return logger


# =============================================================================
# CHILD JOB IDENTIFICATION
# =============================================================================
def is_child_job(filename: str) -> bool:
    """
    Determine if a .item file is a child job (not GrandMaster, Master, or ABAC).
    
    Hierarchy: GrandMaster > Master > ABAC > Child
    Child jobs are identified by NOT having GrandMaster, Master, or ABAC
    in their filename.
    
    Args:
        filename: The .item filename (e.g., 'C360_KIOSK_TRN_HDR_LOAD_0.1.item')
    
    Returns:
        True if this is a child job, False otherwise.
    """
    name_lower = filename.lower()
    # Remove the version suffix for checking (e.g., _0.1.item)
    base_name = re.sub(r'_\d+\.\d+\.item$', '', filename, flags=re.IGNORECASE)
    base_lower = base_name.lower()
    
    skip_keywords = ['grandmaster', 'master', 'abac']
    
    for keyword in skip_keywords:
        # Check if keyword appears as a distinct segment
        # Split by underscore and check each segment
        segments = base_lower.split('_')
        if keyword in segments:
            return False
        
        # Also check if the name ends with the keyword
        if base_lower.endswith(f'_{keyword}') or base_lower.endswith(keyword):
            return False
    
    return True


def is_actual_sql(sql: str) -> bool:
    """
    Check if the query string contains any standard SQL statement keywords.
    Helps avoid sending pure Java expressions (context/globalMap) to the translation API.
    Uses word boundaries to avoid false positives in string literals.
    """
    sql_lower = sql.lower()
    sql_keywords = ['select', 'insert', 'update', 'delete', 'create', 'drop', 'merge', 'with', 'truncate']
    return any(re.search(rf'\b{kw}\b', sql_lower) for kw in sql_keywords)


def find_child_job_items(folder_path: str) -> list:
    """
    Find all child job .item files in a folder (non-recursive for the folder itself,
    but also checks subdirectories since some jobs are nested).
    
    Args:
        folder_path: Path to a job group folder (e.g., CUST360_KIOSK)
    
    Returns:
        List of full paths to child job .item files
    """
    child_items = []
    
    # Walk through the folder and all subdirectories
    for root, dirs, files in os.walk(folder_path):
        for f in files:
            if f.endswith('.item') and is_child_job(f):
                child_items.append(os.path.join(root, f))
    
    return child_items


# =============================================================================
# XML CONTENT PROCESSING
# =============================================================================
def build_unique_name_map(content: str) -> dict:
    """
    Build a mapping of old UNIQUE_NAME values to new ones based on component replacements.
    Ensures that no duplicate UNIQUE_NAMEs are generated even if multiple components
    map to the same new prefix (e.g., tRedshiftRow and tDBRow to tBigQuerySQLRow).

    IMPORTANT: Deduplicates UNIQUE_NAMEs first, as they can appear multiple times
    in the XML (e.g., in component definitions and connection references).
    """
    name_map = {}

    unique_pattern = re.compile(r'name="UNIQUE_NAME"[^>]*value="([^"]+)"')
    all_uniques_raw = [m.group(1) for m in unique_pattern.finditer(content)]

    # DEDUPLICATE: Each UNIQUE_NAME can appear multiple times in XML
    # (e.g., in component definition + connection references)
    # Process each unique value only ONCE to preserve numbers correctly
    all_uniques = list(set(all_uniques_raw))

    final_names = set()
    to_replace = []

    for old_unique in all_uniques:
        needs_replace = False
        for old_prefix, new_prefix in UNIQUE_NAME_PREFIX_REPLACEMENTS.items():
            if old_unique.startswith(old_prefix):
                needs_replace = True
                to_replace.append((old_unique, old_prefix, new_prefix))
                break
        if not needs_replace:
            final_names.add(old_unique)
            
    for old_unique, old_prefix, new_prefix in to_replace:
        # Try to preserve the original number suffix
        # Extract number from old_unique (e.g., tDBInput_1 -> 1)
        import re as re_module
        number_match = re_module.search(r'_(\d+)$', old_unique)

        if number_match:
            original_number = int(number_match.group(1))
            # Try the original number first
            candidate = f"{new_prefix}_{original_number}"
            if candidate not in final_names:
                final_names.add(candidate)
                name_map[old_unique] = candidate
                continue

        # If original number is taken or doesn't exist, find next available
        idx = 1
        while True:
            candidate = f"{new_prefix}_{idx}"
            if candidate not in final_names:
                final_names.add(candidate)
                name_map[old_unique] = candidate
                break
            idx += 1

    return name_map


def replace_component_names(content: str, logger: logging.Logger) -> str:
    """
    Replace component names in <node componentName="..."> elements.
    Also handles removal of components mapped to None, with proper
    connection rewiring to maintain the execution chain.
    """
    result = content
    removed_unique_names = set()
    
    # First pass: identify components to remove and collect their UNIQUE_NAMEs
    for old_comp, new_comp in COMPONENT_REPLACEMENTS.items():
        if new_comp is None:
            # Find all nodes with this component and get their UNIQUE_NAME
            pattern = re.compile(
                rf'<node\s+componentName="{re.escape(old_comp)}"[^>]*>.*?</node>\s*',
                re.DOTALL
            )
            for match in pattern.finditer(result):
                unique_match = re.search(r'name="UNIQUE_NAME"[^>]*value="([^"]+)"', match.group(0))
                if unique_match:
                    removed_unique_names.add(unique_match.group(1))
                    logger.debug(f"  Removing component: {old_comp} (UNIQUE_NAME: {unique_match.group(1)})")
            
            # Remove the node elements
            result = pattern.sub('', result)
    
    # Rewire connections for removed components
    # For each removed component, find incoming and outgoing connections
    # and rewire: if A -> REMOVED -> C, then make A -> C
    if removed_unique_names:
        for unique_name in removed_unique_names:
            logger.debug(f"  Processing connections for removed component: {unique_name}")
            
            # Parse ALL connections to build a graph
            # Connection format: <connection ... source="X" target="Y" ...> ... </connection>
            # Attributes can be in any order on the opening tag
            
            def extract_attr(conn_text, attr_name):
                m = re.search(rf'\b{attr_name}="([^"]*)"', conn_text)
                return m.group(1) if m else ""

            # Find connections where this removed component is the TARGET (incoming)
            # We need the full connection element AND the source component
            incoming_connections = []
            incoming_re = re.compile(
                r'(<connection\s[^>]*?>.*?</connection>)\s*',
                re.DOTALL
            )
            for conn_match in incoming_re.finditer(result):
                conn_text = conn_match.group(1)
                # Extract source and target from the opening <connection> tag
                source_m = re.search(r'\bsource="([^"]+)"', conn_text)
                target_m = re.search(r'\btarget="([^"]+)"', conn_text)
                if source_m and target_m and target_m.group(1) == unique_name:
                    incoming_connections.append({
                        'full_text': conn_match.group(0),
                        'conn_text': conn_text,
                        'source': source_m.group(1),
                        'target': target_m.group(1),
                        'connectorName': extract_attr(conn_text, 'connectorName'),
                        'label': extract_attr(conn_text, 'label'),
                        'lineStyle': extract_attr(conn_text, 'lineStyle'),
                    })
            
            # Find connections where this removed component is the SOURCE (outgoing)
            outgoing_connections = []
            for conn_match in incoming_re.finditer(result):
                conn_text = conn_match.group(1)
                source_m = re.search(r'\bsource="([^"]+)"', conn_text)
                target_m = re.search(r'\btarget="([^"]+)"', conn_text)
                if source_m and target_m and source_m.group(1) == unique_name:
                    outgoing_connections.append({
                        'full_text': conn_match.group(0),
                        'conn_text': conn_text,
                        'source': source_m.group(1),
                        'target': target_m.group(1),
                        'connectorName': extract_attr(conn_text, 'connectorName'),
                        'label': extract_attr(conn_text, 'label'),
                        'lineStyle': extract_attr(conn_text, 'lineStyle'),
                    })
            
            logger.debug(f"    Incoming connections: {len(incoming_connections)}, Outgoing: {len(outgoing_connections)}")
            
            # Rewire: for each incoming connection, connect it to each outgoing target
            if incoming_connections and outgoing_connections:
                for inc_conn in incoming_connections:
                    new_conns = []
                    for out_conn in outgoing_connections:
                        new_text = out_conn['conn_text']
                        
                        # Replace source and metaname
                        new_text = re.sub(r'\bsource="[^"]+"', f'source="{inc_conn["source"]}"', new_text)
                        if 'metaname=' in new_text:
                            new_text = re.sub(r'\bmetaname="[^"]+"', f'metaname="{inc_conn["source"]}"', new_text)
                        else:
                            new_text = re.sub(r'\bsource="[^"]+"', f'source="{inc_conn["source"]}" metaname="{inc_conn["source"]}"', new_text)
                            
                        # Replace connectorName, label, lineStyle
                        new_text = re.sub(r'\bconnectorName="[^"]+"', f'connectorName="{inc_conn["connectorName"]}"', new_text)
                        new_text = re.sub(r'\blabel="[^"]+"', f'label="{inc_conn["label"]}"', new_text)
                        new_text = re.sub(r'\blineStyle="[^"]+"', f'lineStyle="{inc_conn["lineStyle"]}"', new_text)
                        
                        new_conns.append(new_text)
                        
                    # Replace the old incoming connection with the new connections joined together
                    result = result.replace(inc_conn['full_text'], "\n".join(new_conns) + "\n")
                    logger.debug(f"    Rewired: {inc_conn['source']} -> {', '.join(o['target'] for o in outgoing_connections)} (was -> {unique_name})")
            
            # Now remove ALL remaining connections where source or target is the removed component
            # (the outgoing connections from the removed component are no longer needed)
            def remove_connections_for(content_str, uname):
                """Remove all connection elements referencing uname as source or target."""
                # We need to be careful to match individual connection elements
                result_str = content_str
                conn_re = re.compile(
                    r'<connection\s[^>]*?>.*?</connection>\s*',
                    re.DOTALL
                )
                connections_to_remove = []
                for cm in conn_re.finditer(result_str):
                    ct = cm.group(0)
                    src = re.search(r'\bsource="([^"]+)"', ct)
                    tgt = re.search(r'\btarget="([^"]+)"', ct)
                    if src and src.group(1) == uname:
                        connections_to_remove.append(ct)
                    elif tgt and tgt.group(1) == uname:
                        connections_to_remove.append(ct)
                
                for conn in connections_to_remove:
                    result_str = result_str.replace(conn, '')
                
                return result_str
            
            result = remove_connections_for(result, unique_name)
            
            # Remove subjob elements for the removed component
            subjob_pattern2 = re.compile(
                rf'<subjob>(?:(?!</?subjob>).)*?value="{re.escape(unique_name)}"(?:(?!</?subjob>).)*?</subjob>\s*',
                re.DOTALL
            )
            result = subjob_pattern2.sub('', result)
    
    # Second pass: replace component names
    for old_comp, new_comp in COMPONENT_REPLACEMENTS.items():
        if new_comp is not None:
            old_pattern = f'componentName="{old_comp}"'
            new_pattern = f'componentName="{new_comp}"'
            if old_pattern in result:
                count = result.count(old_pattern)
                result = result.replace(old_pattern, new_pattern)
                logger.debug(f"  Replaced componentName: {old_comp} → {new_comp} ({count} occurrences)")
    
    return result, removed_unique_names


def replace_unique_names(content: str, name_map: dict, logger: logging.Logger) -> str:
    """
    Replace all UNIQUE_NAME references throughout the XML.
    This includes:
    - UNIQUE_NAME value in node elements
    - CONNECTION value referencing components
    - source/target in connection elements
    - metaname in connection elements
    - UNIQUE_NAME in subjob elements
    """
    result = content
    
    for old_name, new_name in name_map.items():
        if old_name != new_name:
            # Replace exactly quoted
            old_quoted = f'"{old_name}"'
            new_quoted = f'"{new_name}"'
            
            count = result.count(old_quoted)
            if count > 0:
                result = result.replace(old_quoted, new_quoted)
                logger.debug(f"  Renamed UNIQUE_NAME: {old_name} → {new_name} ({count} quoted refs)")
                
            # Replace inside strings, like "tS3List_1_CURRENT_KEY"
            old_global = f'{old_name}_'
            new_global = f'{new_name}_'
            count_global = result.count(old_global)
            if count_global > 0:
                result = result.replace(old_global, new_global)
                logger.debug(f"  Renamed global references: {old_global}* → {new_global}* ({count_global} refs)")
    
    return result


def replace_labels(content: str, logger: logging.Logger) -> str:
    """
    Replace label text in LABEL fields and other display text.
    Uses pattern matching on label values.
    """
    result = content
    
    for old_label, new_label in LABEL_REPLACEMENTS.items():
        pattern = re.compile(re.escape(old_label), re.IGNORECASE)
        matches = pattern.findall(result)
        if matches:
            # Only replace exact case matches from our config for predictability
            if old_label in result:
                result = result.replace(old_label, new_label)
                logger.debug(f"  Replaced label: {old_label} → {new_label}")
    
    # Generic S3/RS text replacements in labels only (within LABEL value fields)
    # Replace s3_ prefix with GCS_ in label values
    label_pattern = re.compile(r'(name="LABEL"[^>]*value=")([^"]*?)(")')
    
    def transform_label(match):
        prefix = match.group(1)
        label_val = match.group(2)
        suffix = match.group(3)
        
        # Apply generic transforms
        new_val = label_val
        new_val = re.sub(r'\bs3_', 'GCS_', new_val, flags=re.IGNORECASE)
        new_val = re.sub(r'\bS3_', 'GCS_', new_val)
        new_val = re.sub(r'\bRS_', 'BQ_', new_val)
        new_val = re.sub(r'\brs_', 'bq_', new_val)
        new_val = re.sub(r'\bredshift\b', 'bigquery', new_val, flags=re.IGNORECASE)
        
        if new_val != label_val:
            return prefix + new_val + suffix
        return match.group(0)
    
    result = label_pattern.sub(transform_label, result)
    
    return result


def translate_copycommand_value(old_val: str) -> str:
    """
    Translate AWS Redshift COPY options to BigQuery LOAD DATA option format.
    """
    if not old_val:
        return ""
    val_upper = old_val.upper()
    options = []
    
    # 1. Format
    if "PARQUET" in val_upper:
        options.append("format='PARQUET'")
    elif "ORC" in val_upper:
        options.append("format='ORC'")
    elif "AVRO" in val_upper:
        options.append("format='AVRO'")
    elif "JSON" in val_upper:
        options.append("format='JSON'")
    else:
        options.append("format='CSV'")
        
    # 2. Delimiter
    delim_match = re.search(r"\bDELIMITER\s+(?:AS\s+)?['\"]?([^'\"\s]+)['\"]?", old_val, re.IGNORECASE)
    if delim_match:
        options.append(f"field_delimiter='{delim_match.group(1)}'")
        
    # 3. Compression
    if "GZIP" in val_upper:
        options.append("compression='GZIP'")
        
    # 4. Null Marker
    null_match = re.search(r"\bNULL\s+(?:AS\s+)?['\"]?([^'\"\s]+)['\"]?", old_val, re.IGNORECASE)
    if null_match:
        options.append(f"null_marker='{null_match.group(1)}'")
        
    # 5. IGNOREHEADER / skip_leading_rows
    ignore_match = re.search(r"\bIGNOREHEADER\s+(\d+)", old_val, re.IGNORECASE)
    if ignore_match:
        options.append(f"skip_leading_rows={ignore_match.group(1)}")
        
    # 6. Max Errors / max_bad_records
    maxerror_match = re.search(r"\bmaxerror\s+(\d+)", old_val, re.IGNORECASE)
    if maxerror_match:
        options.append(f"max_bad_records={maxerror_match.group(1)}")
        
    if options:
        return ", " + ", ".join(options)
    return ""


def translate_copycommand_in_context(content: str, logger: logging.Logger) -> str:
    """
    Find contextParameter elements with name="copycommand" and translate their value to BQ options.
    """
    pattern = re.compile(r'<contextParameter\s+([^>]+)/>')
    
    def replace_param(match):
        attributes_str = match.group(1)
        if 'name="copycommand"' in attributes_str or "name='copycommand'" in attributes_str:
            val_match = re.search(r'value="([^"]*?)"', attributes_str)
            if not val_match:
                val_match = re.search(r"value='([^']*?)'", attributes_str)
            
            if val_match:
                old_val = val_match.group(1)
                new_val = translate_copycommand_value(old_val)
                if old_val != new_val:
                    logger.info(f"  Translating contextParameter copycommand: '{old_val}' -> '{new_val}'")
                    if 'value="' in attributes_str:
                        new_attributes = attributes_str.replace(f'value="{old_val}"', f'value="{new_val}"')
                    else:
                        new_attributes = attributes_str.replace(f"value='{old_val}'", f"value='{new_val}'")
                    return f'<contextParameter {new_attributes}/>'
        return match.group(0)
        
    return pattern.sub(replace_param, content)


def replace_context_variables(content: str, logger: logging.Logger) -> str:
    """
    Replace context variable references throughout the XML content.
    Lowercases the variable names according to user instructions.

    IMPORTANT: Explicit mappings from config.py take ABSOLUTE precedence.
    If a variable is in CONTEXT_VARIABLE_REPLACEMENTS, use that exact mapping
    without any pattern-based processing.
    """
    result = content

    # Translate copycommand parameter values at context level
    result = translate_copycommand_in_context(result, logger)

    # STEP 1: Apply explicit mappings FIRST (take absolute precedence)
    # Track which variables have been explicitly mapped so patterns don't touch them
    explicitly_mapped_vars = set()

    for old_ctx, new_ctx in CONTEXT_VARIABLE_REPLACEMENTS.items():
        if old_ctx in result:
            count = result.count(old_ctx)
            result = result.replace(old_ctx, new_ctx)
            # Extract variable name (e.g., "context.S3_bucket" -> "S3_bucket")
            var_name = old_ctx.replace('context.', '')
            explicitly_mapped_vars.add(var_name.lower())
            logger.debug(f"  Explicit mapping: {old_ctx} -> {new_ctx} ({count} occurrences)")

    # STEP 2: Apply pattern-based replacements ONLY for variables NOT explicitly mapped

    # S3 -> GCS (preserve case)
    def s3_to_gcs(match):
        var_name_with_prefix = match.group(0).replace('context.', '')  # e.g., "S3_bucket"
        if var_name_with_prefix.lower() in explicitly_mapped_vars:
            return match.group(0)  # Keep unchanged, already processed by explicit mapping
        var_name = match.group(1)  # PRESERVE ORIGINAL CASE
        suffix = "_gcp" if "bucket" in var_name.lower() else ""
        return f"context.GCS_{var_name}{suffix}"

    result, count = re.subn(r'context\.S3_([a-zA-Z0-9_]+)', s3_to_gcs, result, flags=re.IGNORECASE)
    if count > 0:
        logger.debug(f"  Pattern-based: S3_ -> GCS_ ({count} occurrences, case preserved)")

    # Redshift -> BQ (preserve case)
    def redshift_to_bq(match):
        var_name_with_prefix = match.group(0).replace('context.', '')
        if var_name_with_prefix.lower() in explicitly_mapped_vars:
            return match.group(0)
        var_name = match.group(1)  # PRESERVE ORIGINAL CASE
        return f"context.BQ_{var_name}"

    result, count = re.subn(r'context\.Redshift_([a-zA-Z0-9_]+)', redshift_to_bq, result, flags=re.IGNORECASE)
    if count > 0:
        logger.debug(f"  Pattern-based: Redshift_ -> BQ_ ({count} occurrences, case preserved)")

    # AWS -> GCP (preserve case)
    def aws_to_gcp(match):
        var_name_with_prefix = match.group(0).replace('context.', '')
        if var_name_with_prefix.lower() in explicitly_mapped_vars:
            return match.group(0)
        var_name = match.group(1)  # PRESERVE ORIGINAL CASE
        return f"context.GCP_{var_name}"

    result, count = re.subn(r'context\.AWS_([a-zA-Z0-9_]+)', aws_to_gcp, result, flags=re.IGNORECASE)
    if count > 0:
        logger.debug(f"  Pattern-based: AWS_ -> GCP_ ({count} occurrences, case preserved)")
                
    # Now, rename the parameter definitions for JOB-LEVEL contexts only.
    # Job-level contexts do NOT have a repositoryContextId attribute.
    context_param_pattern = re.compile(r'(<contextParameter\s+[^>]*name="([^"]+)"[^>]*>)')
    
    def rename_job_level_context(match):
        full_tag = match.group(1)
        old_name = match.group(2)

        # Check if this is a repository-level context (not job-level)
        # Repository-level: repositoryContextId="ABAC" or any value other than "built-in"
        # Job-level: repositoryContextId="built-in" or NO repositoryContextId attribute
        repo_id_match = re.search(r'repositoryContextId="([^"]*)"', full_tag)
        if repo_id_match:
            repo_id = repo_id_match.group(1)
            # If repositoryContextId is NOT "built-in", it's a repository context - skip renaming
            if repo_id != "built-in":
                return full_tag

        # Determine the new name based on our rules
        # STEP 1: Check explicit mappings FIRST (absolute precedence)
        new_name = None
        for old_ctx, new_ctx in CONTEXT_VARIABLE_REPLACEMENTS.items():
            if old_ctx.replace('context.', '') == old_name:
                new_name = new_ctx.replace('context.', '')
                break

        # STEP 2: Apply pattern-based rules ONLY if no explicit mapping found
        if new_name is None:
            new_name = old_name
            lower_name = old_name.lower()

            if lower_name.startswith('s3_'):
                # PRESERVE CASE: S3_Archive_Bucket -> GCS_Archive_Bucket
                new_name = 'GCS_' + old_name[3:]
                if 'bucket' in lower_name:
                    new_name += '_gcp'
            elif lower_name.startswith('redshift_'):
                # PRESERVE CASE: Redshift_CustDB_MKT_Schema -> BQ_CustDB_MKT_Schema
                new_name = 'BQ_' + old_name[9:]
            elif lower_name.startswith('aws_'):
                # PRESERVE CASE: AWS_Region -> GCP_Region
                new_name = 'GCP_' + old_name[4:]

        if new_name != old_name:
            # Replace name attribute
            full_tag = re.sub(rf'name="{re.escape(old_name)}"', f'name="{new_name}"', full_tag)
            # Replace prompt attribute
            full_tag = re.sub(rf'prompt="{re.escape(old_name)}\?"', f'prompt="{new_name}?"', full_tag)
            logger.debug(f"  Renamed job-level context parameter: {old_name} -> {new_name}")

        return full_tag

    result = context_param_pattern.sub(rename_job_level_context, result)
    
    return result


def convert_sql_in_content(content: str, logger: logging.Logger, job_name: str = None, translated_queries: dict = None, name_map: dict = None) -> tuple:
    """
    Find all SQL queries in the XML content and convert them from
    Redshift to BigQuery syntax. Uses GCP Batch translated queries if available,
    otherwise falls back to local regex-based conversion rules.

    Returns:
        tuple: (converted_content, stats_dict) where stats_dict contains:
            - 'api_translations': count of queries translated via GCP API
            - 'local_conversions': count of queries converted via local rules
    """
    result = content

    # Track conversion statistics
    api_count = 0
    local_count = 0

    # Process <node> blocks individually to associate queries with their component UNIQUE_NAMEs
    node_pattern = re.compile(r'(<node\s+componentName="[^"]+"[^>]*>)(.*?)(</node>)', re.DOTALL)

    def process_node(match):
        nonlocal api_count, local_count
        node_start = match.group(1)
        node_content = match.group(2)
        node_end = match.group(3)
        
        # Check if it has a QUERY parameter
        query_pattern = re.compile(r'(name="QUERY"\s+value=")([^"]*?)(")', re.DOTALL)
        query_match = query_pattern.search(node_content)
        
        if query_match:
            prefix = query_match.group(1)
            sql_content = query_match.group(2)
            suffix = query_match.group(3)
            
            # Extract UNIQUE_NAME of this component
            uname_match = re.search(r'name="UNIQUE_NAME"[^>]*value="([^"]+)"', node_content)
            uname = uname_match.group(1) if uname_match else None
            
            # Look up in translated_queries
            translated = None
            if translated_queries and job_name and uname:
                lookup_key = (job_name, uname)
                if lookup_key in translated_queries:
                    translated = translated_queries[lookup_key]
                    logger.info(f"  -> Using GCP Batch translated SQL for component {uname}")
                else:
                    # Strategy 1: Reverse lookup using name_map (old_name -> new_name)
                    if name_map:
                        for old_name, new_name in name_map.items():
                            if new_name == uname:
                                reverse_key = (job_name, old_name)
                                if reverse_key in translated_queries:
                                    translated = translated_queries[reverse_key]
                                    logger.info(f"  -> Using GCP Batch translated SQL for component {uname} (original: {old_name})")
                                    break
                    
                    # Strategy 2: Try matching by component base type (e.g., tBigQuerySQLRow_1 matches tDBRow_2)
                    # This handles cases where the file was already partially converted
                    if translated is None:
                        # Extract the numeric suffix from uname (e.g., "1" from "tBigQuerySQLRow_1")
                        uname_match_num = re.match(r'(.+?)_(\d+)$', uname)
                        if uname_match_num:
                            uname_prefix = uname_match_num.group(1)
                            uname_num = uname_match_num.group(2)
                            # Check all translated_queries keys for same job
                            for (tj, tc), tv in translated_queries.items():
                                if tj == job_name:
                                    tc_match = re.match(r'(.+?)_(\d+)$', tc)
                                    if tc_match and tc_match.group(2) == uname_num:
                                        # Same numeric suffix — check if the old prefix maps to the new prefix
                                        old_prefix = tc_match.group(1)
                                        from config import UNIQUE_NAME_PREFIX_REPLACEMENTS
                                        expected_new = UNIQUE_NAME_PREFIX_REPLACEMENTS.get(old_prefix)
                                        if expected_new == uname_prefix:
                                            translated = tv
                                            logger.info(f"  -> Using GCP Batch translated SQL for component {uname} (matched from: {tc})")
                                            break
            
            if translated is not None:
                # Strip BQ translation header comments from GCS raw outputs
                translated_clean = re.sub(r'-- Translation time:.*?\n-- Translated to:[^\n]+\n*', '', translated, flags=re.DOTALL | re.IGNORECASE)

                # Still run schema project injection on the translated query
                from sql_converter import _inject_gcp_project_in_schemas, lowercase_table_names_in_sql
                final_sql = _inject_gcp_project_in_schemas(translated_clean)

                # Remove default database and schema prefixes (case-insensitive)
                final_sql = re.sub(r'__(default_database|default_schema)__\.', '', final_sql, flags=re.IGNORECASE)

                # Ensure generic AWS references are replaced and table names are lowercased
                final_sql = final_sql.replace('s3://', 'gs://')

                # Remove backslash-escaped quotes around GCP project/dataset variables
                # Handle raw " form (since html.unescape was used during extraction)
                final_sql = re.sub(r'\\"("\s*\+\s*context\.gcp_[a-z_]+_project)', r'\1', final_sql)
                final_sql = re.sub(r'(context\.gcp_bq_[a-z_]+_dataset\s*\+\s*")\\"', r'\1', final_sql)
                # Handle &quot; form in case it was not unescaped
                final_sql = re.sub(r'\\&quot;(&quot;\s*\+\s*context\.gcp_[a-z_]+_project)', r'\1', final_sql)
                final_sql = re.sub(r'(context\.gcp_bq_[a-z_]+_dataset\s*\+\s*&quot;)\\&quot;', r'\1', final_sql)

                final_sql = lowercase_table_names_in_sql(final_sql)

                # Escape the translated SQL back to safe XML attribute format
                escaped_final_sql = escape_xml_attr(final_sql)

                # Replace query value
                new_query_param = prefix + escaped_final_sql + suffix
                node_content = node_content.replace(query_match.group(0), new_query_param)
                api_count += 1  # Track API translation
            else:
                # Fallback to local rules
                converted = convert_redshift_to_bigquery(sql_content)
                if converted != sql_content:
                    new_query_param = prefix + converted + suffix
                    node_content = node_content.replace(query_match.group(0), new_query_param)
                    local_count += 1  # Track local conversion
                    logger.debug(f"  Converted SQL query locally for component {uname if uname else 'unknown'}")
                    
        return node_start + node_content + node_end
        
    result = node_pattern.sub(process_node, result)
    
    # Also handle CODE fields in tJava components that may contain SQL strings (local rules fallback)
    code_pattern = re.compile(
        r'(name="CODE"\s+value=")([^"]*?)(")',
        re.DOTALL
    )
    
    def convert_code_value(match):
        prefix = match.group(1)
        code_content = match.group(2)
        suffix = match.group(3)
        
        # Only convert if it contains Redshift-specific keywords
        redshift_keywords = ['GETDATE', 'dateadd', 'trunc(', 'UNLOAD', 'NVL(', 'ISNULL(']
        has_redshift = any(kw.lower() in code_content.lower() for kw in redshift_keywords)
        
        if has_redshift:
            converted = convert_redshift_to_bigquery(code_content)
            if converted != code_content:
                logger.debug(f"  Converted SQL in tJava CODE field locally")
            return prefix + converted + suffix
        
        return match.group(0)
    
    result = code_pattern.sub(convert_code_value, result)

    stats = {
        'api_translations': api_count,
        'local_conversions': local_count
    }

    return result, stats


def replace_generic_aws_references(content: str, logger: logging.Logger) -> str:
    """
    Replace remaining generic AWS references:
    - s3:// → gs://
    - AWS-specific parameter names
    - Redshift-specific parameter values
    """
    result = content
    
    # S3 URI scheme
    if 's3://' in result:
        count = result.count('s3://')
        result = result.replace('s3://', 'gs://')
        logger.debug(f"  Replaced s3:// → gs:// ({count} occurrences)")
    
    # S3 endpoint references
    result = result.replace('s3.amazonaws.com', 'storage.googleapis.com')
    
    # Redshift JDBC type references
    result = result.replace('value="REDSHIFT"', 'value="BIGQUERY"')
    result = result.replace('value="Redshift"', 'value="BigQuery"')
    result = result.replace('value="redshift"', 'value="bigquery"')
    
    # Redshift JDBC log file
    result = result.replace('/app/redshift-jdbc.log', '/app/bigquery-jdbc.log')
    
    return result


def inject_new_gcp_context_params(content: str, logger: logging.Logger) -> str:
    """
    Add new GCP-specific context parameters that don't exist yet.
    Injects them into each <context> block.
    """
    result = content
    
    for param_name, param_info in NEW_GCP_CONTEXT_PARAMS.items():
        # Check if this parameter already exists (from renaming)
        if f'name="{param_name}"' in result:
            logger.debug(f"  GCP context param '{param_name}' already exists (from rename), skipping injection")
            continue
        
        # Find each </context> tag and insert the new param before it
        new_param = (
            f'    <contextParameter comment="{param_info["comment"]}" '
            f'internalId="_gcp_{param_name}" '
            f'name="{param_name}" '
            f'prompt="{param_name}?" '
            f'promptNeeded="false" '
            f'repositoryContextId="built-in" '
            f'type="{param_info["type"]}" '
            f'value="{param_info["value"]}"/>\r\n'
        )
        
        result = result.replace('  </context>', new_param + '  </context>')
        logger.debug(f"  Injected new GCP context param: {param_name} = {param_info['value']}")
    
    return result


def extract_gcs_dir(key_expr: str) -> str:
    """
    Remove the filename portion from the end of the Java expression key_expr.
    A Java expression key_expr is composed of string literals (in quotes) and variables/methods.
    E.g. "dir/" + context.var + "/filename.txt"
    We want to find the last slash '/' inside any quoted string and truncate the expression there.
    """
    # decode XML entities first to make sure we parse properly (e.g. &quot; -> ")
    expr = key_expr.replace('&quot;', '"').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    
    parts = []
    current = ""
    in_quotes = False
    quote_char = None
    
    i = 0
    while i < len(expr):
        char = expr[i]
        if char == '"' or char == "'":
            if not in_quotes:
                in_quotes = True
                quote_char = char
                current += char
            elif char == quote_char:
                in_quotes = False
                current += char
                parts.append((current, True))
                current = ""
            else:
                current += char
        else:
            current += char
            if not in_quotes:
                parts.append((current, False))
                current = ""
        i += 1
    if current:
        parts.append((current, in_quotes))
        
    last_slash_part_idx = -1
    last_slash_char_idx = -1
    for idx in range(len(parts) - 1, -1, -1):
        text, is_literal = parts[idx]
        if is_literal:
            content = text[1:-1]
            slash_idx = content.rfind('/')
            if slash_idx != -1:
                last_slash_part_idx = idx
                last_slash_char_idx = slash_idx + 1
                break
                
    if last_slash_part_idx != -1:
        new_parts = parts[:last_slash_part_idx]
        target_text, is_literal = parts[last_slash_part_idx]
        truncated_literal = target_text[:last_slash_char_idx] + target_text[0]
        
        if truncated_literal != '""' and truncated_literal != "''":
            new_parts.append((truncated_literal, True))
            
        rebuild = "".join(p[0] for p in new_parts)
        rebuild = rebuild.strip()
        rebuild = re.sub(r'\s*\+\s*$', '', rebuild)
        rebuild = re.sub(r'^\s*\+\s*', '', rebuild)
        
        # re-encode XML entities
        rebuild = rebuild.replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;').replace('>', '&gt;')
        return rebuild
        
    return key_expr


def update_component_parameters(content: str, logger: logging.Logger) -> str:
    """
    Update parameters for specific GCP components that require different settings
    than their AWS counterparts.
    """
    result = content
    
    # 0. Inherit tGSConnection for all GCS components if connection exists in job
    gs_conn_names = []
    gs_conn_pattern = re.compile(r'<node\s+componentName="tGSConnection"[^>]*>(.*?)</node>', re.DOTALL)
    for m in gs_conn_pattern.finditer(result):
        u_match = re.search(r'name="UNIQUE_NAME"[^>]*value="([^"]+)"', m.group(1))
        if u_match:
            gs_conn_names.append(u_match.group(1))
            
    if gs_conn_names:
        gs_conn_name = gs_conn_names[0]
        gcs_comp_pattern = re.compile(
            r'(<node\s+componentName="(tGSPut|tGSGet|tGSCopy|tGSList|tGSDelete)"[^>]*>)(.*?)(</node>)',
            re.DOTALL
        )
        def update_gcs_node_connection(match):
            node_start = match.group(1)
            comp_name = match.group(2)
            node_content = match.group(3)
            node_end = match.group(4)
            
            # Check if USE_EXISTING_CONNECTION exists
            use_conn_match = re.search(r'(<elementParameter\s+[^>]*?name="USE_EXISTING_CONNECTION"[^>]*?/?>)', node_content)
            if use_conn_match:
                # Replace value with true
                old_param = use_conn_match.group(1)
                new_param = re.sub(r'\bvalue="[^"]+"', 'value="true"', old_param)
                node_content = node_content.replace(old_param, new_param)
            else:
                # Add USE_EXISTING_CONNECTION parameter
                node_content += '\n    <elementParameter field="CHECK" name="USE_EXISTING_CONNECTION" value="true"/>'
                
            # Check if CONNECTION exists
            conn_match = re.search(r'(<elementParameter\s+[^>]*?name="CONNECTION"[^>]*?/?>)', node_content)
            if conn_match:
                # Replace connection value with gs_conn_name
                old_param = conn_match.group(1)
                new_param = re.sub(r'\bvalue="[^"]*"', f'value="{gs_conn_name}"', old_param)
                node_content = node_content.replace(old_param, new_param)
            else:
                # Add CONNECTION parameter
                node_content += f'\n    <elementParameter field="COMPONENT_LIST" name="CONNECTION" value="{gs_conn_name}"/>'
                
            return node_start + node_content + node_end
            
        result = gcs_comp_pattern.sub(update_gcs_node_connection, result)
        logger.debug(f"  Forced GCS components to use connection: {gs_conn_name}")
    
    # 1. tGSList: Enable "List objects in bucket list"
    gslist_pattern = re.compile(r'(<node\s+componentName="tGSList"[^>]*>)(.*?)(</node>)', re.DOTALL)
    def update_gslist(match):
        node_start = match.group(1)
        node_content = match.group(2)
        node_end = match.group(3)
        if 'name="LIST_IN_BUCKET_LIST"' not in node_content:
            new_param = '\n    <elementParameter field="CHECK" name="LIST_IN_BUCKET_LIST" value="true"/>'
            node_content += new_param
            logger.debug("  Added LIST_IN_BUCKET_LIST=true to tGSList")
        return node_start + node_content + node_end
    result = gslist_pattern.sub(update_gslist, result)
    
    # 2. tGSDelete: Convert BUCKET and KEY to BUCKETS table
    gsdelete_pattern = re.compile(r'(<node\s+componentName="tGSDelete"[^>]*>)(.*?)(</node>)', re.DOTALL)
    def update_gsdelete(match):
        node_start = match.group(1)
        node_content = match.group(2)
        node_end = match.group(3)
        bucket_match = re.search(r'<elementParameter\s+field="TEXT"\s+name="BUCKET"\s+value="([^"]+)"\s*/>', node_content)
        key_match = re.search(r'<elementParameter\s+field="TEXT"\s+name="KEY"\s+value="([^"]+)"\s*/>', node_content)
        
        if bucket_match and key_match:
            bucket_val = bucket_match.group(1)
            key_val = key_match.group(1)
            node_content = node_content.replace(bucket_match.group(0), '')
            node_content = node_content.replace(key_match.group(0), '')
            if 'name="DEL_IN_LIST_BUCKETS"' not in node_content:
                new_params = f'''
    <elementParameter field="CHECK" name="DEL_IN_LIST_BUCKETS" value="true"/>
    <elementParameter field="TABLE" name="BUCKETS">
      <elementValue elementRef="BUCKET_NAME" value="{bucket_val}"/>
      <elementValue elementRef="OBJECT_PREFIX" value="{key_val}"/>
      <elementValue elementRef="OBJECT_DELIMITER" value="&quot;&quot;"/>
    </elementParameter>'''
                node_content += new_params
                logger.debug("  Updated tGSDelete parameters (DEL_IN_LIST_BUCKETS and BUCKETS table)")
        return node_start + node_content + node_end
    result = gsdelete_pattern.sub(update_gsdelete, result)
    
    # 3. tGSConnection: Set AUTH_TYPE to APPLICATION_DEFAULT_CREDENTIALS
    gsconn_pattern = re.compile(r'(<node\s+componentName="tGSConnection"[^>]*>)(.*?)(</node>)', re.DOTALL)
    def update_gsconn(match):
        node_start = match.group(1)
        node_content = match.group(2)
        node_end = match.group(3)
        auth_type_pattern = re.compile(r'<elementParameter\s+field="CLOSED_LIST"\s+name="AUTH_TYPE"\s+value="[^"]+"\s*/>')
        new_auth_param = '\n    <elementParameter field="CLOSED_LIST" name="AUTH_TYPE" value="APPLICATION_DEFAULT_CREDENTIALS"/>'
        if auth_type_pattern.search(node_content):
            node_content = auth_type_pattern.sub(new_auth_param.strip(), node_content)
        else:
            node_content += new_auth_param
        logger.debug("  Set tGSConnection AUTH_TYPE to APPLICATION_DEFAULT_CREDENTIALS")
        return node_start + node_content + node_end
    result = gsconn_pattern.sub(update_gsconn, result)
    
    # 4. tGSPut: Map FILE -> LOCALDIR, KEY -> REMOTEDIR (with stripped filename)
    gsput_pattern = re.compile(r'(<node\s+componentName="tGSPut"[^>]*>)(.*?)(</node>)', re.DOTALL)
    def update_gsput(match):
        node_start = match.group(1)
        node_content = match.group(2)
        node_end = match.group(3)
        file_match = re.search(r'<elementParameter\s+field="[^"]+"\s+name="FILE"\s+value="([^"]+)"\s*/>', node_content)
        key_match = re.search(r'<elementParameter\s+field="[^"]+"\s+name="KEY"\s+value="([^"]+)"\s*/>', node_content)
        if file_match:
            file_val = file_match.group(1)
            node_content = node_content.replace(file_match.group(0), '')
            if 'name="LOCALDIR"' not in node_content:
                node_content += f'\n    <elementParameter field="DIRECTORY" name="LOCALDIR" value="{file_val}"/>'
        if key_match:
            key_val = key_match.group(1)
            node_content = node_content.replace(key_match.group(0), '')
            dir_val = extract_gcs_dir(key_val)
            if 'name="REMOTEDIR"' not in node_content:
                node_content += f'\n    <elementParameter field="TEXT" name="REMOTEDIR" value="{dir_val}"/>'
        return node_start + node_content + node_end
    result = gsput_pattern.sub(update_gsput, result)
    
    # 5. tGSGet: Set USE_KEYS_LIST=true and map BUCKET & KEY to BUCKET_NAME & KEY in KEYS table
    gsget_pattern = re.compile(r'(<node\s+componentName="tGSGet"[^>]*>)(.*?)(</node>)', re.DOTALL)
    def update_gsget(match):
        node_start = match.group(1)
        node_content = match.group(2)
        node_end = match.group(3)
        
        # Extract matches
        bucket_match = re.search(r'<elementParameter\s+field="[^"]+"\s+name="BUCKET"\s+value="([^"]+)"\s*/>', node_content)
        key_match = re.search(r'<elementParameter\s+field="[^"]+"\s+name="KEY"\s+value="([^"]+)"\s*/>', node_content)
        file_match = re.search(r'<elementParameter\s+field="[^"]+"\s+name="FILE"\s+value="([^"]+)"\s*/>', node_content)
        dir_match = re.search(r'<elementParameter\s+field="[^"]+"\s+name="DIRECTORY"\s+value="([^"]+)"\s*/>', node_content)
        
        bucket_val = bucket_match.group(1) if bucket_match else None
        key_val = key_match.group(1) if key_match else None
        
        # Remove old BUCKET, KEY, and OBJECTS_PREFIX if they exist
        if bucket_match:
            node_content = node_content.replace(bucket_match.group(0), '')
        if key_match:
            node_content = node_content.replace(key_match.group(0), '')
            
        prefix_match = re.search(r'<elementParameter\s+field="[^"]+"\s+name="OBJECTS_PREFIX"\s+value="[^"]+"\s*/>', node_content)
        if prefix_match:
            node_content = node_content.replace(prefix_match.group(0), '')
            
        # Add USE_KEYS_LIST and KEYS table if BUCKET and KEY are specified
        if bucket_val and key_val:
            if 'name="USE_KEYS_LIST"' not in node_content:
                new_params = f'''
    <elementParameter field="CHECK" name="USE_KEYS_LIST" value="true"/>
    <elementParameter field="TABLE" name="KEYS">
      <elementValue elementRef="BUCKET_NAME" value="{bucket_val}"/>
      <elementValue elementRef="KEY" value="{key_val}"/>
      <elementValue elementRef="NEW_NAME" value="&quot;&quot;"/>
    </elementParameter>'''
                node_content += new_params
                logger.debug(f"  Added USE_KEYS_LIST=true and KEYS table to tGSGet (bucket: {bucket_val}, key: {key_val})")
                
        # Map FILE or DIRECTORY to DIRECTORY
        out_dir = None
        if file_match:
            out_dir = file_match.group(1)
            node_content = node_content.replace(file_match.group(0), '')
        elif dir_match:
            out_dir = dir_match.group(1)
            node_content = node_content.replace(dir_match.group(0), '')
            
        if out_dir and 'name="DIRECTORY"' not in node_content:
            node_content += f'\n    <elementParameter field="DIRECTORY" name="DIRECTORY" value="{out_dir}"/>'
            logger.debug(f"  Mapped tGSGet output location to DIRECTORY: {out_dir}")
            
        return node_start + node_content + node_end
    result = gsget_pattern.sub(update_gsget, result)
    
    # 6. BigQuery components: Auth and Project ID
    bq_pattern = re.compile(r'(<node\s+componentName="tBigQuery[^"]*"[^>]*>)(.*?)(</node>)', re.DOTALL)
    def update_bq(match):
        node_start = match.group(1)
        node_content = match.group(2)
        node_end = match.group(3)
        
        # Disable USE_EXISTING_CONNECTION
        use_conn_pattern = re.compile(r'<elementParameter\s+field="CHECK"\s+name="USE_EXISTING_CONNECTION"\s+value="[^"]+"\s*/?>')
        new_use_conn = '<elementParameter field="CHECK" name="USE_EXISTING_CONNECTION" value="false"/>'
        if use_conn_pattern.search(node_content):
            node_content = use_conn_pattern.sub(new_use_conn, node_content)
        else:
            node_content += '\n    ' + new_use_conn
            
        # Remove CONNECTION parameter reference to deleted components
        conn_pattern = re.compile(r'<elementParameter\s+[^>]*?name="CONNECTION"[^>]*?/>\s*')
        if conn_pattern.search(node_content):
            node_content = conn_pattern.sub('', node_content)

        auth_pattern = re.compile(r'<elementParameter\s+field="CLOSED_LIST"\s+name="AUTH_MODE"\s+value="[^"]+"\s*/>')
        new_auth = '\n    <elementParameter field="CLOSED_LIST" name="AUTH_MODE" value="APPLICATION_DEFAULT_CREDENTIALS"/>'
        if auth_pattern.search(node_content):
            node_content = auth_pattern.sub(new_auth, node_content)
        else:
            node_content += new_auth
            
        project_pattern = re.compile(r'<elementParameter\s+field="TEXT"\s+name="PROJECT_ID"\s+value="[^"]*"\s*/>')
        new_proj = '\n    <elementParameter field="TEXT" name="PROJECT_ID" value="context.gcp_compute_project"/>'
        if project_pattern.search(node_content):
            node_content = project_pattern.sub(new_proj, node_content)
        else:
            node_content += new_proj
            
        return node_start + node_content + node_end
    result = bq_pattern.sub(update_bq, result)

    # 7. tGSCopy: Map parameters from S3Copy mapping
    gscopy_pattern = re.compile(r'(<node\s+componentName="tGSCopy"[^>]*>)(.*?)(</node>)', re.DOTALL)
    def update_gscopy(match):
        node_start = match.group(1)
        node_content = match.group(2)
        node_end = match.group(3)
        
        from_bucket_m = re.search(r'<elementParameter\s+field="[^"]+"\s+name="FROM_BUCKET"\s+value="([^"]+)"\s*/>', node_content)
        from_key_m = re.search(r'<elementParameter\s+field="[^"]+"\s+name="FROM_KEY"\s+value="([^"]+)"\s*/>', node_content)
        to_bucket_m = re.search(r'<elementParameter\s+field="[^"]+"\s+name="TO_BUCKET"\s+value="([^"]+)"\s*/>', node_content)
        to_key_m = re.search(r'<elementParameter\s+field="[^"]+"\s+name="TO_KEY"\s+value="([^"]+)"\s*/>', node_content)
        
        if from_bucket_m:
            val = from_bucket_m.group(1)
            node_content = node_content.replace(from_bucket_m.group(0), '')
            if 'name="SOURCE_BUCKET"' not in node_content:
                node_content += f'\n    <elementParameter field="TEXT" name="SOURCE_BUCKET" value="{val}"/>'
        if from_key_m:
            val = from_key_m.group(1)
            node_content = node_content.replace(from_key_m.group(0), '')
            if 'name="SOURCE_OBJECTKEY"' not in node_content:
                node_content += f'\n    <elementParameter field="TEXT" name="SOURCE_OBJECTKEY" value="{val}"/>'
        if to_bucket_m:
            val = to_bucket_m.group(1)
            node_content = node_content.replace(to_bucket_m.group(0), '')
            if 'name="TARGET_BUCKET"' not in node_content:
                node_content += f'\n    <elementParameter field="TEXT" name="TARGET_BUCKET" value="{val}"/>'
        if to_key_m:
            val = to_key_m.group(1)
            node_content = node_content.replace(to_key_m.group(0), '')
            if 'name="TARGET_FOLDER"' not in node_content:
                node_content += f'\n    <elementParameter field="TEXT" name="TARGET_FOLDER" value="{val}"/>'
                
        return node_start + node_content + node_end
    result = gscopy_pattern.sub(update_gscopy, result)

    return result


def process_item_file(item_path: str, logger: logging.Logger, dry_run: bool = False, translated_queries: dict = None) -> dict:
    """
    Process a single Talend .item file: apply all repointing transformations.
    
    Args:
        item_path: Full path to the .item file
        logger: Logger instance
        dry_run: If True, only report changes without modifying files
        translated_queries: Dictionary of pre-translated queries
    
    Returns:
        Dict with processing statistics
    """
    stats = {
        'file': os.path.basename(item_path),
        'components_replaced': 0,
        'components_removed': 0,
        'context_vars_replaced': 0,
        'sql_converted': False,
        'labels_updated': 0,
        'manual_review_flags': [],
        'api_translations': 0,
        'local_conversions': 0,
    }
    
    logger.info(f"\n  Processing: {os.path.basename(item_path)}")
    
    # Read the file
    with open(item_path, 'r', encoding='utf-8') as f:
        original_content = f.read()
    
    content = original_content
    
    # 1. Build unique name map before making changes
    name_map = build_unique_name_map(content)
    
    # 2. Replace component names (and remove components mapped to None)
    content, removed_names = replace_component_names(content, logger)
    stats['components_removed'] = len(removed_names)
    
    # NEW: Update specific parameters for GCP components
    content = update_component_parameters(content, logger)
    
    # Count component replacements
    for old_comp, new_comp in COMPONENT_REPLACEMENTS.items():
        if new_comp is not None and f'componentName="{new_comp}"' in content:
            stats['components_replaced'] += 1
    
    # 3. Replace UNIQUE_NAME references
    content = replace_unique_names(content, name_map, logger)
    
    # 4. Replace labels
    content = replace_labels(content, logger)
    
    # 5. Convert SQL (Redshift → BigQuery) first, so that restored context variables are present
    job_name = os.path.basename(item_path).replace('.item', '')
    old_content = content
    content, sql_stats = convert_sql_in_content(content, logger, job_name=job_name, translated_queries=translated_queries, name_map=name_map)
    stats['sql_converted'] = (content != old_content)
    stats['api_translations'] = sql_stats['api_translations']
    stats['local_conversions'] = sql_stats['local_conversions']
    
    # 6. Replace context variables throughout (including the newly inserted SQL queries)
    content = replace_context_variables(content, logger)
    
    # 7. Replace generic AWS references
    content = replace_generic_aws_references(content, logger)
    
    # 7. Inject new GCP context parameters
    # The user specifically requested not to add new contexts or rename job-level contexts
    # content = inject_new_gcp_context_params(content, logger)
    
    # 8. Check for manual review flags in SQL content
    sql_sections = re.findall(r'name="QUERY"\s+value="([^"]*?)"', content, re.DOTALL)
    for sql in sql_sections:
        flags = get_manual_review_flags(sql)
        stats['manual_review_flags'].extend(flags)
    
    # Write the modified content
    if not dry_run:
        if content != original_content:
            with open(item_path, 'w', encoding='utf-8') as f:
                f.write(content)
            logger.info(f"  ✅ Saved repointed file")
        else:
            logger.info(f"  ℹ️  No changes needed")
    else:
        if content != original_content:
            logger.info(f"  [DRY RUN] Would modify this file")
        else:
            logger.info(f"  [DRY RUN] No changes needed")
    
    return stats


def process_job_folder(folder_path: str, logger: logging.Logger, dry_run: bool = False, translated_queries: dict = None) -> dict:
    """
    Process a single job group folder:
    1. Create backup with _backup suffix
    2. Create new folder with repointed child jobs
    
    Args:
        folder_path: Full path to the job group folder
        logger: Logger instance
        dry_run: If True, only report without making changes
        translated_queries: Dictionary of pre-translated queries
    
    Returns:
        Dict with processing statistics
    """
    folder_name = os.path.basename(folder_path)
    parent_dir = os.path.dirname(folder_path)
    backup_path = os.path.join(parent_dir, f"{folder_name}_backup")
    
    logger.info(f"\n{'='*70}")
    logger.info(f"Processing folder: {folder_name}")
    logger.info(f"{'='*70}")
    
    # Find child job items
    child_items_relative = []
    for root, dirs, files in os.walk(folder_path):
        for f in files:
            if f.endswith('.item') and is_child_job(f):
                rel_path = os.path.relpath(os.path.join(root, f), folder_path)
                child_items_relative.append(rel_path)
    
    if not child_items_relative:
        logger.info(f"  No child job .item files found in {folder_name}. Skipping.")
        return {'folder': folder_name, 'child_jobs': 0, 'stats': []}
    
    logger.info(f"  Found {len(child_items_relative)} child job(s) to repoint:")
    for ci in child_items_relative:
        logger.info(f"    → {ci}")
    
    if dry_run:
        all_stats = []
        for rel_path in child_items_relative:
            item_path = os.path.join(folder_path, rel_path)
            stats = process_item_file(item_path, logger, dry_run=True, translated_queries=translated_queries)
            all_stats.append(stats)
        return {'folder': folder_name, 'child_jobs': len(child_items_relative), 'stats': all_stats}

    logger.info(f"\n  Repointing child jobs in {folder_name}")
    all_stats = []
    for rel_path in child_items_relative:
        item_path = os.path.join(folder_path, rel_path)
        stats = process_item_file(item_path, logger, translated_queries=translated_queries)
        all_stats.append(stats)
    
    return {'folder': folder_name, 'child_jobs': len(child_items_relative), 'stats': all_stats}


def main():
    """Main entry point for the Talend Repointing Utility."""
    parser = argparse.ArgumentParser(
        description='Talend AWS → GCP Repointing Utility',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python talend_repoint.py "D:\\path\\to\\Jobs"
  python talend_repoint.py "D:\\path\\to\\Jobs" --dry-run
  python talend_repoint.py "D:\\path\\to\\Jobs" --specific-folders CUST360_KIOSK CUST360_CCP_LOADS
        """
    )
    
    parser.add_argument(
        'jobs_folder',
        help='Path to the Talend Jobs folder containing job group subfolders'
    )
    
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Preview changes without modifying any files'
    )
    
    parser.add_argument(
        '--specific-folders',
        nargs='+',
        help='Only process specific job group folders (space-separated names)'
    )
    
    args = parser.parse_args()
    
    # Setup
    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(script_dir, 'logs')
    logger = setup_logging(log_dir)
    
    jobs_folder = os.path.abspath(args.jobs_folder)
    display_folder = jobs_folder
    
    # Fix for Windows MAX_PATH (260 character limit)
    if os.name == 'nt' and not jobs_folder.startswith('\\\\?\\'):
        jobs_folder = '\\\\?\\' + jobs_folder
    
    # Validate input
    if not os.path.isdir(jobs_folder):
        logger.error(f"❌ Jobs folder not found: {display_folder}")
        sys.exit(1)
    
    logger.info("=" * 70)
    logger.info("  TALEND AWS → GCP REPOINTING UTILITY")
    logger.info("=" * 70)
    logger.info(f"  Jobs folder:  {display_folder}")
    logger.info(f"  Mode:         {'DRY RUN (no changes)' if args.dry_run else 'LIVE (will modify files)'}")
    logger.info(f"  Started at:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("")
    
    if not args.dry_run:
        logger.info("⚠️  WARNING: This will modify files. Original folders will be backed up with _backup suffix.")
        logger.info("   Press Ctrl+C within 3 seconds to abort...")
        try:
            import time
            time.sleep(3)
        except KeyboardInterrupt:
            logger.info("\n❌ Aborted by user.")
            sys.exit(0)
    
    # Handle Top-level Backup First
    parent_dir = os.path.dirname(jobs_folder)
    base_name = os.path.basename(jobs_folder)
    backup_path = os.path.join(parent_dir, f"{base_name}_backup")
    
    if not args.dry_run:
        # Create top-level backup
        if os.path.exists(backup_path):
            logger.info(f"  ℹ️  Backup folder already exists: {os.path.basename(backup_path)}")
            logger.info(f"  Using existing backup as source and restoring Jobs folder to original state first.")
            
            # Mirror backup back to Jobs (restoring original content)
            import subprocess
            logger.info(f"\n  Step 1: Restoring original files from backup")
            subprocess.run(
                ['robocopy', backup_path.replace('\\\\?\\', ''), jobs_folder.replace('\\\\?\\', ''), '/MIR', '/NFL', '/NDL', '/NJH', '/NJS', '/NC', '/NS', '/NP', '/R:1', '/W:1'],
                capture_output=True, text=True
            )
            logger.info(f"  ✅ Restored original files from backup")
        else:
            logger.info(f"\n  Step 1: Creating top-level backup")
            logger.info(f"    Renaming: {base_name} → {os.path.basename(backup_path)}")
            os.rename(jobs_folder, backup_path)
            
            logger.info(f"\n  Step 2: Creating new working folder")
            logger.info(f"    Copying: {os.path.basename(backup_path)} → {base_name}")
            # Use robocopy on Windows for long path support
            import subprocess
            subprocess.run(
                ['robocopy', backup_path.replace('\\\\?\\', ''), jobs_folder.replace('\\\\?\\', ''), '/MIR', '/NFL', '/NDL', '/NJH', '/NJS', '/NC', '/NS', '/NP', '/R:1', '/W:1'],
                capture_output=True, text=True
            )
            logger.info(f"  ✅ Working copy created")
    else:
        logger.info(f"\n  [DRY RUN] Would backup {jobs_folder} to {backup_path}")
        logger.info(f"  [DRY RUN] Would process files inside {jobs_folder} in-place after backup")

    # Find folders to process
    all_folders = []
    for entry in os.listdir(jobs_folder):
        full_path = os.path.join(jobs_folder, entry)
        if os.path.isdir(full_path) and not entry.endswith('_backup'):
            if args.specific_folders:
                if entry in args.specific_folders:
                    all_folders.append(full_path)
            else:
                all_folders.append(full_path)
    
    if not all_folders:
        logger.info("No job group folders found to process.")
        sys.exit(0)
    
    logger.info(f"Found {len(all_folders)} folder(s) to process:")
    for f in all_folders:
        logger.info(f"  📁 {os.path.basename(f)}")
    
    # GCP Batch SQL Translation pre-pass
    translated_queries = {}
    api_failed = False  # Track if API failed
    if USE_BQ_BATCH_API:
        logger.info("\n" + "=" * 70)
        logger.info("  STEP 3: EXTRACTING AND TRANSLATING ALL SQL QUERIES IN BATCH")
        logger.info("=" * 70)

        queries_to_translate = {}
        for folder_path in sorted(all_folders):
            # Find child job items
            child_items = []
            for root, dirs, files in os.walk(folder_path):
                for f in files:
                    if f.endswith('.item') and is_child_job(f):
                        child_items.append(os.path.join(root, f))

            for item_path in child_items:
                job_name = os.path.basename(item_path).replace('.item', '')
                extracted = extract_queries_from_item(item_path, logger)
                for comp_name, sql in extracted.items():
                    if is_actual_sql(sql):
                        queries_to_translate[(job_name, comp_name)] = sql
                    else:
                        logger.debug(f"  Skipping API translation for non-SQL Java expression in {job_name}.{comp_name}")

        if queries_to_translate:
            try:
                translated_queries = run_batch_translation(queries_to_translate, logger)
                logger.info(f"  Successfully pre-translated {len(translated_queries)} SQL query/queries.")
            except Exception as e:
                logger.error(f"  ❌ Batch SQL Translation failed: {e}")
                logger.info("  Falling back to local SQL conversion rules.")
                api_failed = True  # Mark API as failed
        else:
            logger.info("  No SQL queries found to translate.")

    # Track SQL conversion statistics
    total_queries_sent_to_api = len(queries_to_translate) if USE_BQ_BATCH_API and queries_to_translate else 0
    total_api_success = len(translated_queries)

    # Process each folder
    all_results = []
    total_child_jobs = 0
    total_components_replaced = 0
    total_components_removed = 0
    total_sql_converted = 0
    total_manual_flags = []
    total_api_translations_used = 0
    total_local_conversions = 0

    for folder_path in sorted(all_folders):
        result = process_job_folder(folder_path, logger, dry_run=args.dry_run, translated_queries=translated_queries)
        all_results.append(result)

        total_child_jobs += result['child_jobs']
        for s in result['stats']:
            total_components_replaced += s['components_replaced']
            total_components_removed += s['components_removed']
            if s['sql_converted']:
                total_sql_converted += 1
            total_manual_flags.extend(s['manual_review_flags'])
            total_api_translations_used += s.get('api_translations', 0)
            total_local_conversions += s.get('local_conversions', 0)
    
    # Calculate API conversion rate
    api_conversion_rate = (total_api_success / total_queries_sent_to_api * 100) if total_queries_sent_to_api > 0 else 0

    # Print summary
    logger.info(f"\n{'='*70}")
    logger.info("  REPOINTING SUMMARY")
    logger.info(f"{'='*70}")
    logger.info(f"  Folders processed:       {len(all_results)}")
    logger.info(f"  Child jobs repointed:    {total_child_jobs}")
    logger.info(f"  Components replaced:     {total_components_replaced}")
    logger.info(f"  Components removed:      {total_components_removed}")
    logger.info(f"  SQL queries converted:   {total_sql_converted}")

    # SQL Conversion Breakdown
    if total_queries_sent_to_api > 0:
        logger.info(f"\n  SQL CONVERSION BREAKDOWN:")
        logger.info(f"  ├─ Queries sent to GCP API:     {total_queries_sent_to_api}")
        logger.info(f"  ├─ API success (pre-translated): {total_api_success}")
        logger.info(f"  ├─ API parser errors:            {total_queries_sent_to_api - total_api_success}")
        logger.info(f"  ├─ API conversion rate:          {api_conversion_rate:.1f}%")
        logger.info(f"  ├─ API translations used:        {total_api_translations_used}")
        logger.info(f"  └─ Local rule conversions:       {total_local_conversions}")

    # Show prominent warning if API failed
    if api_failed and total_queries_sent_to_api > 0:
        logger.error(f"\n  {'='*70}")
        logger.error(f"  ⚠️  🔴 GCP BATCH SQL TRANSLATION API FAILED!")
        logger.error(f"  {'='*70}")
        logger.error(f"  All {total_local_conversions} SQL queries were converted using LOCAL FALLBACK rules.")
        logger.error(f"  SQL-Level Accuracy: ~70% (Fallback) instead of ~100% (API)")
        logger.error(f"  ")
        logger.error(f"  Possible causes:")
        logger.error(f"    • Network connectivity issues")
        logger.error(f"    • SSL/TLS certificate problems")
        logger.error(f"    • GCP authentication errors")
        logger.error(f"    • Proxy/firewall blocking GCS access")
        logger.error(f"  ")
        logger.error(f"  To fix: Check network, verify GCP credentials, or disable API in config.py")
        logger.error(f"  {'='*70}")

    if total_manual_flags:
        logger.info(f"\n  ⚠️  MANUAL REVIEW REQUIRED ({len(total_manual_flags)} items):")
        for flag in total_manual_flags:
            logger.info(f"  {flag}")
    
    logger.info(f"\n  Completed at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"{'='*70}")

    # Generate detailed reports for each job
    if not args.dry_run:
        logger.info(f"\n{'='*70}")
        logger.info("  GENERATING DETAILED REPORTS")
        logger.info(f"{'='*70}")

        # Find the most recent log file
        log_files = sorted([f for f in os.listdir(log_dir) if f.endswith('.log')],
                          key=lambda x: os.path.getmtime(os.path.join(log_dir, x)),
                          reverse=True)

        if log_files:
            latest_log = os.path.join(log_dir, log_files[0])
            logger.info(f"  Using log file: {log_files[0]}")

            try:
                # Run report generator with proper encoding
                import subprocess
                result = subprocess.run(
                    [sys.executable, 'generate_reports_from_logs.py', latest_log, display_folder],
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',  # Replace undecodable characters
                    cwd=script_dir
                )

                if result.returncode == 0:
                    # Print report generator output
                    if result.stdout:
                        for line in result.stdout.split('\n'):
                            if line.strip():
                                logger.info(f"  {line}")
                    else:
                        logger.info(f"  Reports generated successfully!")
                else:
                    logger.warning(f"  ⚠️  Report generation had issues:")
                    if result.stderr:
                        for line in result.stderr.split('\n'):
                            if line.strip():
                                logger.warning(f"    {line}")
                    else:
                        logger.warning(f"    Return code: {result.returncode}")
            except Exception as e:
                logger.warning(f"  ⚠️  Could not generate reports: {e}")
                import traceback
                logger.warning(f"    {traceback.format_exc()}")
        else:
            logger.warning(f"  ⚠️  No log file found for report generation")

        logger.info(f"{'='*70}")

    if args.dry_run:
        logger.info("\n💡 This was a DRY RUN. No files were modified.")
        logger.info("   Run without --dry-run to apply changes.")


if __name__ == '__main__':
    main()
