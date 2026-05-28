"""
SQL Converter: Redshift → BigQuery
Converts Redshift-compatible SQL found in Talend .item files to BigQuery-compatible SQL.
Handles encoded XML entities (&amp;#10; for newline, &amp;#13; for CR, &amp;quot; for quotes).
"""

import re


def _tokenize_talend_sql(sql_text: str):
    """Tokenize Talend Java variables out of the SQL string to make it API-friendly."""
    placeholder_map = {}
    quote_char = '&quot;' if '&quot;' in sql_text else '"'
    
    # 1. Extract leading/trailing whitespace around quotes
    leading_ws = ""
    trailing_ws = ""
    
    ws_match_leading = re.match(r'^(\s+)', sql_text)
    if ws_match_leading:
        leading_ws = ws_match_leading.group(1)
        inner_sql = sql_text[len(leading_ws):]
    else:
        inner_sql = sql_text
        
    ws_match_trailing = re.search(r'(\s+)$', inner_sql)
    if ws_match_trailing:
        trailing_ws = ws_match_trailing.group(1)
        inner_sql = inner_sql[:-len(trailing_ws)]
    else:
        inner_sql = inner_sql
        
    # 2. Check for quotes
    has_leading_xml_quote = inner_sql.startswith('&quot;')
    has_trailing_xml_quote = inner_sql.endswith('&quot;')
    has_leading_raw_quote = not has_leading_xml_quote and inner_sql.startswith('"')
    has_trailing_raw_quote = not has_trailing_xml_quote and inner_sql.endswith('"')
    
    if has_leading_xml_quote:
        inner_sql = inner_sql[6:]
    elif has_leading_raw_quote:
        inner_sql = inner_sql[1:]
        
    if has_trailing_xml_quote:
        inner_sql = inner_sql[:-6]
    elif has_trailing_raw_quote:
        inner_sql = inner_sql[:-1]
        
    # 3. Re-extract any new leading/trailing whitespace inside quotes
    inner_leading_ws = ""
    inner_trailing_ws = ""
    
    ws_match_inner_leading = re.match(r'^(\s+)', inner_sql)
    if ws_match_inner_leading:
        inner_leading_ws = ws_match_inner_leading.group(1)
        inner_sql = inner_sql[len(inner_leading_ws):]
        
    ws_match_inner_trailing = re.search(r'(\s+)$', inner_sql)
    if ws_match_inner_trailing:
        inner_trailing_ws = ws_match_inner_trailing.group(1)
        inner_sql = inner_sql[:-len(inner_trailing_ws)]
        
    # Replace variable sequences in the middle: "+ ... +" or &quot;+ ... +&quot;
    pattern_middle = re.compile(r'((?:&quot;|\")\s*\+\s*)(.*?)(\s*\+\s*(?:&quot;|\"))', re.DOTALL)
    counter = 0
    tokenized_sql = inner_sql
    
    def replace_match(match):
        nonlocal counter
        placeholder = f"talendvar{counter}"
        counter += 1
        placeholder_map[placeholder] = match.group(0)
        return placeholder
        
    tokenized_sql = pattern_middle.sub(replace_match, tokenized_sql)
    
    # Replace variables at the end
    pattern_end = re.compile(r'((?:&quot;|\")\s*\+\s*)(.*?)$', re.DOTALL)
    def replace_end(match):
        nonlocal counter
        placeholder = f"talendvarend{counter}"
        counter += 1
        placeholder_map[placeholder] = match.group(0)
        return placeholder
        
    tokenized_sql = pattern_end.sub(replace_end, tokenized_sql)
    
    # Replace variables at the start
    pattern_start = re.compile(r'^(.*?)(\s*\+\s*(?:&quot;|\"))', re.DOTALL)
    def replace_start(match):
        nonlocal counter
        placeholder = f"talendvarstart{counter}"
        counter += 1
        placeholder_map[placeholder] = match.group(0)
        return placeholder
        
    tokenized_sql = pattern_start.sub(replace_start, tokenized_sql)
    
    return (tokenized_sql, placeholder_map, 
            has_leading_xml_quote, has_trailing_xml_quote, 
            has_leading_raw_quote, has_trailing_raw_quote, quote_char,
            leading_ws, trailing_ws, inner_leading_ws, inner_trailing_ws)


def _detokenize_talend_sql(tokenized_sql: str, placeholder_map: dict, 
                          has_leading_xml_quote, has_trailing_xml_quote, 
                          has_leading_raw_quote, has_trailing_raw_quote, quote_char,
                          leading_ws="", trailing_ws="", inner_leading_ws="", inner_trailing_ws=""):
    """Restore original Talend Java variables and wrap back in outer quotes."""
    # Strip trailing semicolon and whitespace added by BigQuery translation
    tokenized_sql = tokenized_sql.strip()
    if tokenized_sql.endswith(';'):
        tokenized_sql = tokenized_sql[:-1].rstrip()
        
    restored_sql = tokenized_sql
    
    # Check if there are any placeholders
    placeholders = list(placeholder_map.keys())
    if placeholders:
        # Build regex to split by placeholders (case-insensitive)
        pl_regex = re.compile(r'(' + '|'.join(re.escape(p) for p in placeholders) + r')', re.IGNORECASE)
        sql_parts = pl_regex.split(tokenized_sql)
        
        reconstructed_parts = []
        for part in sql_parts:
            lower_part = part.lower()
            matching_placeholder = None
            for p in placeholders:
                if p.lower() == lower_part:
                    matching_placeholder = p
                    break
                    
            if matching_placeholder:
                # Restore original Java part
                reconstructed_parts.append(placeholder_map[matching_placeholder])
            else:
                reconstructed_parts.append(part)
                
        restored_sql = "".join(reconstructed_parts)
        
    # Put back inner whitespace
    restored_sql = f"{inner_leading_ws}{restored_sql}{inner_trailing_ws}"
        
    if has_leading_xml_quote:
        restored_sql = f"&quot;{restored_sql}"
    elif has_leading_raw_quote:
        restored_sql = f'"{restored_sql}'
        
    if has_trailing_xml_quote:
        restored_sql = f"{restored_sql}&quot;"
    elif has_trailing_raw_quote:
        restored_sql = f'{restored_sql}"'
        
    # Put back outer whitespace
    restored_sql = f"{leading_ws}{restored_sql}{trailing_ws}"
            
    return restored_sql


def restore_placeholders(sql: str) -> str:
    """Restore COPY and UNLOAD placeholders to their locally converted BigQuery equivalents."""
    pattern = re.compile(
        r"(?:SELECT\s+1\s*;?\s*)?/\*\s*(COPY|UNLOAD)_PLACEHOLDER:([0-9a-fA-F]+)\s*\*/(?:\s*;?\s*SELECT\s+1\s*;?)?\s*;?",
        re.IGNORECASE | re.DOTALL
    )
    
    def replace_match(match):
        placeholder_type = match.group(1).upper()
        hex_str = match.group(2)
        original_stmt = bytes.fromhex(hex_str).decode('utf-8')
        
        # Run local conversion rules on the statement
        if placeholder_type == "COPY":
            converted = _convert_copy_command(original_stmt)
        else:
            converted = _convert_unload(original_stmt)
            
        return converted
        
    return pattern.sub(replace_match, sql)


_api_warning_shown = False
_api_import_warning_shown = False
_api_disabled = False


