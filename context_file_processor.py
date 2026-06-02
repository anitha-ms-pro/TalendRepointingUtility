"""
Context File Processor
Handles renaming and updating of Talend repository context files based on Excel mappings.
"""

import os
import re
import shutil
from typing import Dict
import logging
import uuid
from datetime import datetime


def backup_context_file(context_file_path: str) -> str:
    """
    Create a backup of a context file.

    Args:
        context_file_path: Full path to the context .item file

    Returns:
        Path to the backup file
    """
    backup_path = context_file_path + '.backup'
    if not os.path.exists(backup_path):
        shutil.copy2(context_file_path, backup_path)
    return backup_path


def create_properties_file(item_file_path: str, context_name: str, logger: logging.Logger) -> bool:
    """
    Create a .properties file for a context .item file that doesn't have one.

    Args:
        item_file_path: Path to the .item file
        context_name: Context name (e.g., 'BQ_CustDB_MKT')
        logger: Logger instance

    Returns:
        True if created successfully
    """
    try:
        # Read the .item file to extract context IDs
        with open(item_file_path, 'r', encoding='utf-8') as f:
            item_content = f.read()

        # Extract context environment IDs
        context_ids = re.findall(r'<talendfile:ContextType xmi:id="([^"]+)"', item_content)

        if not context_ids:
            logger.warning(f"  Could not extract context IDs from {os.path.basename(item_file_path)}")
            return False

        # Extract version from filename
        version_match = re.search(r'_(\d+\.\d+)\.item$', os.path.basename(item_file_path))
        version = version_match.group(1) if version_match else '0.1'

        item_filename = os.path.basename(item_file_path)

        # Generate a unique ID
        unique_id = f"_{uuid.uuid4().hex[:8]}-{uuid.uuid4().hex[:4]}-{uuid.uuid4().hex[:4]}"

        # Create properties file content
        props_content = f'''<?xml version="1.0" encoding="UTF-8"?>
<xmi:XMI xmi:version="2.0" xmlns:xmi="http://www.omg.org/XMI" xmlns:TalendProperties="http://www.talend.org/properties">
  <TalendProperties:Property xmi:id="_cB3XSHCDEeeLSaITbeoHMA" id="{unique_id}" label="{context_name}" version="{version}" statusCode="" item="_cB3XSnCDEeeLSaITbeoHMA" displayName="{context_name}">
    <author href="../talend.project#_OYXU8FtBEfGqyLMUox0Hjw"/>
    <additionalProperties xmi:id="_5v0Idg-QEemYoceZhi-Cyw" key="created_product_fullname" value="Talend Cloud Data Fabric"/>
    <additionalProperties xmi:id="_5v0Idw-QEemYoceZhi-Cyw" key="created_product_version" value="8.0.1.20240321_0816-patch"/>
    <additionalProperties xmi:id="_5v0Iew-QEemYoceZhi-Cyw" key="created_date" value="{datetime.now().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]}+0530"/>
    <additionalProperties xmi:id="_5v0IfA-QEemYoceZhi-Cyw" key="modified_product_fullname" value="Talend Cloud Data Fabric"/>
    <additionalProperties xmi:id="_5v0IfQ-QEemYoceZhi-Cyw" key="modified_product_version" value="8.0.1.20240321_0816-patch"/>
    <additionalProperties xmi:id="_5v0Ifg-QEemYoceZhi-Cyw" key="modified_date" value="{datetime.now().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]}+0530"/>
  </TalendProperties:Property>
  <TalendProperties:ItemState xmi:id="_tB7Um12MEfGXZs8BRISwFQ" path=""/>
  <TalendProperties:ContextItem xmi:id="_cB3XSnCDEeeLSaITbeoHMA" property="_cB3XSHCDEeeLSaITbeoHMA" state="_tB7Um12MEfGXZs8BRISwFQ" defaultContext="Development">
'''

        # Add context href elements
        for ctx_id in context_ids:
            props_content += f'    <context href="{item_filename}#{ctx_id}"/>\n'

        props_content += '''  </TalendProperties:ContextItem>
</xmi:XMI>
'''

        # Write properties file
        props_path = item_file_path.replace('.item', '.properties')
        with open(props_path, 'w', encoding='utf-8') as f:
            f.write(props_content)

        return True

    except Exception as e:
        logger.warning(f"  Error creating properties file: {e}")
        return False


