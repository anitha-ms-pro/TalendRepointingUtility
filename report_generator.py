"""
Report Generator for Talend Job Conversions

Generates detailed Markdown reports for each converted job showing:
- Summary statistics
- Component conversions
- SQL conversions (API vs fallback)
- Context variable mappings
- Manual review items
- And more...
"""

import os
from datetime import datetime
from typing import Dict, List, Any

# HTML generation support (no extra dependencies needed)
HTML_ENABLED = True


class JobConversionReport:
    """Generates comprehensive conversion report for a single job."""

    def __init__(self, job_name: str):
        self.job_name = job_name
        self.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Statistics
        self.total_components = 0
        self.components_replaced = 0
        self.components_removed = 0
        self.components_unchanged = 0

        # Detailed tracking
        self.component_replacements = []  # (old_name, new_name, old_unique, new_unique)
        self.component_removals = []      # (component_name, unique_name, reason)
        self.unique_name_mappings = {}    # old_unique -> new_unique

        # SQL conversions
        self.sql_api_conversions = []     # (component, query_snippet)
        self.sql_fallback_conversions = []  # (component, query_snippet, reason)
        self.sql_conversion_errors = []   # (component, error)

        # Context variables
        self.context_var_replacements = {}  # old -> new
        self.job_level_contexts = []      # [(name, type, value)]
        self.repo_level_contexts = []     # [(name, repo_id)]

        # Manual review items
        self.review_items = []            # (severity, component, reason)

        # Warnings
        self.warnings = []                # (component, warning)

    def add_component_replacement(self, old_comp: str, new_comp: str,
                                  old_unique: str, new_unique: str):
        """Track a component replacement."""
        self.component_replacements.append((old_comp, new_comp, old_unique, new_unique))
        self.components_replaced += 1

    def add_component_removal(self, comp_name: str, unique_name: str, reason: str):
        """Track a component removal."""
        self.component_removals.append((comp_name, unique_name, reason))
        self.components_removed += 1

    def add_unique_name_mapping(self, old_unique: str, new_unique: str):
        """Track UNIQUE_NAME mapping."""
        self.unique_name_mappings[old_unique] = new_unique

    def add_sql_api_conversion(self, component: str, query: str):
        """Track SQL converted via GCP API."""
        snippet = query[:100] + "..." if len(query) > 100 else query
        self.sql_api_conversions.append((component, snippet))

    def add_sql_fallback_conversion(self, component: str, query: str, reason: str):
        """Track SQL converted via fallback rules."""
        snippet = query[:100] + "..." if len(query) > 100 else query
        self.sql_fallback_conversions.append((component, snippet, reason))

    def add_sql_error(self, component: str, error: str):
        """Track SQL conversion error."""
        self.sql_conversion_errors.append((component, error))

    def add_context_replacement(self, old_var: str, new_var: str):
        """Track context variable replacement."""
        self.context_var_replacements[old_var] = new_var

    def add_job_level_context(self, name: str, var_type: str = "", value: str = ""):
        """Track job-level context variable."""
        self.job_level_contexts.append((name, var_type, value))

    def add_repo_level_context(self, name: str, repo_id: str):
        """Track repository-level context variable."""
        self.repo_level_contexts.append((name, repo_id))

    def add_review_item(self, severity: str, component: str, reason: str):
        """Add item requiring manual review."""
        self.review_items.append((severity, component, reason))

    def add_warning(self, component: str, warning: str):
        """Add warning message."""
        self.warnings.append((component, warning))

    def calculate_component_accuracy(self) -> float:
        """Calculate component-level conversion accuracy percentage."""
        if self.total_components == 0:
            return 0.0

        successful = self.components_replaced + self.components_unchanged
        return (successful / self.total_components) * 100

    def calculate_sql_accuracy(self) -> float:
        """Calculate SQL-level conversion accuracy percentage."""
        total_sql = len(self.sql_api_conversions) + len(self.sql_fallback_conversions) + len(self.sql_conversion_errors)

        if total_sql == 0:
            return 0.0

        # API conversions are most accurate (GCP-translated)
        # Fallback conversions are less accurate (local regex rules)
        # Errors are failed conversions
        # Weight: API = 100%, Fallback = 70%, Error = 0%
        weighted_success = (len(self.sql_api_conversions) * 1.0) + (len(self.sql_fallback_conversions) * 0.7)
        return (weighted_success / total_sql) * 100

    def calculate_overall_accuracy(self) -> float:
        """Calculate overall job accuracy (combined components + SQL)."""
        component_acc = self.calculate_component_accuracy()
        sql_acc = self.calculate_sql_accuracy()

        # If no SQL conversions, overall = component accuracy only
        total_sql = len(self.sql_api_conversions) + len(self.sql_fallback_conversions) + len(self.sql_conversion_errors)
        if total_sql == 0:
            return component_acc

        # Weighted average: 60% components + 40% SQL
        return (component_acc * 0.6) + (sql_acc * 0.4)

    def generate_report(self) -> str:
        """Generate complete Markdown report."""

        component_accuracy = self.calculate_component_accuracy()
        sql_accuracy = self.calculate_sql_accuracy()
        overall_accuracy = self.calculate_overall_accuracy()

        report = f"""# Conversion Report: {self.job_name}

**Generated**: {self.timestamp}
**Conversion Tool**: Talend AWS → GCP Repointing Utility

---

## 📊 Summary

| Metric | Value |
|--------|-------|
| **Total Components** | {self.total_components} |
| **Components Replaced** | {self.components_replaced} |
| **Components Removed** | {self.components_removed} |
| **Components Unchanged** | {self.components_unchanged} |
| **Component-Level Accuracy** | {component_accuracy:.1f}% |
| **SQL API Conversions** | {len(self.sql_api_conversions)} |
| **SQL Fallback Conversions** | {len(self.sql_fallback_conversions)} |
| **SQL Errors** | {len(self.sql_conversion_errors)} |
| **SQL-Level Accuracy** | {sql_accuracy:.1f}% |
| **Overall Job Accuracy** | {overall_accuracy:.1f}% |
| **Context Variables Replaced** | {len(self.context_var_replacements)} |
| **Manual Review Items** | {len(self.review_items)} |
| **Warnings** | {len(self.warnings)} |

---

### 📈 Accuracy Metrics Explained

**1. Component-Level Accuracy ({component_accuracy:.1f}%)**
- Measures how well components converted from AWS to GCP
- Formula: `(Replaced + Unchanged) / Total × 100`
- Replaced: Successfully converted to GCP equivalent
- Unchanged: No conversion needed (already compatible)
- Removed components **NOT** counted (lost functionality)

**2. SQL-Level Accuracy ({sql_accuracy:.1f}%)**
- Measures SQL query conversion quality
- Weighted: API (100%) + Fallback (70%) + Errors (0%)
- API: GCP Batch Translation (most accurate)
- Fallback: Local regex rules (moderate accuracy)
- Errors: Failed conversions

**3. Overall Job Accuracy ({overall_accuracy:.1f}%)**
- Combined metric: 60% Components + 40% SQL
- Best indicator of overall conversion success

---

## 🔄 Component Conversions

### Components Replaced ({self.components_replaced})

"""

        if self.component_replacements:
            report += "| Original Component | New Component | Original UNIQUE_NAME | New UNIQUE_NAME | Number Preserved |\n"
            report += "|-------------------|---------------|---------------------|-----------------|------------------|\n"

            for old_comp, new_comp, old_unique, new_unique in sorted(self.component_replacements):
                # Check if number was preserved
                import re
                old_num = re.search(r'_(\d+)$', old_unique)
                new_num = re.search(r'_(\d+)$', new_unique)
                preserved = "✅ Yes" if (old_num and new_num and old_num.group(1) == new_num.group(1)) else "⚠️ No"

                report += f"| `{old_comp}` | `{new_comp}` | `{old_unique}` | `{new_unique}` | {preserved} |\n"
        else:
            report += "*No components were replaced in this job.*\n"

        report += "\n### Components Removed ({})".format(self.components_removed)
        report += "\n\n"

        if self.component_removals:
            report += "| Component | UNIQUE_NAME | Reason |\n"
            report += "|-----------|-------------|--------|\n"

            for comp_name, unique_name, reason in self.component_removals:
                report += f"| `{comp_name}` | `{unique_name}` | {reason} |\n"
        else:
            report += "*No components were removed.*\n"

        report += "\n### UNIQUE_NAME Mappings ({})".format(len(self.unique_name_mappings))
        report += "\n\n"

        if self.unique_name_mappings:
            report += "| Original | New |\n"
            report += "|----------|-----|\n"

            for old_unique in sorted(self.unique_name_mappings.keys()):
                new_unique = self.unique_name_mappings[old_unique]
                report += f"| `{old_unique}` | `{new_unique}` |\n"
        else:
            report += "*No UNIQUE_NAME mappings.*\n"

        report += "\n---\n\n## 🗄️ SQL Conversions\n\n"

        report += f"### API Conversions ({len(self.sql_api_conversions)})\n\n"

        if self.sql_api_conversions:
            report += "*SQL queries converted using GCP Batch Translation API:*\n\n"
            for idx, (component, snippet) in enumerate(self.sql_api_conversions, 1):
                report += f"{idx}. **{component}**\n"
                report += f"   ```sql\n   {snippet}\n   ```\n\n"
        else:
            report += "*No API conversions.*\n\n"

        report += f"### Fallback Conversions ({len(self.sql_fallback_conversions)})\n\n"

        if self.sql_fallback_conversions:
            report += "*SQL queries converted using local fallback rules:*\n\n"
            for idx, (component, snippet, reason) in enumerate(self.sql_fallback_conversions, 1):
                report += f"{idx}. **{component}**\n"
                report += f"   - **Reason**: {reason}\n"
                report += f"   ```sql\n   {snippet}\n   ```\n\n"
        else:
            report += "*No fallback conversions.*\n\n"

        report += f"### Conversion Errors ({len(self.sql_conversion_errors)})\n\n"

        if self.sql_conversion_errors:
            report += "| Component | Error |\n"
            report += "|-----------|-------|\n"
            for component, error in self.sql_conversion_errors:
                report += f"| `{component}` | {error} |\n"
        else:
            report += "*No SQL conversion errors.*\n"

        report += "\n---\n\n## 🔧 Context Variables\n\n"

        report += f"### Context Variable Replacements ({len(self.context_var_replacements)})\n\n"

        if self.context_var_replacements:
            report += "| Original | New | Type |\n"
            report += "|----------|-----|------|\n"

            for old_var in sorted(self.context_var_replacements.keys()):
                new_var = self.context_var_replacements[old_var]

                # Determine type
                if old_var.lower().startswith('context.s3_'):
                    var_type = "Pattern (S3)"
                elif old_var.lower().startswith('context.redshift_'):
                    var_type = "Pattern (Redshift)"
                elif old_var.lower().startswith('context.aws_'):
                    var_type = "Pattern (AWS)"
                else:
                    var_type = "Explicit Mapping"

                report += f"| `{old_var}` | `{new_var}` | {var_type} |\n"
        else:
            report += "*No context variable replacements.*\n"

        report += f"\n### Job-Level Context Variables ({len(self.job_level_contexts)})\n\n"

        if self.job_level_contexts:
            report += "| Name | Type | Default Value |\n"
            report += "|------|------|---------------|\n"

            for name, var_type, value in self.job_level_contexts:
                value_display = value[:50] + "..." if len(value) > 50 else value
                report += f"| `{name}` | {var_type} | `{value_display}` |\n"
        else:
            report += "*No job-level context variables.*\n"

        report += f"\n### Repository-Level Context Variables ({len(self.repo_level_contexts)})\n\n"

        if self.repo_level_contexts:
            report += "| Name | Source |\n"
            report += "|------|--------|\n"

            for name, repo_id in self.repo_level_contexts:
                report += f"| `{name}` | From repository context |\n"
        else:
            report += "*No repository-level context variables.*\n"

        report += "\n---\n\n## ⚠️ Manual Review Required\n\n"

        if self.review_items:
            report += "| Severity | Component | Reason |\n"
            report += "|----------|-----------|--------|\n"

            # Sort by severity
            severity_order = {'HIGH': 0, 'MEDIUM': 1, 'LOW': 2}
            sorted_items = sorted(self.review_items,
                                key=lambda x: severity_order.get(x[0], 999))

            for severity, component, reason in sorted_items:
                emoji = "🔴" if severity == "HIGH" else "🟡" if severity == "MEDIUM" else "🟢"
                report += f"| {emoji} {severity} | `{component}` | {reason} |\n"
        else:
            report += "✅ **No manual review required!**\n"

        report += "\n---\n\n## 📋 Warnings\n\n"

        if self.warnings:
            for idx, (component, warning) in enumerate(self.warnings, 1):
                report += f"{idx}. **{component}**: {warning}\n"
        else:
            report += "✅ **No warnings.**\n"

        report += "\n---\n\n## 📝 Notes\n\n"
        report += "**Component Conversions:**\n"
        report += "- ✅ Components with preserved numbers maintain their original numbering\n"
        report += "- ⚠️ Components with changed numbers had conflicts (number already taken)\n"
        report += "\n**Manual Review Severity:**\n"
        report += "- 🔴 HIGH severity items require immediate review before deployment\n"
        report += "- 🟡 MEDIUM severity items should be reviewed before testing\n"
        report += "- 🟢 LOW severity items are informational\n"
        report += "\n**Accuracy Interpretation:**\n"
        report += "- 95-100%: Excellent - Ready for deployment with minimal review\n"
        report += "- 85-94%: Good - Test thoroughly before deployment\n"
        report += "- 70-84%: Fair - Manual review and fixes required\n"
        report += "- < 70%: Poor - Significant manual intervention needed\n"
        report += "\n---\n\n"
        report += "*Report generated by Talend AWS → GCP Repointing Utility*\n"

        return report