def _translate_via_gcp_api(sql_text: str) -> str:
    """Translates SQL using BQ SQL Translation API if enabled, otherwise returns None."""
    global _api_warning_shown, _api_import_warning_shown, _api_disabled
    
    # If the API has already failed once, bypass immediately to avoid timeout delays
    if _api_disabled:
        return None
        
    try:
        from config import USE_BQ_TRANSLATION_API, GCP_TRANSLATION_PROJECT_ID, GCP_TRANSLATION_LOCATION
        if not USE_BQ_TRANSLATION_API or not GCP_TRANSLATION_PROJECT_ID:
            _api_disabled = True
            return None
            
        # Import dynamically to avoid crash if dependency is missing (v2alpha contains SqlTranslationServiceClient)
        from google.cloud.bigquery_migration_v2alpha import SqlTranslationServiceClient
        
        res = _tokenize_talend_sql(sql_text)
        tokenized_sql = res[0]
        p_map = res[1]
        
        client = SqlTranslationServiceClient()
        parent = f"projects/{GCP_TRANSLATION_PROJECT_ID}/locations/{GCP_TRANSLATION_LOCATION}"
        
        request = {
            "parent": parent,
            "source_dialect": "REDSHIFT",
            "query": tokenized_sql,
        }
        
        response = client.translate_query(request=request)
        translated_query = response.translated_query
        
        if not translated_query:
            return None
            
        return _detokenize_talend_sql(translated_query, *res[1:])
        
    except ImportError:
        _api_disabled = True  # Permanently disable for this execution run
        if not _api_import_warning_shown:
            import sys
            print("⚠️ Warning: google-cloud-bigquery-migration package is not installed or lacks v2alpha. Falling back to local rules.", file=sys.stderr)
            _api_import_warning_shown = True
        return None
    except Exception as e:
        _api_disabled = True  # Permanently disable for this execution run
        if not _api_warning_shown:
            import sys
            print(f"⚠️ Warning: BigQuery SQL Translation API failed ({e}). Falling back to local rules.", file=sys.stderr)
            _api_warning_shown = True
        return None


def lowercase_table_expression(expr: str) -> str:
    """Split table name expression by Java concatenations and lowercase only SQL literal parts."""
    # First check if there's a table alias at the end (1-5 uppercase letters followed by space or end)
    alias_pattern = re.compile(r'\s+([A-Z]{1,5})$')
    alias_match = alias_pattern.search(expr)
    alias = ''
    expr_without_alias = expr

    if alias_match:
        alias = alias_match.group(0)  # Include the space
        expr_without_alias = expr[:alias_match.start()]

    # Split by Java concatenations (e.g., "+ context.var +")
    pattern = re.compile(r'((?:&quot;|"|\')\s*\+\s*.*?\s*\+\s*(?:&quot;|"|\'))', re.DOTALL)
    parts = pattern.split(expr_without_alias)

    lowered_parts = []
    for part in parts:
        part_stripped = part.strip()
        # Check if this is a Java expression (quoted string with + inside)
        is_java = (
            (part_stripped.startswith('"') and part_stripped.endswith('"')) or
            (part_stripped.startswith("'") and part_stripped.endswith("'")) or
            (part_stripped.startswith('&quot;') and part_stripped.endswith('&quot;'))
        ) and '+' in part_stripped

        if is_java:
            # Keep Java expressions as-is (don't lowercase context variables)
            lowered_parts.append(part)
        else:
            # Lowercase SQL literal parts (table names, etc.)
            lowered_parts.append(part.lower())

    # Add back the alias (keep it uppercase for readability)
    return "".join(lowered_parts) + alias


def lowercase_table_names_in_sql(sql: str) -> str:
    """Find and lowercase all table names referenced in SQL statements."""
    keywords = [
        r'\bFROM', r'\bJOIN', r'\bINTO', r'\bUPDATE', r'\bDELETE\s+FROM',
        r'\bDELETE', r'\bUSING', r'\bTABLE(?:\s+IF\s+NOT\s+EXISTS|\s+IF\s+EXISTS)?',
        r'\bMERGE\s+INTO', r'\bMERGE'
    ]

    # Process each keyword separately to handle table expressions properly
    result = sql
    for keyword_pattern in keywords:
        # Match the keyword, then capture everything up to a SQL keyword, newline, or end
        # Note: Don't include semicolon (;) in lookahead - it matches XML entities like &quot;
        pattern = re.compile(
            rf"({keyword_pattern})\s+(.+?)(?=\s+\b(?:WHERE|ON|JOIN|INNER|LEFT|RIGHT|AND|OR|GROUP|ORDER|LIMIT|HAVING|UNION|SET|VALUES)\b|\r|\n|$)",
            re.IGNORECASE | re.DOTALL
        )

        def replace_table(match):
            keyword = match.group(1)
            table_expr = match.group(2).strip()
            lowered_expr = lowercase_table_expression(table_expr)
            return f"{keyword} {lowered_expr}"

        result = pattern.sub(replace_table, result)

    return result


