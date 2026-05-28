"""
Generate conversion reports from utility logs and converted jobs.

This script analyzes:
- Log files to extract conversion details
- Original and converted job files to compare
- Generates comprehensive Markdown reports

Usage:
  python generate_reports_from_logs.py <log_file> <jobs_folder>
"""

import os
import sys
import re
from pathlib import Path

# Ensure UTF-8 output encoding for Windows
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        # Python < 3.7
        import codecs
        sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
        sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')

from report_generator import JobConversionReport, save_report
import config


def parse_log_for_job(log_content: str, job_name: str) -> dict:
    """Extract conversion details for a specific job from log."""

    details = {
        'component_replacements': [],
        'component_removals': [],
        'unique_name_mappings': {},
        'sql_api': [],
        'sql_fallback': [],
        'warnings': [],
    }

    # Find the section for this job
    # Log format: "Processing: JobName_X_Y.item" or "Processing: JobName_X.Y.item"
    # But job_name comes from filename: "JobName_X_Y" or "JobName_X_Y"
    # Need to handle version number format differences (underscores vs dots)

    # Generate variants of job name to search for
    job_name_variants = []

    # Variant 1: job_name.item (as-is with extension)
    job_name_variants.append(f"{job_name}.item")

    # Variant 2: Replace last underscore+digit pattern with dot (e.g., "job_1_0" -> "job_1.0")
    # This handles version numbers like "1_0" -> "1.0"
    import re as re_local
    dot_variant = re_local.sub(r'_(\d+)$', r'.\1', job_name)
    if dot_variant != job_name:
        job_name_variants.append(f"{dot_variant}.item")

    # Variant 3: Replace all underscores after last known separator with dots
    # Handle cases like "Job_1_0_0" -> "Job_1.0.0"
    parts = job_name.rsplit('_', 2)  # Split from right, max 2 splits
    if len(parts) > 1 and all(p.isdigit() for p in parts[1:]):
        dot_variant2 = parts[0] + '_' + '.'.join(parts[1:])
        job_name_variants.append(f"{dot_variant2}.item")

    job_sections = []
    lines = log_content.split('\n')

    for variant in job_name_variants:
        job_pattern = rf"Processing: {re.escape(variant)}"
        in_job_section = False
        current_section = []

        for line in lines:
            if re.search(job_pattern, line):
                in_job_section = True
                current_section = [line]
            elif in_job_section:
                # End section when we hit another "Processing:" line
                if line.strip().startswith('Processing:'):
                    # Different job - end section
                    job_sections.append('\n'.join(current_section))
                    in_job_section = False
                    current_section = []
                else:
                    current_section.append(line)

        if current_section:
            job_sections.append('\n'.join(current_section))

        # If we found sections, stop trying variants
        if job_sections:
            break

    job_log = '\n'.join(job_sections)

    # Parse component replacements
    replacement_pattern = r'Replaced componentName: (\w+) → (\w+) \((\d+) occurrences\)'
    for match in re.finditer(replacement_pattern, job_log):
        old_comp, new_comp, count = match.groups()
        details['component_replacements'].append((old_comp, new_comp, int(count)))

    # Parse component removals
    removal_pattern = r'Removing component: (\w+) \(UNIQUE_NAME: ([^)]+)\)'
    for match in re.finditer(removal_pattern, job_log):
        comp_name, unique_name = match.groups()
        details['component_removals'].append((comp_name, unique_name))

    # Parse UNIQUE_NAME mappings
    mapping_pattern = r'Renamed UNIQUE_NAME: ([^\s]+) → ([^\s]+)'
    for match in re.finditer(mapping_pattern, job_log):
        old_unique, new_unique = match.groups()
        details['unique_name_mappings'][old_unique] = new_unique

    # Parse SQL conversions
    api_pattern = r'Using GCP Batch translated SQL for component ([^\s]+) \(original: ([^)]+)\)'
    for match in re.finditer(api_pattern, job_log):
        component, original = match.groups()
        details['sql_api'].append((component, original))

    fallback_pattern = r'Converted SQL query locally for component ([^\s]+)'
    for match in re.finditer(fallback_pattern, job_log):
        component = match.group(1)
        details['sql_fallback'].append((component, "Local conversion rules"))

    # Parse warnings
    warning_pattern = r'WARNING.*?GCP Batch Translation returned a parser error for query: ([^\s]+)\.sql'
    for match in re.finditer(warning_pattern, job_log):
        query_name = match.group(1)
        details['warnings'].append((query_name, "GCP API parser error - used fallback"))

    return details