def simple_markdown_to_html(markdown_content: str) -> str:
    """Convert basic markdown to HTML without external dependencies."""
    import re

    lines = markdown_content.split('\n')
    html_lines = []
    in_table = False
    in_code_block = False
    code_lang = ''

    i = 0
    while i < len(lines):
        line = lines[i]

        # Code blocks
        if line.strip().startswith('```'):
            if not in_code_block:
                code_lang = line.strip()[3:].strip()
                in_code_block = True
                html_lines.append(f'<pre><code class="language-{code_lang}">')
            else:
                in_code_block = False
                html_lines.append('</code></pre>')
            i += 1
            continue

        if in_code_block:
            html_lines.append(line.replace('<', '&lt;').replace('>', '&gt;'))
            i += 1
            continue

        # Headers
        if line.startswith('# '):
            html_lines.append(f'<h1>{line[2:]}</h1>')
        elif line.startswith('## '):
            html_lines.append(f'<h2>{line[3:]}</h2>')
        elif line.startswith('### '):
            html_lines.append(f'<h3>{line[4:]}</h3>')
        # Horizontal rules
        elif line.strip() == '---':
            html_lines.append('<hr>')
        # Tables
        elif '|' in line and line.strip().startswith('|'):
            if not in_table:
                html_lines.append('<table>')
                in_table = True

            # Check if it's a separator line
            if re.match(r'^\|[\s\-:|]+\|$', line.strip()):
                i += 1
                continue

            cells = [cell.strip() for cell in line.split('|')[1:-1]]

            # Determine if header row (next line is separator)
            is_header = False
            if i + 1 < len(lines):
                next_line = lines[i + 1]
                if re.match(r'^\|[\s\-:|]+\|$', next_line.strip()):
                    is_header = True

            if is_header:
                html_lines.append('<thead><tr>')
                for cell in cells:
                    html_lines.append(f'<th>{process_inline_markdown(cell)}</th>')
                html_lines.append('</tr></thead><tbody>')
            else:
                html_lines.append('<tr>')
                for cell in cells:
                    html_lines.append(f'<td>{process_inline_markdown(cell)}</td>')
                html_lines.append('</tr>')
        else:
            if in_table and '|' not in line:
                html_lines.append('</tbody></table>')
                in_table = False

            if line.strip():
                html_lines.append(f'<p>{process_inline_markdown(line)}</p>')
            elif not in_table:
                html_lines.append('<br>')

        i += 1

    if in_table:
        html_lines.append('</tbody></table>')

    return '\n'.join(html_lines)