def update_properties_file_content(props_path: str, old_context_name: str, new_context_name: str, new_item_filename: str, logger: logging.Logger) -> bool:
    """
    Update the internal XML content of .properties file to match new context name.

    Args:
        props_path: Path to .properties file
        old_context_name: Old context name
        new_context_name: New context name
        new_item_filename: New .item filename (e.g., BQ_CustDB_MKT_0.1.item)
        logger: Logger instance

    Returns:
        True if updated successfully
    """
    if not os.path.exists(props_path):
        return False

    try:
        with open(props_path, 'r', encoding='utf-8') as f:
            content = f.read()

        original_content = content

        # Update label attribute
        content = re.sub(
            rf'label="{re.escape(old_context_name)}"',
            f'label="{new_context_name}"',
            content
        )

        # Update displayName attribute
        content = re.sub(
            rf'displayName="{re.escape(old_context_name)}"',
            f'displayName="{new_context_name}"',
            content
        )

        # Update href references - from old .item filename to new .item filename
        # Pattern: href="Redshift_CustDB_MKT_0.1.item#..."
        old_item_filename = old_context_name + props_path[props_path.rfind('_'):props_path.rfind('.properties')] + '.item'
        content = re.sub(
            rf'href="{re.escape(old_item_filename)}#',
            f'href="{new_item_filename}#',
            content
        )

        if content != original_content:
            with open(props_path, 'w', encoding='utf-8') as f:
                f.write(content)
            return True

        return False

    except Exception as e:
        logger.warning(f"  Error updating properties file: {e}")
        return False