def analyze_job_file(job_path: str) -> dict:
    """Analyze a job file to extract component and context information."""

    try:
        with open(job_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        return {'error': str(e)}

    info = {
        'total_components': 0,
        'job_level_contexts': [],
        'repo_level_contexts': [],
    }

    # Count components
    info['total_components'] = len(re.findall(r'<node\s+componentName="', content))

    # Find all USED context variables (context.VarName)
    used_vars = set()
    for match in re.finditer(r'context\.([A-Za-z_][A-Za-z0-9_]*)', content):
        used_vars.add(match.group(1))

    # Extract context parameters - attributes can be in any order
    # Find all contextParameter tags
    context_tags = re.finditer(r'<contextParameter([^>]*)>', content)

    # Build dictionaries to collect all variables
    all_job_level = {}
    all_repo_level = {}

    for tag_match in context_tags:
        attrs = tag_match.group(1)

        # Extract individual attributes
        name_match = re.search(r'name="([^"]+)"', attrs)
        type_match = re.search(r'type="([^"]*)"', attrs)
        value_match = re.search(r'value="([^"]*)"', attrs)
        repo_match = re.search(r'repositoryContextId="([^"]*)"', attrs)

        if not name_match:
            continue

        name = name_match.group(1)
        var_type = type_match.group(1) if type_match else ""
        value = value_match.group(1) if value_match else ""
        repo_id = repo_match.group(1) if repo_match else ""

        # Skip if not used in the job
        if name not in used_vars:
            continue

        # "built-in" or empty means job-level, anything else is repository-level
        if repo_id == "built-in" or repo_id == "":
            all_job_level[name] = (var_type, value)
        else:
            all_repo_level[name] = repo_id

    # Convert to lists
    for name, (var_type, value) in all_job_level.items():
        info['job_level_contexts'].append((name, var_type, value))

    for name, repo_id in all_repo_level.items():
        info['repo_level_contexts'].append((name, repo_id))

    return info


def generate_report_for_job(job_name: str, log_details: dict, original_info: dict, converted_info: dict) -> JobConversionReport:
    """Generate a comprehensive report for a job."""

    report = JobConversionReport(job_name)

    # Set total components
    report.total_components = converted_info.get('total_components', 0)

    # Add component replacements
    for old_comp, new_comp, count in log_details['component_replacements']:
        # Find corresponding UNIQUE_NAME mappings
        # Match on NEW component name since old UNIQUE_NAME might be different
        # (e.g., tRedshiftRow had UNIQUE_NAME tDBRow_1 in old file)
        for old_unique, new_unique in log_details['unique_name_mappings'].items():
            # Match on new_unique prefix to handle cases where old UNIQUE_NAME
            # doesn't match old componentName (e.g., tDBRow vs tRedshiftRow)
            if new_unique.startswith(new_comp + '_'):
                report.add_component_replacement(old_comp, new_comp, old_unique, new_unique)

    # Add component removals
    for comp_name, unique_name in log_details['component_removals']:
        report.add_component_removal(comp_name, unique_name,
                                     "Component removed during AWS->GCP conversion")

    # Add all UNIQUE_NAME mappings
    for old_unique, new_unique in log_details['unique_name_mappings'].items():
        report.add_unique_name_mapping(old_unique, new_unique)

    # Add SQL conversions
    for component, original in log_details['sql_api']:
        report.add_sql_api_conversion(component, f"Converted from {original}")

    for component, reason in log_details['sql_fallback']:
        report.add_sql_fallback_conversion(component, "Query converted locally", reason)

    # Add context variables
    for name, var_type, value in converted_info.get('job_level_contexts', []):
        report.add_job_level_context(name, var_type, value)

    for name, repo_id in converted_info.get('repo_level_contexts', []):
        report.add_repo_level_context(name, repo_id)

    # Add warnings as review items
    for query_name, warning in log_details['warnings']:
        report.add_review_item("MEDIUM", query_name, warning)

    # Calculate unchanged components
    report.components_unchanged = (report.total_components -
                                   report.components_replaced -
                                   report.components_removed)

    return report


def main():
    if len(sys.argv) < 3:
        print("Usage: python generate_reports_from_logs.py <log_file> <jobs_folder>")
        print("\nExample:")
        print('  python generate_reports_from_logs.py "logs/repoint_20260527_222324.log" "D:/Talend/.../Jobs"')
        sys.exit(1)

    log_file = sys.argv[1]
    jobs_folder = sys.argv[2]

    if not os.path.exists(log_file):
        print(f"Error: Log file not found: {log_file}")
        sys.exit(1)

    if not os.path.exists(jobs_folder):
        print(f"Error: Jobs folder not found: {jobs_folder}")
        sys.exit(1)

    # Load config for report format
    report_format = getattr(config, 'REPORT_FORMAT', 'md').lower()

    print("="*70)
    print("  GENERATING CONVERSION REPORTS")
    print("="*70)
    print(f"  Log file: {log_file}")
    print(f"  Jobs folder: {jobs_folder}")
    print(f"  Report format: {report_format.upper()}")
    print()

    # Read log file
    with open(log_file, 'r', encoding='utf-8') as f:
        log_content = f.read()

    # Create reports folder parallel to Jobs folder
    parent_dir = os.path.dirname(jobs_folder)
    base_name = os.path.basename(jobs_folder)
    reports_dir = os.path.join(parent_dir, f"{base_name}_reports")

    # Handle old reports folder
    if os.path.exists(reports_dir):
        import shutil
        import time

        try:
            print(f"  Cleaning old reports folder...")
            # Delete report files based on format
            deleted_count = 0
            extensions_to_delete = []

            if report_format in ['md', 'both']:
                extensions_to_delete.append('.md')
            if report_format in ['pdf', 'both']:
                extensions_to_delete.append('.pdf')

            for root, dirs, files in os.walk(reports_dir):
                for file in files:
                    if any(file.endswith(ext) for ext in extensions_to_delete):
                        try:
                            os.remove(os.path.join(root, file))
                            deleted_count += 1
                        except:
                            pass

            if deleted_count > 0:
                print(f"    Removed {deleted_count} old report files")
            else:
                print(f"    Reports folder exists, will overwrite existing reports")

        except Exception as e:
            print(f"    Warning: Could not clean old reports: {e}")
            print(f"    Will overwrite existing reports")
    else:
        print(f"  Creating new reports folder...")

    try:
        os.makedirs(reports_dir, exist_ok=True)
        print(f"  Reports folder: {reports_dir}")
        print()
    except Exception as e:
        print(f"  ❌ ERROR: Could not create reports folder: {e}")
        print(f"  Please check permissions and try again.")
        sys.exit(1)

    # Find all job files
    job_files = []
    for root, dirs, files in os.walk(jobs_folder):
        for file in files:
            if file.endswith('.item'):
                job_files.append(os.path.join(root, file))

    print(f"  Found {len(job_files)} job files")
    print()

    # Get backup folder for original files
    backup_folder = os.path.join(parent_dir, f"{base_name}_backup")

    reports_generated = 0
    reports_failed = 0

    for job_path in job_files:
        job_name = None
        try:
            job_name = os.path.basename(job_path).replace('.item', '')
            rel_path = os.path.relpath(job_path, jobs_folder)

            # Extract only TOP-LEVEL folder name for flat structure
            # Example: "C360_Cashless_Token/SubFolder/DeepFolder/Job.item" → "C360_Cashless_Token"
            path_parts = rel_path.split(os.sep)
            top_level_folder = path_parts[0] if len(path_parts) > 1 else ""

            print(f"  Processing: {job_name}...", flush=True)

            # Parse log for this job
            log_details = parse_log_for_job(log_content, job_name)

            # Analyze original file (with error handling for long paths)
            original_info = {}
            try:
                original_path = os.path.join(backup_folder, rel_path)
                # Handle Windows long paths
                if os.name == 'nt' and len(original_path) > 260:
                    original_path = '\\\\?\\' + os.path.abspath(original_path)

                if os.path.exists(original_path):
                    original_info = analyze_job_file(original_path)
            except Exception as orig_error:
                # Continue without original file analysis if backup not found
                print(f"    (Warning: Could not analyze backup file: {str(orig_error)[:80]})", flush=True)

            # Analyze converted file (with error handling for long paths)
            converted_path = job_path
            if os.name == 'nt' and len(converted_path) > 260:
                converted_path = '\\\\?\\' + os.path.abspath(converted_path)

            converted_info = analyze_job_file(converted_path)

            # Generate report
            report = generate_report_for_job(job_name, log_details, original_info, converted_info)

            # Save report - only 1 level of nesting (top-level folder only)
            job_reports_dir = os.path.join(reports_dir, top_level_folder) if top_level_folder else reports_dir

            # Handle Windows long paths for directory creation
            makedirs_path = job_reports_dir
            if os.name == 'nt' and len(makedirs_path) > 248:  # Directory path limit is 248
                makedirs_path = '\\\\?\\' + os.path.abspath(makedirs_path)

            # Ensure all intermediate directories exist
            os.makedirs(makedirs_path, exist_ok=True)

            report_path = save_report(report, job_reports_dir, format=report_format)

            # Clean path for display
            clean_path = report_path.replace('\\\\?\\', '')
            rel_report_path = os.path.relpath(clean_path, reports_dir)

            # Show format indicator
            format_icon = "📄" if report_format == 'md' else "📕" if report_format == 'pdf' else "📄📕"
            print(f"    -> {format_icon} Saved: {rel_report_path}", flush=True)
            reports_generated += 1

        except Exception as e:
            error_job = job_name if job_name else os.path.basename(job_path)
            # Show complete error with traceback for debugging
            import traceback
            error_msg = str(e)
            print(f"    -> ❌ ERROR: {error_job}: {error_msg}", flush=True)
            # Uncomment for full traceback:
            # traceback.print_exc()
            reports_failed += 1
            continue

    print()
    print("="*70)
    print(f"  ✅ Successfully generated: {reports_generated} reports")
    if reports_failed > 0:
        print(f"  ❌ Failed: {reports_failed} reports")
    print(f"  📊 Total processed: {reports_generated + reports_failed} / {len(job_files)}")
    print(f"  📁 Reports folder: {reports_dir}")
    print("="*70)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ FATAL ERROR: {e}", file=sys.stderr, flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)