def convert_redshift_to_bigquery(sql_text: str) -> str:
    """
    Convert Redshift SQL to BigQuery SQL.
    The input may contain XML-encoded entities since it comes from Talend .item files.

    Args:
        sql_text: Raw SQL string (may contain XML entities like &quot; &#13; &#10;)

    Returns:
        Converted BigQuery-compatible SQL string
    """
    if not sql_text:
        return sql_text

    # Try BQ SQL Translation API first (if enabled and set up)
    api_result = _translate_via_gcp_api(sql_text)
    if api_result is not None:
        # Schema injection and post-processing should still be run on the API output
        result = _inject_gcp_project_in_schemas(api_result)
        result = result.replace('s3://', 'gs://')
        result = re.sub(r'\\&quot;(&quot;\s*\+\s*context\.gcp_[a-z_]+_project)', r'\1', result)
        result = re.sub(r'(context\.gcp_bq_[a-z_]+_dataset\s*\+\s*&quot;)\\&quot;', r'\1', result)
        result = lowercase_table_names_in_sql(result)
        return result
    else:
        result = sql_text

    # =========================================================================
    # 1. Function replacements (case-insensitive)
    # =========================================================================
    
    # GETDATE() → CURRENT_TIMESTAMP() (for audit timestamps - UTC timezone)
    result = re.sub(r'\bGETDATE\s*\(\s*\)', 'CURRENT_TIMESTAMP()', result, flags=re.IGNORECASE)

    # SYSDATE → CURRENT_TIMESTAMP() (for audit timestamps - UTC timezone)
    result = re.sub(r'\bSYSDATE\b', 'CURRENT_TIMESTAMP()', result, flags=re.IGNORECASE)

    # CURRENT_DATE (without parens in Redshift) → CURRENT_DATE() in BQ
    # But be careful not to double-parenthesize if already has ()
    result = re.sub(r'\bCURRENT_DATE\b(?!\s*\()', 'CURRENT_DATE()', result, flags=re.IGNORECASE)

    # trunc(expression) → DATE(expression) for date truncation
    result = re.sub(r'\btrunc\s*\(', 'DATE(', result, flags=re.IGNORECASE)

    # dateadd(day, -N, date_expr) → DATE_SUB(date_expr, INTERVAL N DAY)
    # dateadd(day, N, date_expr) → DATE_ADD(date_expr, INTERVAL N DAY)
    result = _convert_dateadd(result)

    # datediff(unit, start, end) → DATE_DIFF(end, start, unit)
    result = _convert_datediff(result)

    # NVL(a, b) → IFNULL(a, b)
    result = re.sub(r'\bNVL\s*\(', 'IFNULL(', result, flags=re.IGNORECASE)

    # ISNULL(a, b) → IFNULL(a, b)
    result = re.sub(r'\bISNULL\s*\(', 'IFNULL(', result, flags=re.IGNORECASE)

    # CONVERT(type, expr) → CAST(expr AS type) - basic pattern
    result = _convert_convert_function(result)

    # LEN(x) → LENGTH(x)
    result = re.sub(r'\bLEN\s*\(', 'LENGTH(', result, flags=re.IGNORECASE)

    # CHARINDEX(substr, str) → STRPOS(str, substr) -- swap args
    result = _convert_charindex(result)

    # TOP N → LIMIT N (handled in query structure)
    result = _convert_top_to_limit(result)

    # =========================================================================
    # 2. DDL / Table syntax replacements
    # =========================================================================

    # CREATE TEMP TABLE ... (LIKE schema.table) → CREATE TEMP TABLE ... AS SELECT * FROM `project.dataset.table` WHERE 1=0
    result = _convert_create_like(result)

    # Strip Redshift-specific storage clauses (ENCODE, DISTKEY, SORTKEY, DISTSTYLE)
    result = _strip_redshift_storage_clauses(result)

    # =========================================================================
    # 3. DELETE ... FROM pattern (Redshift style) → BigQuery MERGE syntax
    # =========================================================================
    result = _convert_delete_from(result)

    # Convert parenthesized INSERT statement queries
    result = _convert_parenthesized_insert(result)

    # =========================================================================
    # 4. UNLOAD → EXPORT DATA (if present)
    # =========================================================================
    result = _convert_unload(result)

    # =========================================================================
    # 5. COPY → LOAD DATA (if present)
    # =========================================================================
    result = _convert_copy_command(result)

    # =========================================================================
    # 6. String / misc replacements
    # =========================================================================
    
    # Convert || string concatenation to BigQuery CONCAT
    result = _convert_concat_operator(result)
    
    # :: type cast (Redshift) → CAST( AS type) - basic patterns
    result = _convert_double_colon_cast(result)

    # =========================================================================
    # 6.5 Additional Redshift Function Conversions (HIGH IMPACT!)
    # =========================================================================

    # LISTAGG(column, delimiter) → STRING_AGG(column, delimiter)
    result = re.sub(r'\bLISTAGG\s*\(', 'STRING_AGG(', result, flags=re.IGNORECASE)

    # DATE_PART('part', date) → EXTRACT(part FROM date)
    result = _convert_date_part(result)

    # SUBSTRING(str, start, length) → SUBSTR(str, start, length)
    result = re.sub(r'\bSUBSTRING\s*\(', 'SUBSTR(', result, flags=re.IGNORECASE)

    # POSITION(substr IN str) → STRPOS(str, substr)
    result = _convert_position(result)

    # REGEXP_REPLACE(str, pattern, replacement) - already compatible, but add flag support
    result = _convert_regexp_replace(result)

    # MEDIAN(column) → PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY column)
    result = _convert_median(result)

    # =========================================================================
    # 7. Project Injection for BQ Schemas
    # =========================================================================
    result = _inject_gcp_project_in_schemas(result)

    # Replace s3:// references with gs://
    result = result.replace('s3://', 'gs://')

    # Remove \" from around the GCP project/dataset variables
    # This ensures the format is exactly: "+context.gcp_src_project+"."+context.gcp_bq_prod_dataset+"
    # Original XML encoded string has: \&quot;&quot;+context.gcp_tgt_project
    result = re.sub(r'\\&quot;(&quot;\s*\+\s*context\.gcp_[a-z_]+_project)', r'\1', result)
    result = re.sub(r'(context\.gcp_bq_[a-z_]+_dataset\s*\+\s*&quot;)\\&quot;', r'\1', result)

    # Lowercase table names in the final query to handle BigQuery case sensitivity
    result = lowercase_table_names_in_sql(result)

    return result


def parse_function_args(sql: str, func_name: str) -> list:
    """
    Finds all occurrences of func_name(...) in sql and returns a list of tuples:
    (start_idx, end_idx, [arg1, arg2, arg3, ...])
    Handles nested parentheses correctly.
    """
    pattern = re.compile(rf"\b{func_name}\s*\(", re.IGNORECASE)
    results = []
    
    for match in pattern.finditer(sql):
        start_idx = match.start()
        paren_depth = 1
        current_idx = match.end()
        arg_start = current_idx
        args = []
        
        while paren_depth > 0 and current_idx < len(sql):
            char = sql[current_idx]
            if char == '(':
                paren_depth += 1
            elif char == ')':
                paren_depth -= 1
                if paren_depth == 0:
                    args.append(sql[arg_start:current_idx].strip())
                    current_idx += 1
                    break
            elif char == ',' and paren_depth == 1:
                args.append(sql[arg_start:current_idx].strip())
                arg_start = current_idx + 1
            current_idx += 1
            
        if paren_depth == 0:
            results.append((start_idx, current_idx, args))
            
    return results


def _convert_dateadd(sql: str) -> str:
    """
    Convert dateadd(interval, amount, date_expr) to BigQuery equivalent.
    dateadd(day, -N, expr) → DATE_SUB(expr, INTERVAL N DAY)
    dateadd(day, N, expr) → DATE_ADD(expr, INTERVAL N DAY)
    """
    while True:
        matches = parse_function_args(sql, "dateadd")
        if not matches:
            break
            
        target_match = None
        for start, end, args in matches:
            has_nested = any(re.search(r"\bdateadd\s*\(", arg, re.IGNORECASE) for arg in args)
            if not has_nested:
                target_match = (start, end, args)
                break
        if not target_match:
            target_match = matches[-1]
            
        start, end, args = target_match
        if len(args) != 3:
            break
            
        unit = args[0].upper()
        amount = args[1]
        date_expr = args[2]
        
        unit_map = {
            'DAY': 'DAY', 'DAYS': 'DAY',
            'WEEK': 'WEEK', 'WEEKS': 'WEEK',
            'MONTH': 'MONTH', 'MONTHS': 'MONTH',
            'QUARTER': 'QUARTER', 'QUARTERS': 'QUARTER',
            'YEAR': 'YEAR', 'YEARS': 'YEAR',
            'HOUR': 'HOUR', 'HOURS': 'HOUR',
            'MINUTE': 'MINUTE', 'MINUTES': 'MINUTE',
            'SECOND': 'SECOND', 'SECONDS': 'SECOND',
            'MILLISECOND': 'MILLISECOND', 'MILLISECONDS': 'MILLISECOND',
            'MICROSECOND': 'MICROSECOND', 'MICROSECONDS': 'MICROSECOND',
        }
        bq_unit = unit_map.get(unit, unit)
        
        if amount.startswith('-'):
            clean_amount = amount.lstrip('-').strip()
            replacement = f"DATE_SUB({date_expr}, INTERVAL {clean_amount} {bq_unit})"
        elif amount.strip().startswith('-'):
            clean_amount = amount.strip().lstrip('-').strip()
            replacement = f"DATE_SUB({date_expr}, INTERVAL {clean_amount} {bq_unit})"
        else:
            replacement = f"DATE_ADD({date_expr}, INTERVAL {amount} {bq_unit})"
            
        sql = sql[:start] + replacement + sql[end:]
        
    return sql


def _convert_datediff(sql: str) -> str:
    """
    Convert datediff(unit, start, end) → DATE_DIFF(end, start, unit)
    Note: BigQuery swaps the order of start and end compared to Redshift.
    """
    while True:
        matches = parse_function_args(sql, "datediff")
        if not matches:
            break
            
        target_match = None
        for start, end, args in matches:
            has_nested = any(re.search(r"\bdatediff\s*\(", arg, re.IGNORECASE) for arg in args)
            if not has_nested:
                target_match = (start, end, args)
                break
        if not target_match:
            target_match = matches[-1]
            
        start, end, args = target_match
        if len(args) != 3:
            break
            
        unit = args[0].upper()
        start_expr = args[1]
        end_expr = args[2]
        
        unit_map = {
            'DAY': 'DAY', 'DAYS': 'DAY',
            'WEEK': 'WEEK', 'WEEKS': 'WEEK',
            'MONTH': 'MONTH', 'MONTHS': 'MONTH',
            'QUARTER': 'QUARTER', 'QUARTERS': 'QUARTER',
            'YEAR': 'YEAR', 'YEARS': 'YEAR',
            'HOUR': 'HOUR', 'MINUTE': 'MINUTE',
            'SECOND': 'SECOND',
            'MILLISECOND': 'MILLISECOND', 'MILLISECONDS': 'MILLISECOND',
            'MICROSECOND': 'MICROSECOND', 'MICROSECONDS': 'MICROSECOND',
        }
        bq_unit = unit_map.get(unit, unit)

        replacement = f"DATE_DIFF({end_expr}, {start_expr}, {bq_unit})"
        sql = sql[:start] + replacement + sql[end:]
        
    return sql