def remove_unnecessary_bq_parameters(context_file_path: str, logger: logging.Logger) -> int:
    """
    Remove parameters that are not needed for BigQuery contexts.

    BigQuery doesn't need: Port, Login, Password, Database, AdditionalParams

    Args:
        context_file_path: Full path to the context .item file
        logger: Logger instance

    Returns:
        Number of parameters removed
    """
    if not os.path.exists(context_file_path):
        return 0

    # Only process BQ context files
    if not os.path.basename(context_file_path).startswith('BQ_'):
        return 0

    try:
        with open(context_file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        original_content = content
        removed_count = 0

        # Parameters to remove (case-insensitive suffix matching)
        remove_suffixes = ['_Port', '_Login', '_Password', '_Database', '_AdditionalParams']

        # Find and remove contextParameter elements with these suffixes
        for suffix in remove_suffixes:
            # Pattern to match entire contextParameter element
            # Matches: <contextParameter ... name="BQ_XXX_Port" ... />
            pattern = r'<contextParameter[^>]*name="[^"]*' + re.escape(suffix) + r'"[^>]*(?:/?>|>[^<]*</contextParameter>)\s*\n?'
            matches = re.findall(pattern, content)
            removed_count += len(matches)
            content = re.sub(pattern, '', content)

        if content != original_content:
            with open(context_file_path, 'w', encoding='utf-8') as f:
                f.write(content)

            if removed_count > 0:
                logger.info(f"  Removed {removed_count} unnecessary BQ parameters (Port, Login, Password, Database, AdditionalParams)")

        return removed_count

    except Exception as e:
        logger.warning(f"  Error removing unnecessary parameters: {e}")
        return 0


def rename_context_file(old_path: str, new_context_name: str, logger: logging.Logger) -> str:
    """
    Create a new context file based on the old one (COPY, not rename).
    This keeps the original Redshift context intact and creates a new BQ context.

    Args:
        old_path: Full path to the old context file
        new_context_name: New context name (e.g., 'BQ_CustDB_MKT')
        logger: Logger instance

    Returns:
        Path to the new file
    """
    directory = os.path.dirname(old_path)
    old_filename = os.path.basename(old_path)

    # Extract old context name and version
    version_match = re.search(r'(_\d+\.\d+\.item)$', old_filename)
    if version_match:
        version = version_match.group(1)
        old_context_name = old_filename.replace(version, '')
        new_filename = f"{new_context_name}{version}"
    else:
        old_context_name = old_filename.replace('.item', '')
        new_filename = f"{new_context_name}.item"

    new_path = os.path.join(directory, new_filename)

    # Check if target BQ context already exists
    if os.path.exists(new_path) and new_path != old_path:
        logger.info(f"  Skipped creation: {new_filename} already exists (using existing BQ context)")
        return new_path

    # COPY the .item file (instead of rename)
    if os.path.exists(old_path) and old_path != new_path:
        shutil.copy2(old_path, new_path)
        logger.info(f"  Created new context file: {new_filename} (from {old_filename})")

        # Remove unnecessary BQ parameters (Port, Login, Password, Database, AdditionalParams)
        remove_unnecessary_bq_parameters(new_path, logger)

        # Also copy and update .properties file if it exists
        old_props = old_path.replace('.item', '.properties')
        new_props = new_path.replace('.item', '.properties')

        if os.path.exists(old_props):
            # Copy the properties file
            shutil.copy2(old_props, new_props)

            # Update internal content with new context name
            update_properties_file_content(new_props, old_context_name, new_context_name, new_filename, logger)

            logger.info(f"  Created properties file: {os.path.basename(new_props)}")
        else:
            # Properties file doesn't exist - create one
            logger.warning(f"  Properties file not found for {old_context_name}, creating new one")
            create_properties_file(new_path, new_context_name, logger)
            logger.info(f"  Created properties file: {os.path.basename(new_props)}")

    return new_path


def update_context_file_variables(context_file_path: str, variable_renames: Dict[str, str],
                                  logger: logging.Logger) -> int:
    """
    Update variable names inside a context file across all environments.

    Args:
        context_file_path: Full path to the context .item file
        variable_renames: Dictionary mapping old variable names to new ones
        logger: Logger instance

    Returns:
        Number of variables renamed
    """
    if not os.path.exists(context_file_path):
        logger.warning(f"  Context file not found: {context_file_path}")
        return 0

    with open(context_file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    original_content = content
    renames_count = 0

    # Update contextParameter name attributes
    for old_var, new_var in variable_renames.items():
        if old_var == new_var:
            continue

        # Pattern: <contextParameter ... name="old_var" ...>
        old_pattern = f'name="{old_var}"'
        new_pattern = f'name="{new_var}"'

        if old_pattern in content:
            count = content.count(old_pattern)
            content = content.replace(old_pattern, new_pattern)
            renames_count += count
            logger.debug(f"    Renamed variable: {old_var} → {new_var} ({count} occurrences)")

        # Also update prompt attributes
        old_prompt = f'prompt="{old_var}?"'
        new_prompt = f'prompt="{new_var}?"'

        if old_prompt in content:
            content = content.replace(old_prompt, new_prompt)

    # Write back if changes were made
    if content != original_content:
        with open(context_file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        logger.info(f"  Updated {renames_count} variable references in context file")

    return renames_count


def process_context_files(context_dir: str, context_renames: Dict[str, str],
                          variable_renames: Dict[str, str], logger: logging.Logger,
                          dry_run: bool = False) -> Dict:
    """
    Process all context files in the context directory.

    Args:
        context_dir: Path to the context directory
        context_renames: Dictionary mapping old context names to new ones
        variable_renames: Dictionary mapping old variable names to new ones
        logger: Logger instance
        dry_run: If True, only report changes without making them

    Returns:
        Dictionary with processing statistics
    """
    stats = {
        'context_files_created': 0,  # Changed from 'renamed' to 'created'
        'context_files_updated': 0,
        'total_variable_renames': 0,
        'backups_created': 0,
    }

    if not os.path.exists(context_dir):
        logger.error(f"Context directory not found: {context_dir}")
        return stats

    logger.info(f"\n{'='*70}")
    logger.info(f"PROCESSING CONTEXT FILES")
    logger.info(f"{'='*70}")
    logger.info(f"Context directory: {context_dir}")
    logger.info(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")

    # Find all context .item files recursively (including subfolders)
    context_files = []
    for root, dirs, files in os.walk(context_dir):
        for f in files:
            if f.endswith('.item'):
                rel_path = os.path.relpath(os.path.join(root, f), context_dir)
                context_files.append(rel_path)

    logger.info(f"Found {len(context_files)} context files (including subfolders)")

    for rel_filepath in sorted(context_files):
        filename = os.path.basename(rel_filepath)

        # Extract context name from filename (remove version suffix)
        context_name_match = re.match(r'(.+?)_\d+\.\d+\.item$', filename)
        if not context_name_match:
            continue

        old_context_name = context_name_match.group(1)
        new_context_name = context_renames.get(old_context_name, old_context_name)

        context_file_path = os.path.join(context_dir, rel_filepath)

        logger.info(f"\nProcessing: {filename}")

        # Step 1: Create backup
        if not dry_run:
            backup_path = backup_context_file(context_file_path)
            stats['backups_created'] += 1
            logger.info(f"  ✓ Backup created: {os.path.basename(backup_path)}")

        # Step 2: Filter variable renames relevant to this context
        # We'll apply all variable renames since context files can contain variables
        # from the same context name
        relevant_var_renames = {}

        # Get all variables that belong to this context from Excel
        # For simplicity, apply all variable renames (they're idempotent if not present)
        relevant_var_renames = variable_renames

        # Step 3: Update variables inside the file
        if not dry_run:
            renames_count = update_context_file_variables(context_file_path, relevant_var_renames, logger)
            if renames_count > 0:
                stats['context_files_updated'] += 1
                stats['total_variable_renames'] += renames_count

        # Step 4: Rename the context file if needed
        if old_context_name != new_context_name:
            if dry_run:
                logger.info(f"  [DRY RUN] Would rename: {old_context_name} → {new_context_name}")
            else:
                new_path = rename_context_file(context_file_path, new_context_name, logger)
                stats['context_files_created'] += 1

    logger.info(f"\n{'='*70}")
    logger.info(f"CONTEXT FILES PROCESSING SUMMARY")
    logger.info(f"{'='*70}")
    logger.info(f"  New BQ/GCS contexts created: {stats['context_files_created']}")
    logger.info(f"  Context files updated:     {stats['context_files_updated']}")
    logger.info(f"  Total variable renames:    {stats['total_variable_renames']}")
    logger.info(f"  Backups created:           {stats['backups_created']}")
    logger.info(f"{'='*70}")

    return stats
