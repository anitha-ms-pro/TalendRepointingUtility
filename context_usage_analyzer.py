"""
Context Usage Analyzer
Analyzes which context files are used by specific job folders.
"""

import os
import re
from typing import Set


def find_contexts_used_by_jobs(job_folders: list, logger) -> Set[str]:
    """
    Scan job folders to find which context files they reference.

    Args:
        job_folders: List of job folder paths to scan
        logger: Logger instance

    Returns:
        Set of context names (without version/extension) that are used
    """
    contexts_used = set()

    logger.info(f"\n{'='*70}")
    logger.info(f"ANALYZING CONTEXT USAGE IN SPECIFIED JOB FOLDERS")
    logger.info(f"{'='*70}")

    for folder_path in job_folders:
        folder_name = os.path.basename(folder_path)
        logger.info(f"\nScanning folder: {folder_name}")

        # Find all .item files in this folder
        item_files = []
        for root, dirs, files in os.walk(folder_path):
            for f in files:
                if f.endswith('.item'):
                    item_files.append(os.path.join(root, f))

        logger.info(f"  Found {len(item_files)} .item files")

        # Scan each job file for repositoryContextId references
        folder_contexts = set()
        for item_file in item_files:
            try:
                with open(item_file, 'r', encoding='utf-8') as f:
                    content = f.read()

                # Find repositoryContextId attributes - these are internal IDs
                # We need to find contextParameter elements with repositoryContextId
                # Pattern: <contextParameter ... name="VariableName" ... repositoryContextId="_someID" ...>

                # Better approach: Look for context file references in job properties
                # Jobs reference contexts by name in their .properties file or through parameter names

                # Extract context names from variable naming patterns
                # e.g., context.Redshift_CustDB_MKT_Schema suggests Redshift_CustDB_MKT context
                context_vars = re.findall(r'context\.([A-Za-z0-9_]+)_(?:Schema|Server|Database|Port|Login|Password|AdditionalParams|Dataset|Project)', content)

                for var_prefix in context_vars:
                    # Extract the context name (e.g., "Redshift_CustDB_MKT" from "Redshift_CustDB_MKT_Schema")
                    folder_contexts.add(var_prefix)

            except Exception as e:
                logger.debug(f"  Error reading {os.path.basename(item_file)}: {e}")
                continue

        if folder_contexts:
            logger.info(f"  Contexts used: {sorted(folder_contexts)}")
            contexts_used.update(folder_contexts)
        else:
            logger.info(f"  No contexts detected")

    logger.info(f"\n{'='*70}")
    logger.info(f"TOTAL UNIQUE CONTEXTS FOUND: {len(contexts_used)}")
    logger.info(f"{'='*70}")
    if contexts_used:
        for ctx in sorted(contexts_used):
            logger.info(f"  - {ctx}")

    return contexts_used


def filter_context_renames(context_renames: dict, contexts_to_process: Set[str]) -> dict:
    """
    Filter context renames to only include contexts that should be processed.

    Args:
        context_renames: Full dictionary of context renames
        contexts_to_process: Set of context names to include

    Returns:
        Filtered dictionary
    """
    if not contexts_to_process:
        return context_renames

    filtered = {}
    for old_context, new_context in context_renames.items():
        if old_context in contexts_to_process:
            filtered[old_context] = new_context

    return filtered


def filter_variable_renames(variable_renames: dict, contexts_to_process: Set[str]) -> dict:
    """
    Filter variable renames to only include variables from contexts that should be processed.

    Args:
        variable_renames: Full dictionary of variable renames
        contexts_to_process: Set of context names to include

    Returns:
        Filtered dictionary
    """
    if not contexts_to_process:
        return variable_renames

    filtered = {}
    for old_var, new_var in variable_renames.items():
        # Check if this variable belongs to one of the contexts we're processing
        # Variable names typically follow pattern: ContextName_VariableName
        # e.g., Redshift_CustDB_MKT_Schema, S3_Bucket_GDAP_archive

        # Check if variable starts with any of the context names
        for context in contexts_to_process:
            if old_var.startswith(context + '_') or context in old_var:
                filtered[old_var] = new_var
                break

    return filtered