def _convert_convert_function(sql: str) -> str:
    """
    Convert CONVERT(type, expr) → CAST(expr AS type)
    Handles nested parentheses properly using parse_function_args.
    """
    # Type mapping for SQL Server/Redshift types to BigQuery
    type_map = {
        'VARCHAR': 'STRING',
        'NVARCHAR': 'STRING',
        'CHAR': 'STRING',
        'INT': 'INT64',
        'INTEGER': 'INT64',
        'BIGINT': 'INT64',
        'TINYINT': 'INT64',
        'SMALLINT': 'INT64',
        'FLOAT': 'FLOAT64',
        'DOUBLE': 'FLOAT64',
        'DECIMAL': 'NUMERIC',
        'NUMERIC': 'NUMERIC',
        'MONEY': 'NUMERIC',
        'DATE': 'DATE',
        'DATETIME': 'DATETIME',
        'TIMESTAMP': 'TIMESTAMP',
    }

    while True:
        matches = parse_function_args(sql, "CONVERT")
        if not matches:
            break

        # Process innermost CONVERT first (no nested CONVERT in args)
        target_match = None
        for start, end, args in matches:
            has_nested = any(re.search(r"\bCONVERT\s*\(", arg, re.IGNORECASE) for arg in args)
            if not has_nested:
                target_match = (start, end, args)
                break
        if not target_match:
            target_match = matches[-1]

        start, end, args = target_match
        if len(args) != 2:
            break

        target_type = args[0].strip()
        expr = args[1].strip()

        # Map type to BigQuery equivalent
        base_type = target_type.split('(')[0].upper()
        bq_type = type_map.get(base_type, target_type)

        replacement = f'CAST({expr} AS {bq_type})'
        sql = sql[:start] + replacement + sql[end:]

    return sql


def _convert_charindex(sql: str) -> str:
    """
    Convert CHARINDEX(substr, str) → STRPOS(str, substr)
    Swaps the argument order.
    """
    while True:
        matches = parse_function_args(sql, "CHARINDEX")
        if not matches:
            break
            
        target_match = None
        for start, end, args in matches:
            has_nested = any(re.search(r"\bCHARINDEX\s*\(", arg, re.IGNORECASE) for arg in args)
            if not has_nested:
                target_match = (start, end, args)
                break
        if not target_match:
            target_match = matches[-1]
            
        start, end, args = target_match
        if len(args) != 2:
            break
            
        substr = args[0]
        str_expr = args[1]
        
        replacement = f"STRPOS({str_expr}, {substr})"
        sql = sql[:start] + replacement + sql[end:]
        
    return sql


def _convert_top_to_limit(sql: str) -> str:
    """
    Convert SELECT TOP N ... → SELECT ... LIMIT N

    Note: This is a partial conversion. The LIMIT clause must be manually added
    at the end of the query. The GCP Batch Translation API handles this automatically,
    so this function primarily serves as a fallback marker for manual review.
    """
    pattern = re.compile(
        r'\bSELECT\s+TOP\s+(\d+)\b',
        re.IGNORECASE
    )

    # Replace SELECT TOP N with SELECT and leave a marker
    # GCP Batch API will handle this properly; this is just for local fallback
    matches = list(pattern.finditer(sql))
    if matches:
        for match in reversed(matches):
            n = match.group(1)
            # Replace SELECT TOP N with SELECT
            # Note: LIMIT N should be added at the end of the statement
            sql = sql[:match.start()] + f'SELECT /* MANUAL REVIEW: Add LIMIT {n} at end of this query */' + sql[match.end():]

    return sql


def _convert_create_like(sql: str) -> str:
    """
    Convert CREATE TEMP TABLE ... (LIKE schema.table)
    → CREATE TEMP TABLE ... AS SELECT * FROM `project.dataset.table` WHERE 1=0
    """
    pattern = re.compile(
        r'CREATE\s+TEMP\s+TABLE\s+(\w+)\s*\(\s*LIKE\s+([^\s\)]+)\s*\)',
        re.IGNORECASE
    )
    
    def replace_create_like(match):
        table_name = match.group(1)
        source_table = match.group(2)
        return f'CREATE TEMP TABLE {table_name} AS SELECT * FROM {source_table} WHERE 1=0'
    
    return pattern.sub(replace_create_like, sql)


def _convert_delete_from(sql: str) -> str:
    """
    Convert Redshift DELETE ... FROM or DELETE FROM ... USING pattern.
    DELETE target FROM source WHERE target.col = source.col
    → MERGE target USING source ON target.col = source.col WHEN MATCHED THEN DELETE;
    """
    ws = r"(?:\s|&#13;|&#10;|&amp;#13;|&amp;#10;)*"
    not_xml_entity = r"(?<!&amp;quot)(?<!&amp;#10)(?<!&amp;#13)(?<!&amp;#9)(?<!&amp;#39)(?<!&amp;amp)(?<!&amp;lt)(?<!&amp;gt)(?<!&quot)(?<!&#10)(?<!&#13)(?<!&#9)(?<!&#39)(?<!&amp)(?<!&lt)(?<!&gt)"
    # Matches both original DELETE target FROM source AND previously converted DELETE FROM target USING source
    pattern = re.compile(
        rf"DELETE{ws}(?:FROM{ws})?(\S+){ws}(?:USING|FROM){ws}(\S+){ws}WHERE{ws}(.*?){not_xml_entity};",
        re.IGNORECASE | re.DOTALL
    )
    
    def replace_delete(match):
        target = match.group(1)
        source = match.group(2)
        condition = match.group(3).strip()
        return f'MERGE {target} USING {source} ON {condition} WHEN MATCHED THEN DELETE;'
    
    return pattern.sub(replace_delete, sql)


def _convert_parenthesized_insert(sql: str) -> str:
    """
    Convert INSERT INTO table (SELECT ...) or INSERT INTO table (cols) (SELECT ...)
    to INSERT INTO table SELECT ... or INSERT INTO table (cols) SELECT ...
    by removing the outer parentheses around the SELECT query.
    """
    ws = r"(?:\s|&#13;|&#10;|&amp;#13;|&amp;#10;)*"
    pattern = re.compile(
        rf"(INSERT{ws}INTO{ws}\S+(?:{ws}\([^)]+\))?{ws})\(({ws}(?:\({ws})*SELECT\b)",
        re.IGNORECASE | re.DOTALL
    )
    
    while True:
        match = pattern.search(sql)
        if not match:
            break
            
        start_insert_part = match.start(1)
        open_paren_idx = match.end(1)
        select_start_idx = match.start(2)
        
        depth = 1
        close_paren_idx = -1
        for idx in range(open_paren_idx + 1, len(sql)):
            char = sql[idx]
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
                if depth == 0:
                    close_paren_idx = idx
                    break
                    
        if close_paren_idx != -1:
            before = sql[:open_paren_idx]
            select_query = sql[select_start_idx:close_paren_idx]
            after = sql[close_paren_idx + 1:]
            sql = before + select_query + after
        else:
            break
            
    return sql