def process_inline_markdown(text: str) -> str:
    """Process inline markdown (bold, code, etc.)."""
    import re

    # Inline code
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    # Bold
    text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)

    return text


def markdown_to_html(markdown_content: str, job_name: str) -> str:
    """Convert markdown to styled HTML."""

    # Convert markdown to HTML (simple version, no dependencies)
    html_body = simple_markdown_to_html(markdown_content)

    # HTML template with beautiful CSS styling
    html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{job_name} - Conversion Report</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            line-height: 1.6;
            color: #2c3e50;
            background: #f8f9fa;
            padding: 20px;
        }}

        .container {{
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            padding: 40px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}

        h1 {{
            color: #1a73e8;
            border-bottom: 4px solid #1a73e8;
            padding-bottom: 15px;
            margin-bottom: 30px;
            font-size: 2.2em;
        }}

        h2 {{
            color: #34495e;
            border-bottom: 2px solid #e1e4e8;
            padding-bottom: 10px;
            margin-top: 40px;
            margin-bottom: 20px;
            font-size: 1.8em;
        }}

        h3 {{
            color: #5a6c7d;
            margin-top: 25px;
            margin-bottom: 15px;
            font-size: 1.4em;
        }}

        p {{
            margin-bottom: 15px;
        }}

        table {{
            border-collapse: collapse;
            width: 100%;
            margin: 20px 0;
            font-size: 0.95em;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }}

        thead {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
        }}

        th {{
            padding: 12px 15px;
            text-align: left;
            font-weight: 600;
            border: 1px solid #e1e4e8;
        }}

        td {{
            padding: 10px 15px;
            border: 1px solid #e1e4e8;
        }}

        tbody tr:nth-child(odd) {{
            background-color: #f8f9fa;
        }}

        tbody tr:hover {{
            background-color: #e9ecef;
            transition: background-color 0.2s;
        }}

        code {{
            background-color: #f1f3f5;
            padding: 3px 8px;
            border-radius: 4px;
            font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
            font-size: 0.9em;
            color: #d63384;
        }}

        pre {{
            background-color: #2d2d2d;
            color: #f8f8f2;
            padding: 20px;
            border-radius: 6px;
            overflow-x: auto;
            margin: 20px 0;
            border-left: 4px solid #1a73e8;
        }}

        pre code {{
            background: none;
            padding: 0;
            color: #f8f8f2;
            font-size: 0.9em;
        }}

        hr {{
            border: none;
            border-top: 2px solid #e1e4e8;
            margin: 30px 0;
        }}

        strong {{
            color: #1a73e8;
            font-weight: 600;
        }}

        @media print {{
            body {{
                background: white;
                padding: 0;
            }}
            .container {{
                box-shadow: none;
                padding: 20px;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
{html_body}
    </div>
</body>
</html>
"""
    return html_template


def save_report(report: JobConversionReport, output_dir: str, format: str = 'md'):
    """
    Save report to file.

    Args:
        report: JobConversionReport instance
        output_dir: Output directory path
        format: Report format - 'md', 'html', or 'both'

    Returns:
        Path(s) to saved file(s)
    """
    # Handle Windows long paths
    output_dir_abs = os.path.abspath(output_dir)
    # Lower threshold to account for long filenames (_conversion_report.html = ~30 chars)
    # Directory + filename can exceed 260 even if directory alone is < 200
    if os.name == 'nt' and not output_dir_abs.startswith('\\\\?\\'):
        if len(output_dir_abs) > 180:  # Conservative limit: 180 + 80 char filename = 260 limit
            output_dir_abs = '\\\\?\\' + output_dir_abs

    os.makedirs(output_dir_abs, exist_ok=True)

    # Sanitize job name for filename
    safe_name = "".join(c if c.isalnum() or c in ('_', '-') else '_'
                       for c in report.job_name)

    # Generate markdown content
    markdown_content = report.generate_report()

    saved_files = []
    format = format.lower()

    # Save Markdown
    if format in ['md', 'both']:
        md_filename = f"{safe_name}_conversion_report.md"
        md_filepath = os.path.join(output_dir_abs, md_filename)

        with open(md_filepath, 'w', encoding='utf-8') as f:
            f.write(markdown_content)

        saved_files.append(md_filepath.replace('\\\\?\\', ''))

    # Save HTML
    if format in ['html', 'both']:
        html_filename = f"{safe_name}_conversion_report.html"
        html_filepath = os.path.join(output_dir_abs, html_filename)

        html_content = markdown_to_html(markdown_content, report.job_name)

        with open(html_filepath, 'w', encoding='utf-8') as f:
            f.write(html_content)

        saved_files.append(html_filepath.replace('\\\\?\\', ''))

    # Return first saved file path (for backward compatibility)
    return saved_files[0] if saved_files else None