def _convert_concat_operator(sql: str) -> str:
    """
    Convert Redshift 'A || B' string concatenation to BigQuery CONCAT with CASTs.
    Casts non-string operands to STRING to prevent type mismatch errors in BigQuery.
    """
    if "||" not in sql:
        return sql
        
    def get_operand_left(s, start_idx):
        depth = 0
        idx = start_idx
        while idx >= 0:
            if idx >= 1 and s[idx-1:idx+1] == '||':
                break
            char = s[idx]
            if char == ')':
                depth += 1
            elif char == '(':
                depth -= 1
                if depth < 0:
                    break
            elif char in (',', ';') and depth == 0:
                if char == ';' and re.search(r'&(?:amp;)?(?:[a-zA-Z0-9]+|#[0-9]+)$', s[:idx]):
                    pass  # Semicolon is part of an XML entity, do not break
                else:
                    break
            if depth == 0 and idx < start_idx:
                sub = s[idx:start_idx]
                sub_stripped = sub.strip()
                match = re.match(r'^(?:SELECT|WHERE|AND|OR|ON|FROM|INSERT|UPDATE|DELETE|SET|LIMIT|GROUP|ORDER|BY|AS|HAVING)\b', sub_stripped, re.IGNORECASE)
                if match:
                    keyword_len = len(match.group(0))
                    leading_spaces = len(sub) - len(sub.lstrip())
                    return idx + leading_spaces + keyword_len
            idx -= 1
        return idx + 1

    def get_operand_right(s, start_idx):
        depth = 0
        idx = start_idx
        while idx < len(s):
            if idx < len(s) - 1 and s[idx:idx+2] == '||':
                break
            char = s[idx]
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
                if depth < 0:
                    break
            elif char in (',', ';') and depth == 0:
                if char == ';' and re.search(r'&(?:amp;)?(?:[a-zA-Z0-9]+|#[0-9]+)$', s[:idx]):
                    pass  # Semicolon is part of an XML entity, do not break
                else:
                    break
            if depth == 0 and idx > start_idx:
                sub = s[start_idx:idx]
                sub_stripped = sub.strip()
                match = re.search(r'\b(SELECT|WHERE|AND|OR|ON|FROM|INSERT|UPDATE|DELETE|SET|LIMIT|GROUP|ORDER|BY|AS|HAVING)$', sub_stripped, re.IGNORECASE)
                if match:
                    keyword = match.group(1)
                    keyword_len = len(keyword)
                    trailing_spaces = len(sub) - len(sub.rstrip())
                    keyword_start_in_sub = len(sub) - trailing_spaces - keyword_len
                    return start_idx + keyword_start_in_sub
            idx += 1
        return idx

    while True:
        idx = sql.rfind("||")
        if idx == -1:
            break
            
        quotes_before = sql[:idx].count("'")
        if quotes_before % 2 != 0:
            temp_idx = idx
            found = False
            while temp_idx > 0:
                temp_idx = sql.rfind("||", 0, temp_idx)
                if temp_idx == -1:
                    break
                if sql[:temp_idx].count("'") % 2 == 0:
                    idx = temp_idx
                    found = True
                    break
            if not found:
                break
            
        lhs_start = get_operand_left(sql, idx - 1)
        lhs = sql[lhs_start:idx].strip()
        
        rhs_end = get_operand_right(sql, idx + 2)
        rhs = sql[idx + 2:rhs_end].strip()
        
        if not lhs or not rhs:
            idx -= 2  # skip this one to avoid infinite loop
            if idx < 0:
                break
            continue
            
        def wrap_cast(op):
            op_clean = op.strip()
            if re.match(r'^CONCAT\s*\(.*\)$', op_clean, re.IGNORECASE):
                return op_clean
            if re.match(r'^CAST\s*\(.*\bAS\s+(?:STRING|VARCHAR|CHAR|TEXT)(?:\(\d+\))?\s*\)$', op_clean, re.IGNORECASE):
                op_clean = re.sub(r'\bAS\s+(?:VARCHAR|CHAR|TEXT)(?:\(\d+\))?', 'AS STRING', op_clean, flags=re.IGNORECASE)
                return op_clean
            if (op_clean.startswith("'") and op_clean.endswith("'")) or (op_clean.startswith('&quot;') and op_clean.endswith('&quot;')):
                return op_clean
            return f"CAST({op_clean} AS STRING)"

        new_lhs = wrap_cast(lhs)
        new_rhs = wrap_cast(rhs)
        
        replacement = f"CONCAT({new_lhs}, {new_rhs})"
        sql = sql[:lhs_start] + replacement + sql[rhs_end:]
        
    return sql


def _strip_redshift_storage_clauses(sql: str) -> str:
    """
    Automatically strip Redshift-specific physical storage clauses (ENCODE, DISTKEY, SORTKEY, DISTSTYLE)
    to make table creation statements compatible with BigQuery.
    """
    # 1. Strip ENCODE <encoding>
    sql = re.sub(r"\bENCODE\s+\w+\b", "", sql, flags=re.IGNORECASE)
    
    # 2. Strip DISTSTYLE <style>
    sql = re.sub(r"\bDISTSTYLE\s+(?:ALL|EVEN|KEY|AUTO)\b", "", sql, flags=re.IGNORECASE)
    
    # 3. Strip DISTKEY(<col>) or DISTKEY
    sql = re.sub(r"\bDISTKEY\s*\([^)]*\)", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bDISTKEY\b", "", sql, flags=re.IGNORECASE)
    
    # 4. Strip SORTKEY(<cols>) or SORTKEY
    sql = re.sub(r"\bSORTKEY\s*\([^)]*\)", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bSORTKEY\b", "", sql, flags=re.IGNORECASE)
    
    # 5. Clean up redundant spaces and empty lines left over
    lines = []
    for line in sql.splitlines():
        line_clean = re.sub(r"[ \t]+", " ", line).rstrip()
        line_clean = re.sub(r"\s+,\s*", ", ", line_clean)
        line_clean = re.sub(r"\(\s+", "(", line_clean)
        line_clean = re.sub(r"\s+\)", ")", line_clean)
        lines.append(line_clean)
        
    return "\n".join(lines)


def _convert_unload(sql: str) -> str:
    """
    Convert UNLOAD ('SELECT ...') TO 's3://...' ...
      EXPORT DATA OPTIONS(uri='gs://..._*', format='CSV', overwrite=true, header=true) AS SELECT ...
    """
    # Replace XML encoded newlines with actual spaces for easier matching
    # But wait, we shouldn't modify the input string permanently if we want to preserve XML.
    # We can just define a whitespace pattern that includes the XML entities.
    ws = r"(?:&#13;|&#10;|\s)*"
    not_xml_entity = r"(?<!&amp;quot)(?<!&amp;#10)(?<!&amp;#13)(?<!&amp;#9)(?<!&amp;#39)(?<!&amp;amp)(?<!&amp;lt)(?<!&amp;gt)(?<!&quot)(?<!&#10)(?<!&#13)(?<!&#9)(?<!&#39)(?<!&amp)(?<!&lt)(?<!&gt)"
    
    pattern = re.compile(
        rf"unload{ws}\({ws}'(.*?)'{ws}\){ws}to{ws}'(?:s3|gs)://([^']+)'{ws}(.*?){not_xml_entity};",
        re.IGNORECASE | re.DOTALL
    )
    
    def replace_unload(match):
        select_stmt = match.group(1)
        uri_path = match.group(2)

        # Clean escaped quotes from UNLOAD string
        # UNLOAD uses 'SELECT FROM \"schema\".table' format
        # We need to remove backslash escapes: \" → "
        select_stmt = select_stmt.replace(r'\&quot;', '&quot;')  # XML-encoded: \&quot; → &quot;
        select_stmt = select_stmt.replace(r'\"', '"')  # Raw: \" → "

        # Fix doubled quotes (empty string concatenation artifact)
        # Pattern: &quot;&quot; → &quot; (double quote becomes single quote)
        # This handles cases like ""+context.var+"" → "+context.var+"
        select_stmt = select_stmt.replace('&quot;&quot;', '&quot;')  # XML: &quot;&quot; → &quot;
        select_stmt = select_stmt.replace('""', '"')  # Raw: "" → "

        # BigQuery export requires a wildcard like _* for exporting multiple files.
        return f"EXPORT DATA OPTIONS(uri='gs://{uri_path}_*', format='CSV', overwrite=true, header=true) AS {select_stmt};"

    return pattern.sub(replace_unload, sql)


def _convert_copy_command(sql: str) -> str:
    """
    Convert COPY table FROM 's3://...'
    → LOAD DATA INTO table FROM FILES(uris=['gs://...'])
    """
    ws = r"(?:&#13;|&#10;|\s)*"
    not_xml_entity = r"(?<!&amp;quot)(?<!&amp;#10)(?<!&amp;#13)(?<!&amp;#9)(?<!&amp;#39)(?<!&amp;amp)(?<!&amp;lt)(?<!&amp;gt)(?<!&quot)(?<!&#10)(?<!&#13)(?<!&#9)(?<!&#39)(?<!&amp)(?<!&lt)(?<!&gt)"
    
    # Match the entire COPY statement up to the semicolon, capturing trailing options
    pattern = re.compile(
        rf"COPY{ws}(\S+){ws}FROM{ws}'(?:s3|gs)://([^']+)'{ws}(.*?){not_xml_entity};",
        re.IGNORECASE | re.DOTALL
    )
    
    def replace_copy(match):
        table = match.group(1)
        s3_path = match.group(2)
        trailing_options = match.group(3)
        
        # Check if context.copycommand is in the trailing options
        copy_cmd_str = ""
        if "context.copycommand" in trailing_options:
            copy_cmd_str = ' "+context.copycommand+" '
            
        skip_rows = ""
        if "IGNOREHEADER 1" in trailing_options.upper():
            skip_rows = ", skip_leading_rows=1"
            
        return f"LOAD DATA INTO {table} FROM FILES(uris=['gs://{s3_path}']{skip_rows}{copy_cmd_str});"
    
    return pattern.sub(replace_copy, sql)


def _convert_double_colon_cast(sql: str) -> str:
    """
    Convert Redshift-style ::type casts to CAST(expr AS type).
    Handles patterns like: column::varchar → CAST(column AS STRING)

    Note: This is a simplified conversion. Complex expressions with :: may
    need manual review.
    """
    type_map = {
        'varchar': 'STRING',
        'nvarchar': 'STRING',
        'char': 'STRING',
        'nchar': 'STRING',
        'text': 'STRING',
        'int': 'INT64',
        'integer': 'INT64',
        'bigint': 'INT64',
        'smallint': 'INT64',
        'tinyint': 'INT64',
        'float': 'FLOAT64',
        'double': 'FLOAT64',
        'real': 'FLOAT64',
        'decimal': 'NUMERIC',
        'numeric': 'NUMERIC',
        'money': 'NUMERIC',
        'date': 'DATE',
        'timestamp': 'DATETIME',        # Redshift TIMESTAMP (no TZ) → BQ DATETIME (no TZ)
        'timestamptz': 'TIMESTAMP',     # Redshift TIMESTAMPTZ (with TZ) → BQ TIMESTAMP (with TZ)
        'datetime': 'DATETIME',
        'boolean': 'BOOL',
        'bool': 'BOOL',
        'bytea': 'BYTES',
        'binary': 'BYTES',
        'varbinary': 'BYTES',
    }
    
    # Pattern: word::type or 'string'::type
    pattern = re.compile(r"(\b\w+(?:\([^)]*\))?)\s*::\s*(\w+(?:\([^)]*\))?)", re.IGNORECASE)
    
    def replace_cast(match):
        expr = match.group(1)
        cast_type = match.group(2).strip()
        base_type = cast_type.split('(')[0].lower()
        bq_type = type_map.get(base_type, cast_type.upper())
        # Preserve precision if present
        if '(' in cast_type and base_type in ('decimal', 'numeric', 'varchar'):
            bq_type = bq_type  # keep as-is
        return f'CAST({expr} AS {bq_type})'
    
    return pattern.sub(replace_cast, sql)


def _convert_date_part(sql: str) -> str:
    """
    Convert DATE_PART('part', date) → EXTRACT(part FROM date)
    Example: DATE_PART('month', order_date) → EXTRACT(MONTH FROM order_date)
    """
    pattern = re.compile(r"\bDATE_PART\s*\(\s*['\"](\w+)['\"]\s*,\s*(.+?)\)", re.IGNORECASE)

    def replace_datepart(match):
        part = match.group(1).upper()
        date_expr = match.group(2).strip()
        return f"EXTRACT({part} FROM {date_expr})"

    return pattern.sub(replace_datepart, sql)


def _convert_position(sql: str) -> str:
    """
    Convert POSITION(substr IN str) → STRPOS(str, substr)
    Swaps argument order to match BigQuery STRPOS function
    """
    pattern = re.compile(r"\bPOSITION\s*\(\s*(.+?)\s+IN\s+(.+?)\)", re.IGNORECASE)

    def replace_position(match):
        substr = match.group(1).strip()
        str_expr = match.group(2).strip()
        return f"STRPOS({str_expr}, {substr})"

    return pattern.sub(replace_position, sql)


def _convert_regexp_replace(sql: str) -> str:
    """
    REGEXP_REPLACE in Redshift has different flag syntax than BigQuery
    Redshift: REGEXP_REPLACE(str, pattern, replacement, 'flag')
    BigQuery: REGEXP_REPLACE(str, pattern, replacement)
    Strip the flags parameter if present (4th argument)
    """
    pattern = re.compile(r"\bREGEXP_REPLACE\s*\(([^,]+),([^,]+),([^,]+),[^)]+\)", re.IGNORECASE)

    def replace_regex(match):
        str_expr = match.group(1).strip()
        pattern_expr = match.group(2).strip()
        replacement = match.group(3).strip()
        return f"REGEXP_REPLACE({str_expr}, {pattern_expr}, {replacement})"

    return pattern.sub(replace_regex, sql)


def _convert_median(sql: str) -> str:
    """
    Convert MEDIAN(column) → PERCENTILE_CONT(column, 0.5) OVER()
    Note: BigQuery PERCENTILE_CONT is an analytic function
    """
    pattern = re.compile(r"\bMEDIAN\s*\(\s*(.+?)\s*\)", re.IGNORECASE)

    def replace_median(match):
        column = match.group(1).strip()
        return f"PERCENTILE_CONT({column}, 0.5) OVER()"

    return pattern.sub(replace_median, sql)


# =============================================================================
# Utility: identify SQL patterns that need manual review
# =============================================================================
MANUAL_REVIEW_PATTERNS = [
    (re.compile(r'\bDISTKEY\b', re.IGNORECASE), "DISTKEY (Redshift distribution) - remove for BQ"),
    (re.compile(r'\bSORTKEY\b', re.IGNORECASE), "SORTKEY (Redshift sort key) - remove for BQ"),
    (re.compile(r'\bDISTSTYLE\b', re.IGNORECASE), "DISTSTYLE (Redshift) - remove for BQ"),
    (re.compile(r'\bENCODE\s+\w+', re.IGNORECASE), "ENCODE compression (Redshift) - remove for BQ"),
    (re.compile(r'\bSTL_\w+', re.IGNORECASE), "STL system table (Redshift) - replace with BQ equivalent"),
    (re.compile(r'\bSVV_\w+', re.IGNORECASE), "SVV system view (Redshift) - replace with BQ equivalent"),
    (re.compile(r'\bSELECT\s+TOP\s+\d+\b', re.IGNORECASE), "SELECT TOP N - ensure LIMIT N added at end of query"),
    (re.compile(r'/\*\s*MANUAL REVIEW:', re.IGNORECASE), "Manual review required - check conversion comments"),
    (re.compile(r'\bIDENTITY\s*\(', re.IGNORECASE), "IDENTITY column - use GENERATE_UUID or sequence in BQ"),
    (re.compile(r'::\w+', re.IGNORECASE), ":: type cast - verify CAST conversion"),
    (re.compile(r'\bINTERLEAVED\b', re.IGNORECASE), "INTERLEAVED SORTKEY - remove for BQ"),
]


def get_manual_review_flags(sql_text: str) -> list:
    """
    Check SQL for patterns that may need manual review after automated conversion.
    
    Returns:
        List of (line_hint, pattern_description) tuples
    """
    flags = []
    for pattern, description in MANUAL_REVIEW_PATTERNS:
        matches = pattern.findall(sql_text)
        if matches:
            flags.append(f"  ⚠ {description} (found {len(matches)} occurrence(s))")
    return flags

def scan_job_file_for_dml_tables(job_file_content: str) -> set:
    """
    Scan entire job file to identify all tables that have DML operations.

    This performs a first-pass scan of the job file to extract all SQL queries
    and identify which (schema, table) combinations have INSERT/UPDATE/DELETE/MERGE operations.

    Args:
        job_file_content: Full .item XML file content

    Returns:
        Set of (schema_variable, table_name) tuples for tables with DML operations
    """
    tables_with_dml = set()

    # Extract all SQL queries from the job file
    # Pattern 1: QUERY parameters in ElementParameter nodes
    query_pattern = re.compile(
        r'<ElementParameter[^>]*field="QUERY"[^>]*>\s*<elementValue[^>]*value="([^"]*)"',
        re.IGNORECASE | re.DOTALL
    )

    for match in query_pattern.finditer(job_file_content):
        sql_content = match.group(1)
        if sql_content:
            # Decode basic HTML entities
            sql_content = sql_content.replace('&amp;', '&')
            sql_content = sql_content.replace('&#10;', '\n')
            sql_content = sql_content.replace('&#13;', '\r')
            # Extract DML tables from this query
            extract_schema_table_from_sql(sql_content, tables_with_dml)

    # Pattern 2: CODE fields in tJava components
    code_pattern = re.compile(
        r'<ElementParameter[^>]*field="CODE"[^>]*>\s*<elementValue[^>]*value="([^"]*)"',
        re.IGNORECASE | re.DOTALL
    )

    for match in code_pattern.finditer(job_file_content):
        code_content = match.group(1)
        if code_content:
            # Decode basic HTML entities
            code_content = code_content.replace('&amp;', '&')
            code_content = code_content.replace('&#10;', '\n')
            code_content = code_content.replace('&#13;', '\r')
            # Extract DML tables from this code
            extract_schema_table_from_sql(code_content, tables_with_dml)

    return tables_with_dml


def extract_schema_table_from_sql(sql_text: str, tables_with_dml: set = None) -> set:
    """
    Extract (schema, table) tuples from SQL queries that have DML operations.

    Returns:
        Set of (schema_variable, table_name) tuples for tables with INSERT/UPDATE/DELETE/MERGE
    """
    if tables_with_dml is None:
        tables_with_dml = set()

    # DML keywords to look for
    # Patterns handle both XML-encoded (&quot;) and raw (") quotes
    dml_patterns = [
        # INSERT INTO "+context.bq_xxx+".TABLE_NAME or &quot;+context.bq_xxx+&quot;.TABLE_NAME
        r'insert\s+into\s+(?:&quot;|\")?\s*\+\s*context\.(bq_[a-z0-9_]+)\s*\+\s*(?:&quot;|\")\s*\.(?:&quot;|\")?\s*([a-z0-9_]+)',
        # UPDATE "+context.bq_xxx+".TABLE_NAME
        r'update\s+(?:&quot;|\")?\s*\+\s*context\.(bq_[a-z0-9_]+)\s*\+\s*(?:&quot;|\")\s*\.(?:&quot;|\")?\s*([a-z0-9_]+)',
        # DELETE FROM "+context.bq_xxx+".TABLE_NAME
        r'delete\s+from\s+(?:&quot;|\")?\s*\+\s*context\.(bq_[a-z0-9_]+)\s*\+\s*(?:&quot;|\")\s*\.(?:&quot;|\")?\s*([a-z0-9_]+)',
        # MERGE INTO "+context.bq_xxx+".TABLE_NAME
        r'merge\s+into\s+(?:&quot;|\")?\s*\+\s*context\.(bq_[a-z0-9_]+)\s*\+\s*(?:&quot;|\")\s*\.(?:&quot;|\")?\s*([a-z0-9_]+)',
        # TRUNCATE TABLE "+context.bq_xxx+".TABLE_NAME
        r'truncate\s+table\s+(?:&quot;|\")?\s*\+\s*context\.(bq_[a-z0-9_]+)\s*\+\s*(?:&quot;|\")\s*\.(?:&quot;|\")?\s*([a-z0-9_]+)',
    ]

    for pattern in dml_patterns:
        matches = re.finditer(pattern, sql_text, re.IGNORECASE | re.DOTALL)
        for match in matches:
            schema_var = match.group(1).lower()  # e.g., "bq_prod_dataset"
            table_name = match.group(2).lower()  # e.g., "pos_hdr_cust_card_xtnd"
            tables_with_dml.add((schema_var, table_name))

    return tables_with_dml


def _inject_gcp_project_in_schemas(sql_text: str) -> str:
    """
    Prepends context.gcp_src_project or context.gcp_tgt_project to
    context.bq_...schema references based on the preceding SQL keyword.

    Handles both XML-encoded quotes (&quot;) and raw quotes (").

    Can be disabled via config.INJECT_GCP_PROJECT_IN_SCHEMAS flag.

    Args:
        sql_text: SQL query text
    """
    from config import INJECT_GCP_PROJECT_IN_SCHEMAS

    if not INJECT_GCP_PROJECT_IN_SCHEMAS:
        return sql_text

    def _get_project_by_context(before_text: str) -> str:
        """Determine project variable based on the preceding SQL keyword."""
        bt = before_text.lower()
        keywords = re.findall(
            r'\b(from|join|insert\s+into|update|delete\s+from|delete|merge\s+into|merge|truncate\s+table|table)\b',
            bt
        )
        if keywords:
            last_keyword = keywords[-1]
            if last_keyword in ('insert into', 'update', 'delete from', 'delete', 'merge into', 'merge', 'truncate table', 'table'):
                return "context.gcp_tgt_project"
        return "context.gcp_src_project"

    # Pattern 1: XML-encoded quotes  &quot;+context.bq_xxx+&quot; (with optional spaces)
    def inject_xml(match):
        before_text = match.string[:match.start()]
        bt_lower = before_text.lower()
        # Skip if project is already injected
        if bt_lower.endswith('gcp_tgt_project+&quot;.') or bt_lower.endswith('gcp_src_project+&quot;.'):
            return match.group(0)
        project = _get_project_by_context(before_text)
        # Match groups: (sp1)(+)(sp2)context.(var)(sp3)(+)(sp4)
        # Preserve original spacing
        sp1 = match.group(1)  # space before first +
        sp2 = match.group(3)  # space after first +
        var_name = match.group(4)  # variable name
        sp3 = match.group(5)  # space before second +
        sp4 = match.group(7)  # space after second +
        return f'&quot;{sp1}+{sp2}{project}{sp3}+{sp4}&quot;.&quot;{sp1}+{sp2}context.{var_name}{sp3}+{sp4}&quot;'

    pattern_xml = re.compile(r'&quot;(\s*)(\+)(\s*)context\.(bq_[a-zA-Z0-9_]+)(\s*)(\+)(\s*)&quot;')
    sql_text = pattern_xml.sub(inject_xml, sql_text)

    # Pattern 2: Raw quotes  "+context.bq_xxx+" (with optional spaces)
    def inject_raw(match):
        before_text = match.string[:match.start()]
        bt_lower = before_text.lower()
        # Skip if project is already injected
        if bt_lower.endswith('gcp_tgt_project+".') or bt_lower.endswith('gcp_src_project+".'):
            return match.group(0)
        project = _get_project_by_context(before_text)
        # Match groups: (sp1)(+)(sp2)context.(var)(sp3)(+)(sp4)
        # Preserve original spacing
        sp1 = match.group(1)  # space before first +
        sp2 = match.group(3)  # space after first +
        var_name = match.group(4)  # variable name
        sp3 = match.group(5)  # space before second +
        sp4 = match.group(7)  # space after second +
        return f'"{sp1}+{sp2}{project}{sp3}+{sp4}"."{sp1}+{sp2}context.{var_name}{sp3}+{sp4}"'

    pattern_raw = re.compile(r'"(\s*)(\+)(\s*)context\.(bq_[a-zA-Z0-9_]+)(\s*)(\+)(\s*)"')
    sql_text = pattern_raw.sub(inject_raw, sql_text)

    return sql_text


def preprocess_placeholders_for_api(sql: str) -> str:
    """
    Temporarily preprocess placeholders (talendvarX) before API call to prevent syntax errors.
    Converts bare placeholders in expression or value positions to valid SQL dummy expressions.
    The goal is to produce syntactically valid SQL that the GCP Batch Translation API can parse.
    """
    placeholder_pattern = re.compile(r'\b(talendvar(?:start|end)?\d+)\b')
    
    def repl(match):
        pl = match.group(1)
        start_idx = match.start()
        end_idx = match.end()
        
        # Check surrounding characters for dotted identifier context (schema.table or table.column)
        before_char = sql[start_idx - 1] if start_idx > 0 else ''
        after_text = sql[end_idx:].lstrip()
        after_char = after_text[0] if after_text else ''
        
        # If part of a dotted path, keep as identifier
        if before_char == '.' or after_char == '.':
            return pl

        # Get preceding text and last word
        preceding_text = sql[:start_idx].rstrip()
        words = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', preceding_text)
        last_word = words[-1].lower() if words else ''

        # CRITICAL FIX: Check if preceded by identifier then dot (e.g., "dataset.talendvar0")
        # This catches: FROM schema.talendvar0, JOIN dataset.talendvar1, etc.
        # Pattern: word characters, then dot, then optional whitespace, then our placeholder
        schema_ref_pattern = re.search(r'\w+\.\s*$', preceding_text)
        if schema_ref_pattern:
            return pl  # Keep as identifier - it's a schema.table or table.column reference
        
        # Get last non-whitespace character
        last_char = preceding_text[-1] if preceding_text else ''
        
        # INTERVAL <amount> DAY/HOUR/MINUTE/SECOND — needs a numeric literal
        if last_word == 'interval':
            return f"1 /* {pl} */"
        
        # Table/schema/identifier positions — keep as an identifier
        # These keywords are followed by table/column/schema names, NOT values
        table_keywords = {
            'from', 'join', 'into', 'update', 'table', 'using', 'merge',
            'inner', 'left', 'right', 'outer', 'cross', 'full',  # JOIN types
            'insert', 'delete', 'truncate', 'create', 'drop', 'alter'  # DDL
        }
        if last_word in table_keywords:
            return pl

        # SELECT list, GROUP BY, ORDER BY, AS alias — keep as identifier
        if last_word in ('select', 'by', 'as', 'set', 'on'):
            return pl
        
        # After comparison/arithmetic operators — needs a value
        if last_char in ('=', '<', '>', '!', '+', '-', '*', '/', '%'):
            return f"1 /* {pl} */"
            
        # WHERE/HAVING special handling:
        # If immediately after WHERE/HAVING (no operators), it's likely a column name
        # Example: WHERE talendvar0 = 'value' (talendvar0 is column, not value)
        if last_word in ('where', 'having', 'and', 'or'):
            # Check if there's an operator in the next few characters after our placeholder
            lookahead = after_text[:20] if len(after_text) > 0 else ''
            # If followed by comparison operator, it's a column name
            if any(op in lookahead for op in ['=', '<', '>', '!=', 'IN', 'LIKE', 'IS']):
                return pl  # Keep as identifier (column name)
            # Otherwise treat as value
            return f"1 /* {pl} */"

        # After keywords that expect a value
        value_keywords = {'when', 'then', 'else', 'not', 'in', 'like', 'between', 'values', 'limit', 'offset', 'top'}
        if last_word in value_keywords:
            return f"1 /* {pl} */"

        # After a comma - need to distinguish SELECT list vs VALUES list
        if last_char == ',':
            # Check if we're in a SELECT clause (between SELECT and FROM/WHERE)
            preceding_upper = sql[:start_idx].upper()

            # Find the last SELECT keyword position
            select_matches = list(re.finditer(r'\bSELECT\b', preceding_upper))
            if select_matches:
                last_select_pos = select_matches[-1].end()
                after_select = preceding_upper[last_select_pos:]

                # If no FROM/WHERE/GROUP/ORDER after SELECT, we're still in SELECT list
                if not any(kw in after_select for kw in ['FROM', 'WHERE', 'GROUP BY', 'ORDER BY', 'HAVING', 'LIMIT']):
                    # We're in SELECT column list - keep as identifier
                    return pl

            # Check if we're in VALUES clause
            if 'VALUES' in preceding_upper:
                values_matches = list(re.finditer(r'\bVALUES\b', preceding_upper))
                if values_matches:
                    last_values_pos = values_matches[-1].end()
                    # If we're after VALUES, treat as value
                    if last_values_pos < len(preceding_upper):
                        return f"1 /* {pl} */"

            # Default for comma: keep as identifier (safer for API)
            return pl

        # After opening parenthesis - check context
        if last_char == '(':
            # If after VALUES, it's a value
            if last_word == 'values':
                return f"1 /* {pl} */"
            # If after IN, it's a value
            if last_word == 'in':
                return f"1 /* {pl} */"
            # Otherwise keep as identifier (function call, subquery, etc.)
            return pl

        # Default: keep as identifier (most conservative approach)
        # The API is smart enough to handle unknown identifiers
        return pl
        
    return placeholder_pattern.sub(repl, sql)


def restore_placeholders_after_api(sql: str) -> str:
    """
    Restore preprocessed dummy SQL placeholders back to their original talendvar form.
    Handles all patterns: 1 /* pl */, AND 1=1 /* pl */, standalone /* pl */
    """
    # Pattern: optional leading "AND 1=1 " or "1 " before the comment, optional trailing " 1" after
    pattern = re.compile(
        r'(?:\b(?:and|or)\s+)?(?:1\s*=\s*1\s+)?(?:\b1\s+)?/\*\s*(talendvar(?:start|end)?\d+)\s*\*/(?:\s+1\b)?',
        re.IGNORECASE
    )
    return pattern.sub(r'\1', sql)
